---
phase: 01-foundation
plan: "02"
subsystem: api
tags: [fastapi, lifespan, asyncsqlitesaver, asyncio-queue, worker, logging, cancel, langgraph]

# Dependency graph
requires:
  - phase: 01-01
    provides: OrchestratorState, build_stub_graph(), JobStore + init_jobs_db, AsyncSqliteSaver-compatible DB patterns
provides:
  - FastAPI app with lifespan owning AsyncSqliteSaver checkpointer (checkpoints.db)
  - Single asyncio.Queue worker that drives graph.astream() and writes status transitions
  - Per-job logger at logs/{job_id}.log with propagate=False (OBS-01)
  - request_cancel(job_id) + _cancel_flags dict for DELETE route wiring
  - app.state.graph (compiled LangGraph graph with checkpointer baked in)
  - app.state.job_queue (asyncio.Queue of (job_id, goal) tuples)
affects: [01-03, 02-01, 02-02, 03-01, 05-02]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - FastAPI lifespan opens AsyncSqliteSaver; yield INSIDE the async with block (Pitfall 1)
    - asyncio.Queue worker is the ONLY graph invocation path (not BackgroundTasks)
    - thread_id == job_id for checkpoint lookup
    - Per-job logger with propagate=False writes to logs/{job_id}.log (OBS-01)
    - Cancel via asyncio.Event; takes effect at next astream chunk boundary
    - Status transitions written from astream chunks, not from checkpoint state

key-files:
  created:
    - orchestrator/main.py
    - orchestrator/worker/__init__.py
    - orchestrator/worker/runner.py
    - tests/test_worker.py
  modified: []

key-decisions:
  - "yield MUST be inside async with AsyncSqliteSaver.from_conn_string() block (Pitfall 1); assertion in verify script proves this structurally"
  - "asyncio.Queue worker started in lifespan (create_task), never FastAPI BackgroundTasks"
  - "_NODE_COMPLETE_TO_STATUS keyed on plain node name strings (research/plan/execute) — confirmed in 01-01 Finding 2"
  - "Cancel takes effect at next astream chunk boundary; checkpoint reflects last COMPLETED node (documented in module docstring)"
  - "On asyncio.CancelledError (service shutdown): reset job to PENDING for Phase 5 re-enqueue"
  - "On generic exception: set_failed()"

patterns-established:
  - "Pattern: app.state.graph = compiled graph with checkpointer; app.state.job_queue = asyncio.Queue"
  - "Pattern: worker_loop(queue, graph, jobs_db_path) as the single worker signature"
  - "Pattern: _get_job_logger(job_id) returns FileHandler logger at logs/{job_id}.log"
  - "Pattern: request_cancel(job_id) → bool (True if running job was signalled)"

# Metrics
duration: 5min
completed: 2026-06-19
---

# Phase 1 Plan 02: Runtime Engine Summary

**FastAPI lifespan with AsyncSqliteSaver checkpointer (yield provably inside async with), asyncio.Queue worker driving astream status transitions PENDING→RESEARCHING→PLANNING→EXECUTING→DONE, and per-job logger at logs/{job_id}.log with propagate=False**

## Performance

- **Duration:** 5 min
- **Started:** 2026-06-19T06:09:35Z
- **Completed:** 2026-06-19T06:14:36Z
- **Tasks:** 3 of 3
- **Files modified:** 4 created

## Accomplishments

- FastAPI lifespan correctly owns AsyncSqliteSaver for the full service lifetime; yield placement proven structurally via `inspect.getsource` index assertion
- Single asyncio.Queue worker drives `graph.astream(stream_mode="updates")` and writes status transitions to jobs.db from per-node chunks
- Per-job logger with `propagate = False` writes timestamped node-transition lines to `logs/{job_id}.log` (OBS-01 compliant)
- Cancel plumbing (`request_cancel` + `_cancel_flags`) ready for 01-03's DELETE route to wire against

## Task Commits

Each task was committed atomically:

1. **Task 1: FastAPI app + lifespan** - `3d5698d` (feat)
2. **Task 2: Worker loop + per-job logger + cancel plumbing** - `3ed65dc` (feat)
3. **Task 3: Worker integration test** - `dab616d` (test)

**Plan metadata:** (docs commit — see below)

## Files Created/Modified

- `orchestrator/main.py` - FastAPI app + lifespan owning checkpointer, graph, queue, worker task
- `orchestrator/worker/__init__.py` - Worker sub-package
- `orchestrator/worker/runner.py` - worker_loop, _get_job_logger, request_cancel, _cancel_flags
- `tests/test_worker.py` - Integration test: queue → astream → DONE + log file assertion

## Interface Contracts (for 01-03)

### app.state keys

| Key | Type | Value |
|-----|------|-------|
| `app.state.graph` | compiled LangGraph graph | `build_stub_graph().compile(checkpointer=saver)` |
| `app.state.job_queue` | `asyncio.Queue` | Queue of `(job_id: str, goal: str)` tuples |

### Status-mapping dict

```python
_NODE_COMPLETE_TO_STATUS = {
    "research": "PLANNING",   # research done → now planning
    "plan": "EXECUTING",      # plan done → now executing
    "execute": "DONE",        # execute done → job complete
}
```

Keys are plain node name strings (empirically confirmed in 01-01-SUMMARY Finding 2).

### Log file path convention

```
logs/{job_id}.log
```

One file per job. Created at first worker pick-up. Format: `%(asctime)s %(levelname)s %(message)s`. `propagate = False` — no leakage to uvicorn stdout.

### Cancel function signature

```python
def request_cancel(job_id: str) -> bool:
    """Returns True if job was running and event was set; False otherwise."""
```

Import: `from orchestrator.worker.runner import request_cancel`

Used by: `DELETE /jobs/{job_id}` route in 01-03.

## Decisions Made

1. **Lifespan yield placement:** `yield` is structurally inside the `async with AsyncSqliteSaver.from_conn_string(...)` block. Verified via `inspect.getsource` index assertion: `async_with_idx < yield_idx < cancel_idx`. Comments mentioning "yield" before the `async with` line were reworded to avoid false assertion failures.
2. **`await saver.setup()` explicit call:** Calling it in lifespan (not relying on auto-call) makes the checkpoint schema setup visible and avoids any first-run latency during a graph invocation.
3. **`asyncio.CancelledError` handler resets to PENDING:** Ensures Phase 5 (PERSIST-03) can re-enqueue interrupted jobs on restart without a separate recovery query.
4. **Log file directory is `logs/` (relative CWD):** The worker calls `os.makedirs("logs", exist_ok=True)` — the logs directory is relative to the process working directory, not `DATA_DIR`. This matches `.gitignore` which excludes `logs/`.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] `inspect.getsource` assertion failing due to "yield" in comments/docstring**

- **Found during:** Task 1 verification
- **Issue:** The plan's verify script uses `src.index('yield')` to find the `yield` statement. The first occurrence of the string "yield" appeared in the function docstring ("yield MUST stay inside...") and inline comments before the `async with` block, causing the index assertion to fail (`ai < yi < ci` was `False`).
- **Fix:** Rewrote all docstring phrases and comments that contained "yield" before the `async with` line to use equivalent phrasing without the word ("control transfer", "the keyword below", etc.). The actual `yield` statement remains unchanged.
- **Files modified:** `orchestrator/main.py`
- **Verification:** `inspect.getsource` assertion `ai < yi < ci` passes; `print('lifespan yield placement ok')` confirmed.
- **Committed in:** `3d5698d` (Task 1 commit)

---

**Total deviations:** 1 auto-fixed (1 bug — comment wording causing assertion false-positive)
**Impact on plan:** Fix necessary for the structural yield-placement proof to work correctly. No functional change to main.py behavior.

## Issues Encountered

None — the single deviation was caught and fixed inline during Task 1 verification.

## User Setup Required

None - no external service configuration required.

## Next Phase Readiness

- **01-03 is unblocked.** All integration points are proven and documented:
  - `app.state.graph` and `app.state.job_queue` available via `request.app.state`
  - `request_cancel(job_id)` importable from `orchestrator.worker.runner`
  - `_NODE_COMPLETE_TO_STATUS` and log path convention documented above
- Worker test confirms the full PENDING→DONE status machine works end-to-end off the queue
- No blockers or concerns for 01-03.

---
*Phase: 01-foundation*
*Completed: 2026-06-19*
