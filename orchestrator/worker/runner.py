"""orchestrator/worker/runner.py
Single asyncio.Queue worker that drives the LangGraph stub graph and writes
job-status transitions to jobs.db from astream(stream_mode="updates") events.

Status state machine (written to jobs.db per astream chunk):
  PENDING  →  RESEARCHING  →  PLANNING  →  EXECUTING  →  DONE
                                                       ↘  FAILED   (on exception)
                                                       ↘  CANCELLED (on cancel signal)

Cancel semantics (API-04 / RESEARCH Pattern 6):
  Cancel takes effect AFTER the current astream chunk completes — never mid-node.
  LangGraph checkpoints the last COMPLETED node, so cancelling mid-research means
  the checkpoint has no research data; cancelling mid-plan means research is
  checkpointed but plan is not. This is expected behavior: the checkpoint reflects
  the last fully-completed node, not a partially-executed one.

  On service shutdown (asyncio.CancelledError from worker_task.cancel()), the job
  status is reset to PENDING so Phase 5 can re-enqueue it on restart.
"""
import asyncio
import logging
import os
from typing import Optional

from orchestrator.persistence.job_store import JobStore

# ---------------------------------------------------------------------------
# Status mapping: keyed on the astream chunk node name (empirically confirmed
# in 01-01-SUMMARY.md Finding 2: chunk keys are plain strings "research" /
# "plan" / "execute" — no namespace wrapping).
#
# Semantics: when node N completes, set job status to the NEXT phase label.
# ---------------------------------------------------------------------------
_NODE_COMPLETE_TO_STATUS: dict[str, str] = {
    "research": "PLANNING",   # research done → now planning
    "plan": "EXECUTING",      # plan done → now executing
    "execute": "DONE",        # execute done → job complete
}

# ---------------------------------------------------------------------------
# Cancel plumbing (RESEARCH Pattern 6).
# HTTP DELETE route (01-03) calls request_cancel(job_id) to signal the worker.
# ---------------------------------------------------------------------------
_cancel_flags: dict[str, asyncio.Event] = {}


def request_cancel(job_id: str) -> bool:
    """Signal the running worker to stop after the current astream chunk.

    Called from the DELETE /jobs/{job_id} route (01-03).

    Returns:
        True  — job was running and the cancel event was set.
        False — job is not currently running (may be queued or already terminal).

    Cancel takes effect after the next astream chunk boundary.  The checkpoint
    reflects the last COMPLETED node, never mid-node state (see module docstring).
    """
    event = _cancel_flags.get(job_id)
    if event:
        event.set()
        return True
    return False


# ---------------------------------------------------------------------------
# Per-job logger (RESEARCH Pattern 7 / OBS-01).
# ---------------------------------------------------------------------------

def _get_job_logger(job_id: str) -> logging.Logger:
    """Return a per-job logger writing timestamped lines to logs/{job_id}.log.

    Idempotent: the FileHandler is added only once per job_id.

    propagate = False is CRITICAL (RESEARCH Pitfall 4 / OBS-01):
      Without it, every log record reaches uvicorn's root-logger StreamHandler
      and appears TWICE — once in the per-job file and once in uvicorn stdout.
      Setting propagate=False keeps job content out of the shared service log.

    Log file location convention: logs/{job_id}.log
    Format: %(asctime)s %(levelname)s %(message)s  (ISO 8601 timestamps)
    """
    logger = logging.getLogger(f"job.{job_id}")
    if not logger.handlers:
        os.makedirs("logs", exist_ok=True)
        log_path = os.path.join("logs", f"{job_id}.log")
        fh = logging.FileHandler(log_path)
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(fh)
        logger.propagate = False  # do NOT send to uvicorn root logger (OBS-01)
        logger.setLevel(logging.DEBUG)
    return logger


# ---------------------------------------------------------------------------
# Worker loop (RESEARCH Pattern 4).
# ---------------------------------------------------------------------------

async def worker_loop(
    queue: asyncio.Queue,
    graph,
    jobs_db_path: str,
) -> None:
    """Drain the asyncio.Queue, run each job through the graph, write status.

    This is the ONLY code that calls graph.astream(). HTTP handlers enqueue
    (job_id, goal) tuples; they never invoke the graph directly.

    Per-job flow:
      1. Pick up (job_id, goal) from queue.
      2. Register a cancel asyncio.Event in _cancel_flags[job_id].
      3. Set status RESEARCHING, build initial_state (thread_id == job_id).
      4. astream(initial_state, stream_mode="updates"):
           - Before processing each chunk: check cancel flag → CANCELLED + break.
           - After each chunk: map node_name → next_status via _NODE_COMPLETE_TO_STATUS.
           - On "execute" complete: fetch final state, call set_complete().
      5. On asyncio.CancelledError (service shutdown): reset to PENDING, re-raise.
      6. On generic exception: set_failed().
      7. Finally: pop cancel flag, queue.task_done().
    """
    store = JobStore(jobs_db_path)

    while True:
        job_id, goal = await queue.get()
        job_logger = _get_job_logger(job_id)

        # Register cancel signal for this job (cleared in finally).
        cancel_event: asyncio.Event = asyncio.Event()
        _cancel_flags[job_id] = cancel_event

        try:
            # Transition: PENDING → RESEARCHING (worker just picked up the job).
            await store.set_status(job_id, "RESEARCHING")
            job_logger.info("RESEARCHING started")

            config = {"configurable": {"thread_id": job_id}}
            initial_state = {
                "goal": goal,
                "job_id": job_id,
                "research_findings": None,
                "plan": None,
                "execution_result": None,
                "execution_status": None,
                "event_log": [],
                "node_models": {},  # OBS-03: empty dict, NOT None (merge reducer requires dict)
            }

            # astream yields one chunk per completed node.
            # Chunk shape confirmed empirically in 01-01-SUMMARY.md Finding 2:
            #   {node_name_string: partial_state_dict}
            # e.g. {"research": {"research_findings": "...", "event_log": [...]}}
            async for chunk in graph.astream(
                initial_state, config=config, stream_mode="updates"
            ):
                # Check cancel flag BEFORE processing the chunk so the worker
                # stops at the next chunk boundary (not mid-node).
                if cancel_event.is_set():
                    await store.set_status(job_id, "CANCELLED")
                    job_logger.info("CANCELLED (cancel signal received)")
                    break

                for node_name in chunk:
                    next_status: Optional[str] = _NODE_COMPLETE_TO_STATUS.get(node_name)
                    if next_status is None:
                        # Unknown node name — log and skip; do not fail the job.
                        job_logger.warning(
                            "Unknown node name in astream chunk: %r", node_name
                        )
                        continue

                    if next_status == "DONE":
                        # Fetch final state from checkpointer to extract the result.
                        final_state = await graph.aget_state(config)
                        result: Optional[str] = final_state.values.get(
                            "execution_result"
                        )
                        await store.set_complete(job_id, result)
                        job_logger.info("DONE: %s complete; result=%r", node_name, result)
                    else:
                        await store.set_status(job_id, next_status)
                        job_logger.info(
                            "%s started (after %s node complete)", next_status, node_name
                        )

        except asyncio.CancelledError:
            # Service shutdown — worker_task.cancel() was called in lifespan.
            # Reset to PENDING so Phase 5 (PERSIST-03) can re-enqueue on restart.
            await store.set_status(job_id, "PENDING")
            job_logger.info("Worker cancelled (service shutdown); reset to PENDING")
            raise  # Re-raise so the task exits cleanly.

        except Exception as exc:
            await store.set_failed(job_id, str(exc))
            job_logger.exception("FAILED: %s", exc)

        finally:
            # Always clean up cancel flag and mark the queue item done.
            _cancel_flags.pop(job_id, None)
            queue.task_done()
