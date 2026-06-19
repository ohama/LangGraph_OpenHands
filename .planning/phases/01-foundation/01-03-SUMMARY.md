# Summary: 01-03 — REST surface + end-to-end verification

**Plan:** 01-03
**Status:** Complete
**Completed:** 2026-06-19

## What was built

The operator-facing REST layer over the 01-02 runtime engine, plus the single runnable proof of all 5 Phase 1 success criteria.

- `orchestrator/api/models.py` — Pydantic v2 models: `GoalRequest` (goal, min_length=1 → 422 on empty), `JobStatusResponse`, `JobResultResponse`.
- `orchestrator/api/routes.py` — `APIRouter` with five endpoints (status read ONLY from jobs.db; POST writes the row before enqueue).
- `orchestrator/main.py` — `app.include_router(router)` at the 01-02 TODO marker.
- `tests/test_api.py` — `TestClient` integration tests (submit→poll→DONE, result 409-then-200, health, cancel 404/terminal, per-run log present).
- `scripts/verify_phase1.sh` — real-uvicorn end-to-end proof incl. `kill -9` + restart durability.

## Endpoint contract

| Method | Path | Success | Errors |
|--------|------|---------|--------|
| POST | `/goals` | 202 `{job_id, status:"PENDING"}` | 422 empty goal |
| GET | `/jobs/{job_id}/status` | 200 `{job_id, status}` | 404 unknown |
| GET | `/jobs/{job_id}/result` | 200 `{job_id, result}` | 404 unknown · 409 not DONE |
| DELETE | `/jobs/{job_id}` | 200 cancelled + caveat | 404 unknown · "already terminal" |
| GET | `/health` | 200 `{status:"ok", sqlite:"reachable"}` | 503 sqlite unreachable |

Cancel caveat string returned: "checkpoint reflects last completed node, not mid-node state" (API-04). `request_cancel` imported lazily inside the DELETE handler to avoid an import cycle.

## Launch command (used by verify script; carried to Phase 5 launchd)

```
DATA_DIR=<data> LOG_DIR=<logs> .venv/bin/uvicorn orchestrator.main:app --workers 1 --port 8099
```
- `--workers 1` is mandatory: a single in-process asyncio.Queue worker; multi-worker would fragment the queue.
- Launch via the venv's `uvicorn` binary directly (NOT `uv run uvicorn`) — see deviation below.

## Requirement coverage (10/10 for Phase 1)

| Req | Satisfied by |
|-----|--------------|
| ORCH-04 | 01-01 `OrchestratorState` (typed, no messages) |
| PERSIST-01 | checkpoint survives kill+restart — verify_phase1.sh criterion 3 + bonus (checkpoints.db) |
| PERSIST-02 | 01-01 `JobStore`/`init_jobs_db` WAL jobs.db; exercised end-to-end here |
| API-01 | `POST /goals` 202 + job_id |
| API-02 | `GET /jobs/{id}/status` |
| API-03 | `GET /jobs/{id}/result` (404/409 guards) |
| API-04 | `DELETE /jobs/{id}` + 01-02 `request_cancel` |
| API-05 | `GET /health` (SQLite liveness) |
| API-06 | 01-02 asyncio.Queue worker (not BackgroundTasks); POST is non-blocking |
| OBS-01 | per-job `logs/{job_id}.log` (01-02 logger), asserted here |

## Verification result

- `uv run pytest -q` → **18 passed**.
- `bash scripts/verify_phase1.sh` → **PHASE 1 VERIFICATION PASSED** (criteria 1–5, incl. real `kill -9` + restart; checkpoint found in checkpoints.db).

## Deviation (auto-fixed, important)

**`uv run uvicorn` orphans the server on kill.** The verify script originally launched the server via `uv run uvicorn …`. `uv run` spawns uvicorn as a child; `kill -9 $!` killed only the `uv` wrapper, leaving an orphaned uvicorn still LISTENing on the port. This caused two symptoms during verification:
1. Intermittent **500 on the first POST** — a stale orphan from a prior run (whose data dir had been `rm -rf`'d) answered `/health` (200) but failed the INSERT on its unlinked DB. The error was invisible (old process, old code, deleted log), which is why it was not a code bug.
2. **Criterion 3 "Port not freed within 30s"** — the restart step's `kill -9` left the real uvicorn child holding the port.

**Fix:** launch via `"$PROJECT_DIR/.venv/bin/uvicorn"` directly so `$!` is the actual server process; `kill -9` then frees the port immediately. This also matches how Phase 5 will run the service under launchd (absolute path to `.venv/bin/uvicorn`). All Phase-1 application code was correct; no source change was needed beyond the script's launch command.

**Operational note for later phases:** any tooling that starts the service must kill the real uvicorn process (or process group), never just a `uv run` wrapper, to avoid orphaned port-holders.

## Empirical findings carried from 01-01 (still authoritative for Phase 2/5)

- Resume input: `astream(None, config)` skips completed nodes; `astream(initial_state, …)` re-runs all → **Phase 5 PERSIST-03 must pass `None`**.
- `astream(stream_mode="updates")` chunk shape is plain `{node_name: partial_state}` (no namespace) → worker maps via `for node_name in chunk`.
