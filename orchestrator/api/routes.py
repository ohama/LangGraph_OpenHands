# orchestrator/api/routes.py
# FastAPI router exposing the five REST endpoints (RESEARCH.md Pattern 9):
#   POST   /goals                -> 202 + job_id
#   GET    /jobs/{job_id}/status -> 200 / 404
#   GET    /jobs/{job_id}/result -> 200 / 404 / 409
#   DELETE /jobs/{job_id}        -> 200 / 404
#   GET    /health               -> 200 / 503
#
# Critical ordering (RESEARCH Pitfall 2):
#   POST /goals MUST call await store.create_job() BEFORE await queue.put().
#   The worker can pick up the tuple from the queue immediately after put();
#   if the DB row doesn't exist yet, set_status("RESEARCHING") silently affects
#   0 rows and GET /jobs/{id}/status returns 404.
#
# Import request_cancel LAZILY inside DELETE handler to avoid import cycle at
# module load time (routes -> runner imports would form a cycle if top-level).
import os
import uuid

import aiosqlite
from fastapi import APIRouter, HTTPException, Request

from orchestrator.api.models import GoalRequest, JobResultResponse, JobStatusResponse
from orchestrator.persistence.job_store import JobStore

router = APIRouter()


def _jobs_db_path() -> str:
    """Return the same jobs.db path that main.py uses (DATA_DIR env var, default 'data')."""
    data_dir = os.environ.get("DATA_DIR", "data")
    return os.path.join(data_dir, "jobs.db")


# ---------------------------------------------------------------------------
# POST /goals — submit a new goal and return immediately (API-01, API-06)
# ---------------------------------------------------------------------------

@router.post("/goals", status_code=202)
async def submit_goal(req: GoalRequest, request: Request):
    """Submit a new goal. Returns 202 + job_id immediately; graph runs in background.

    Ordering guarantee (RESEARCH Pitfall 2):
      1. create_job() inserts the DB row (PENDING).
      2. queue.put() enqueues the job for the worker.
    The worker MUST NOT pick up the job before step 1 completes.
    """
    job_id = str(uuid.uuid4())
    store = JobStore(_jobs_db_path())

    # STEP 1 — write the row to jobs.db BEFORE enqueue (Pitfall 2).
    await store.create_job(job_id, req.goal)

    # STEP 2 — enqueue for the background worker.
    await request.app.state.job_queue.put((job_id, req.goal))

    return {"job_id": job_id, "status": "PENDING"}


# ---------------------------------------------------------------------------
# GET /jobs/{job_id}/status — live status from jobs.db (API-02)
# ---------------------------------------------------------------------------

@router.get("/jobs/{job_id}/status", response_model=JobStatusResponse)
async def get_status(job_id: str):
    """Return current status from jobs.db. Status is written by the worker; never
    derived from the checkpoint (RESEARCH Pattern 5)."""
    store = JobStore(_jobs_db_path())
    job = await store.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return JobStatusResponse(job_id=job_id, status=job["status"])


# ---------------------------------------------------------------------------
# GET /jobs/{job_id}/result — result for DONE jobs (API-03)
# ---------------------------------------------------------------------------

@router.get("/jobs/{job_id}/result", response_model=JobResultResponse)
async def get_result(job_id: str):
    """Return execution result for a DONE job.

    Returns 404 for unknown job_id and 409 if the job is not yet DONE.
    """
    store = JobStore(_jobs_db_path())
    job = await store.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    if job["status"] != "DONE":
        raise HTTPException(
            status_code=409,
            detail=f"Job status is {job['status']}, not DONE",
        )
    return JobResultResponse(job_id=job_id, result=job["result"])


# ---------------------------------------------------------------------------
# DELETE /jobs/{job_id} — cancel a running or queued job (API-04)
# ---------------------------------------------------------------------------

_TERMINAL_STATUSES = frozenset({"DONE", "FAILED", "CANCELLED"})


@router.delete("/jobs/{job_id}", status_code=200)
async def cancel_job(job_id: str):
    """Signal cancellation for a running or queued job.

    For a running job:  calls request_cancel(job_id) which sets the asyncio.Event
                        in the worker; the worker stops at the next astream chunk.
    For a queued job:   request_cancel returns False; we set status CANCELLED directly.

    Cancel semantics (RESEARCH Pattern 6):
      - Cancellation takes effect at the next astream chunk boundary, NOT mid-node.
      - The checkpoint reflects the last FULLY-COMPLETED node; partial node state
        is NOT persisted.

    Import is lazy (inside function body) to avoid import cycles at module load.
    """
    store = JobStore(_jobs_db_path())
    job = await store.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    if job["status"] in _TERMINAL_STATUSES:
        return {
            "job_id": job_id,
            "status": job["status"],
            "note": "already terminal",
        }

    # Lazy import to avoid circular import at module-load time.
    from orchestrator.worker.runner import request_cancel  # noqa: PLC0415

    was_running = request_cancel(job_id)
    if not was_running:
        # Job is queued but not yet picked up by the worker — set directly.
        await store.set_status(job_id, "CANCELLED")

    return {
        "job_id": job_id,
        "status": "CANCELLED",
        "caveat": "checkpoint reflects last completed node, not mid-node state",
    }


# ---------------------------------------------------------------------------
# GET /health — SQLite liveness probe (API-05)
# ---------------------------------------------------------------------------

@router.get("/health")
async def health():
    """Liveness probe. Verifies jobs.db is reachable via a no-op query.

    Returns 200 {"status": "ok", "sqlite": "reachable"} on success.
    Returns 503 {"status": "degraded", "sqlite": "<error>"} on failure.
    """
    try:
        async with aiosqlite.connect(_jobs_db_path()) as conn:
            await conn.execute("SELECT 1")
        return {"status": "ok", "sqlite": "reachable"}
    except Exception as exc:
        from fastapi.responses import JSONResponse

        return JSONResponse(
            status_code=503,
            content={"status": "degraded", "sqlite": str(exc)},
        )
