# tests/test_worker.py
# Worker integration test (01-02 Task 3).
#
# Exercises the real worker_loop against a real AsyncSqliteSaver + JobStore on
# temp DB paths.  No HTTP layer involved — that comes in 01-03.
#
# Assertions (per plan):
#   1. Final job status == "DONE"
#   2. result == "STUB: execution complete"
#   3. Intermediate status RESEARCHING was observed before DONE
#   4. logs/{job_id}.log exists and contains "RESEARCHING" and "DONE" substrings
import asyncio
import os
import tempfile
import uuid

import pytest
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from orchestrator.graph.stub_graph import build_stub_graph
from orchestrator.persistence.job_store import JobStore, init_jobs_db
from orchestrator.worker.runner import worker_loop


async def _poll_job_until_done(
    store: JobStore,
    job_id: str,
    timeout: float = 15.0,
    interval: float = 0.25,
) -> list[str]:
    """Poll get_job() until status reaches a terminal state.

    Returns the list of all distinct statuses observed in poll order.
    Raises TimeoutError if the job does not complete within `timeout` seconds.
    """
    observed: list[str] = []
    deadline = asyncio.get_event_loop().time() + timeout

    while True:
        job = await store.get_job(job_id)
        assert job is not None, f"Job {job_id} not found in jobs.db"
        status: str = job["status"]

        if not observed or observed[-1] != status:
            observed.append(status)

        if status in ("DONE", "FAILED", "CANCELLED"):
            return observed

        if asyncio.get_event_loop().time() > deadline:
            raise TimeoutError(
                f"Job {job_id} did not reach terminal status within {timeout}s; "
                f"last status={status!r}, observed={observed}"
            )

        await asyncio.sleep(interval)


async def test_worker_drives_status_to_done_and_writes_log():
    """Full worker integration: queue → astream → DONE; per-job log file written.

    Uses a temp checkpoints.db and a temp jobs.db so test isolation is guaranteed.
    The stub graph has real asyncio.sleep delays (~4s total); timeout is 15s.
    """
    job_id = f"test-worker-{uuid.uuid4().hex[:8]}"
    goal = "integration test goal"

    with (
        tempfile.NamedTemporaryFile(suffix=".db", delete=False) as ckpt_f,
        tempfile.NamedTemporaryFile(suffix=".db", delete=False) as jobs_f,
    ):
        ckpt_path = ckpt_f.name
        jobs_path = jobs_f.name

    worker_task = None
    try:
        # 1. Init jobs.db schema
        await init_jobs_db(jobs_path)
        store = JobStore(jobs_path)

        # 2. Open AsyncSqliteSaver, compile graph
        async with AsyncSqliteSaver.from_conn_string(ckpt_path) as saver:
            await saver.setup()
            graph = build_stub_graph().compile(checkpointer=saver)

            # 3. Create job row in jobs.db BEFORE enqueuing (Pitfall 2: create_job first)
            await store.create_job(job_id, goal)

            # 4. Start worker
            queue: asyncio.Queue = asyncio.Queue()
            worker_task = asyncio.create_task(
                worker_loop(queue, graph, jobs_path)
            )

            # 5. Enqueue the job
            await queue.put((job_id, goal))

            # 6. Poll until DONE (timeout 15s; stub graph takes ~4s)
            observed_statuses = await _poll_job_until_done(store, job_id, timeout=15.0)

        # ── Assertions ──────────────────────────────────────────────────────

        # a) Final status must be DONE
        final_job = await store.get_job(job_id)
        assert final_job is not None
        assert final_job["status"] == "DONE", (
            f"Expected DONE, got {final_job['status']!r}; observed={observed_statuses}"
        )

        # b) Result must match the stub's return value
        assert final_job["result"] == "STUB: execution complete", (
            f"Unexpected result: {final_job['result']!r}"
        )

        # c) RESEARCHING must have been observed before DONE (proves per-node transitions)
        assert "RESEARCHING" in observed_statuses, (
            f"RESEARCHING not observed; statuses seen: {observed_statuses}"
        )
        researching_idx = observed_statuses.index("RESEARCHING")
        done_idx = observed_statuses.index("DONE")
        assert researching_idx < done_idx, (
            f"RESEARCHING ({researching_idx}) must precede DONE ({done_idx})"
        )

        # d) At least one intermediate transition (PLANNING or EXECUTING) seen
        intermediates = {"PLANNING", "EXECUTING"}
        assert intermediates.intersection(observed_statuses), (
            f"Expected at least one of {intermediates}; observed={observed_statuses}"
        )

        # e) Per-job log file exists at logs/{job_id}.log
        log_path = os.path.join("logs", f"{job_id}.log")
        assert os.path.exists(log_path), (
            f"Per-job log file not found at {log_path!r}"
        )

        # f) Log file contains at least one timestamped RESEARCHING line and one DONE line
        with open(log_path) as lf:
            log_content = lf.read()

        assert "RESEARCHING" in log_content, (
            f"'RESEARCHING' not found in log file {log_path!r}; content:\n{log_content}"
        )
        assert "DONE" in log_content, (
            f"'DONE' not found in log file {log_path!r}; content:\n{log_content}"
        )

    finally:
        # Cancel worker task cleanly; swallow CancelledError.
        if worker_task is not None and not worker_task.done():
            worker_task.cancel()
            try:
                await worker_task
            except asyncio.CancelledError:
                pass

        # Clean up temp DB files.
        for path in (ckpt_path, jobs_path):
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
