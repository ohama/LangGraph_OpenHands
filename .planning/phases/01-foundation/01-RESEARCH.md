# Phase 1: Foundation — Research

**Researched:** 2026-06-19
**Domain:** FastAPI lifespan + AsyncSqliteSaver + asyncio.Queue worker + stub LangGraph graph + aiosqlite job registry
**Confidence:** HIGH (primary questions resolved via official API reference, GitHub source, and official docs; two LOW-confidence sub-items flagged for empirical verification)

---

## Summary

Phase 1 builds the entire durable-persistence and event-loop-safe-worker skeleton **before** any real LLM call. It uses a stub LangGraph graph (nodes just `asyncio.sleep` and update state) to prove the AsyncSqliteSaver checkpoint pattern, the FastAPI lifespan ownership of the checkpointer, and the asyncio.Queue single-worker model. Real node logic (LLM calls, OpenHands SDK) slots in during Phases 2–3 without touching the skeleton.

The most important correctness requirement for this phase is the `AsyncSqliteSaver.from_conn_string()` lifecycle. `from_conn_string` is itself an `@asynccontextmanager` — the FastAPI lifespan `yield` **must** be placed _inside_ the `async with AsyncSqliteSaver.from_conn_string(...) as saver:` block. Placing `yield` outside that block (e.g. after the `async with` closes) would close the SQLite connection before any request is handled, causing all graph invocations to fail with "connection closed" errors. This is the #1 implementation bug found in third-party tutorials.

The second key pattern is job-status tracking: each stub node writes its status transition directly to `jobs.db` at node entry (not at node exit), so `GET /jobs/{id}/status` always reflects which node is currently executing (PENDING → RESEARCHING → PLANNING → EXECUTING → DONE). This write happens inside the stub node itself using aiosqlite, making the status observable without polling the checkpointer.

**Primary recommendation:** Wire `AsyncSqliteSaver` inside the FastAPI lifespan context, compile the graph once at startup, share via `app.state.graph`, and use `astream` with `stream_mode="updates"` in the worker to get per-node callbacks for status writes.

---

## Standard Stack

### Core (Phase 1 only — Phase 2/3 add langchain-openai, openhands-sdk)

| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| `langgraph` | 1.2.6 | StateGraph, stub node definitions, ainvoke/astream, checkpoint integration | Stable 1.x API; superstep-level checkpointing built-in |
| `langgraph-checkpoint-sqlite` | 3.1.0 | AsyncSqliteSaver — durable per-thread checkpoints | Zero-ops SQLite; MUST be >=3.0.1 (CVE-2025-67644 SQL injection in thread_id parameter) |
| `fastapi` | 0.137.2 | HTTP API, lifespan context manager | Standard for async Python APIs |
| `uvicorn[standard]` | 0.49.0 | ASGI server; `[standard]` pulls uvloop for faster event loop | Pairs with FastAPI; `--workers 1` for single asyncio.Queue worker |
| `aiosqlite` | >=0.19 | Async job registry (`jobs.db`) | Transitive dep of langgraph-checkpoint-sqlite; use for `jobs.db` directly |
| `pydantic` | >=2.7 | Request/response models (GoalRequest, JobStatusResponse) | FastAPI 0.137 requires pydantic v2 |
| `python-dotenv` | >=1.0 | Load `.env` for dev; launchd plist handles prod env | Avoids hardcoded paths in dev |

### Supporting

| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| `pytest` | >=8.0 | Test suite | All tests |
| `pytest-asyncio` | >=0.24 | `async def test_*` support | Required for aiosqlite and LangGraph async tests |
| `ruff` | latest | Lint + format (replaces flake8/black/isort) | One tool, zero config |

### Alternatives Considered

| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| `AsyncSqliteSaver` (async) | `SqliteSaver` (sync) | SqliteSaver wraps a sync sqlite3 conn; calling it from async code blocks the event loop per checkpoint write. Use AsyncSqliteSaver always in a FastAPI/asyncio context. |
| `asyncio.Queue` worker | `FastAPI BackgroundTasks` | BackgroundTasks has no queue, no concurrency limit, no status tracking. Runs after response in request cycle. Wrong for multi-minute graph runs. |
| Two SQLite files | Single SQLite file | Mixing LangGraph checkpoint schema with app tables creates version-coupling. LangGraph checkpoint schema is internal and may change. Keep separate: `checkpoints.db` (LangGraph-owned) and `jobs.db` (app-owned). |

**Installation (Phase 1 deps only):**

```bash
uv venv .venv --python 3.12
source .venv/bin/activate

uv pip install \
  "langgraph==1.2.6" \
  "langgraph-checkpoint-sqlite==3.1.0" \
  "fastapi==0.137.2" \
  "uvicorn[standard]==0.49.0" \
  "aiosqlite>=0.19" \
  "pydantic>=2.7" \
  "python-dotenv>=1.0"

uv pip install pytest pytest-asyncio ruff
```

---

## Architecture Patterns

### Recommended Project Structure

```
orchestrator/
├── orchestrator/
│   ├── __init__.py
│   ├── main.py               # FastAPI app + lifespan (owns checkpointer, worker task)
│   ├── api/
│   │   ├── __init__.py
│   │   ├── routes.py         # POST /goals, GET /jobs/{id}/status, GET /jobs/{id}/result,
│   │   │                     # DELETE /jobs/{id}, GET /health
│   │   └── models.py         # GoalRequest, JobStatusResponse, JobResultResponse Pydantic models
│   ├── graph/
│   │   ├── __init__.py
│   │   ├── state.py          # OrchestratorState TypedDict (the REAL schema, not placeholder)
│   │   └── stub_graph.py     # build_stub_graph() → StateGraph; nodes sleep+transition
│   ├── persistence/
│   │   ├── __init__.py
│   │   └── job_store.py      # aiosqlite CRUD for jobs.db
│   └── worker/
│       ├── __init__.py
│       └── runner.py         # asyncio.Queue + worker coroutine; status transitions
├── tests/
│   ├── test_job_store.py     # aiosqlite CRUD (pytest-asyncio)
│   ├── test_stub_graph.py    # graph.ainvoke with AsyncSqliteSaver; checkpoint survives restart
│   └── test_api.py           # FastAPI TestClient: submit → poll → done
├── logs/                     # per-job log files land here
├── data/                     # checkpoints.db and jobs.db land here
├── pyproject.toml
└── .env                      # DATA_DIR=./data LOG_DIR=./logs
```

### Pattern 1: AsyncSqliteSaver Lifespan Wiring (CRITICAL)

**What:** `AsyncSqliteSaver.from_conn_string()` is an `@asynccontextmanager`. The FastAPI lifespan `yield` MUST be inside the `async with` block. The compiled graph (with checkpointer baked in) is stored in `app.state.graph` and reused across all requests.

**Why critical:** Placing `yield` outside the `async with` closes the aiosqlite connection before any requests are handled. All `graph.ainvoke()` calls fail with connection errors.

**Verified pattern (from GitHub source `aio.py` and official API reference):**

```python
# orchestrator/main.py
import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from orchestrator.graph.stub_graph import build_stub_graph
from orchestrator.worker.runner import worker_loop
from orchestrator.persistence.job_store import init_jobs_db

DATA_DIR = "data"

@asynccontextmanager
async def lifespan(app: FastAPI):
    # 1. Init jobs.db schema (synchronous, runs once)
    await init_jobs_db(f"{DATA_DIR}/jobs.db")

    # 2. Open AsyncSqliteSaver — yield MUST be inside this block
    async with AsyncSqliteSaver.from_conn_string(f"{DATA_DIR}/checkpoints.db") as saver:
        # 3. Compile graph once; checkpointer baked in
        graph = build_stub_graph().compile(checkpointer=saver)
        app.state.graph = graph

        # 4. Start single worker coroutine
        queue: asyncio.Queue = asyncio.Queue()
        app.state.job_queue = queue
        worker_task = asyncio.create_task(worker_loop(queue, graph, f"{DATA_DIR}/jobs.db"))

        yield  # <-- INSIDE the async with block; connection stays open

        # 5. Shutdown: cancel worker, let checkpointer close naturally
        worker_task.cancel()
        try:
            await worker_task
        except asyncio.CancelledError:
            pass
    # async with exits here, aiosqlite connection closes cleanly

app = FastAPI(lifespan=lifespan)
```

**Note on `setup()`:** `AsyncSqliteSaver.setup()` is called automatically by the first async operation (`aput`, `aget_tuple`, etc.) via an `is_setup` guard. You do NOT need to call it explicitly — but calling it at startup is harmless and ensures the schema exists before any graph invocation. For clarity, call `await saver.setup()` after opening the context manager.

### Pattern 2: OrchestratorState TypedDict (ORCH-04)

**What:** Use a flat TypedDict with typed string fields — no `messages` list accumulator. Each phase's output is a single string field. The `event_log` uses `Annotated[list[str], operator.add]` for an append-only audit trail. Define the REAL schema now so Phases 2–3 slot in without renaming fields.

**Source:** ARCHITECTURE.md §Graph State Schema + PITFALLS.md §Pitfall 4 (state bloat)

```python
# orchestrator/graph/state.py
from typing import TypedDict, Annotated, Optional
import operator

class OrchestratorState(TypedDict):
    # Set at job submission, never mutated by nodes
    goal: str
    job_id: str

    # Populated by research_node (Phase 2); stub returns placeholder string
    research_findings: Optional[str]

    # Populated by plan_node (Phase 2); stub returns placeholder string
    plan: Optional[str]

    # Populated by execute_node (Phase 3); stub returns placeholder string
    execution_result: Optional[str]

    # "success" | "partial" | "failed" — written by execute_node (Phase 3)
    execution_status: Optional[str]

    # Append-only log; Annotated[..., operator.add] means LangGraph appends, never replaces
    # Each node appends one entry; never accumulates conversation messages
    event_log: Annotated[list[str], operator.add]
```

**Design rules:**
- `goal` and `job_id` are immutable — no node writes them.
- `research_findings`, `plan`, `execution_result` are `Optional[str]` — `None` at entry, each written exactly once.
- Each node returns only the keys it changes; LangGraph merges partial dicts.
- The stub nodes write placeholder strings so checkpoint/resume tests work with real data shapes.

### Pattern 3: Stub Graph (proves checkpoint without LLM)

**What:** Three stub nodes (`research_stub`, `plan_stub`, `execute_stub`) each `asyncio.sleep` briefly and write their status to `jobs.db` at entry, then return partial state. The node names and graph edges are the real ones — Phases 2–3 replace implementations only.

```python
# orchestrator/graph/stub_graph.py
import asyncio
from datetime import datetime, timezone
from langgraph.graph import StateGraph, END
from orchestrator.graph.state import OrchestratorState

# Note: nodes receive only 'state' dict — they cannot directly await aiosqlite
# Status writes happen via astream in the worker (see Pattern 5), OR
# nodes write status synchronously to jobs.db using a sync sqlite3 call.
# Recommended: worker drives status transitions from astream events.

async def research_stub(state: OrchestratorState) -> dict:
    """Stub: simulates a ~1s research call, returns placeholder findings."""
    await asyncio.sleep(1.0)  # simulate LLM latency
    return {
        "research_findings": "STUB: research complete",
        "event_log": [f"{datetime.now(timezone.utc).isoformat()} research_stub: complete"],
    }

async def plan_stub(state: OrchestratorState) -> dict:
    """Stub: simulates a ~1s plan call, returns placeholder plan."""
    await asyncio.sleep(1.0)
    return {
        "plan": "STUB: plan step 1, step 2, step 3",
        "event_log": [f"{datetime.now(timezone.utc).isoformat()} plan_stub: complete"],
    }

async def execute_stub(state: OrchestratorState) -> dict:
    """Stub: simulates a ~2s execute call. In Phase 3 replaced by OpenHands adapter."""
    await asyncio.sleep(2.0)
    return {
        "execution_result": "STUB: execution complete",
        "execution_status": "success",
        "event_log": [f"{datetime.now(timezone.utc).isoformat()} execute_stub: complete"],
    }

def build_stub_graph() -> StateGraph:
    builder = StateGraph(OrchestratorState)
    builder.add_node("research", research_stub)
    builder.add_node("plan", plan_stub)
    builder.add_node("execute", execute_stub)
    builder.set_entry_point("research")
    builder.add_edge("research", "plan")
    builder.add_edge("plan", "execute")
    builder.add_edge("execute", END)
    return builder
# Compiled in lifespan: graph = build_stub_graph().compile(checkpointer=saver)
```

### Pattern 4: asyncio.Queue Worker + Status Transitions

**What:** A single worker coroutine drains an `asyncio.Queue`. The worker is the ONLY code that calls `graph.astream()`/`graph.ainvoke()`. Status is written to `jobs.db` at job pick-up, per-node transition (via astream events), and at completion/failure. The HTTP handler only enqueues and reads.

**Why `astream` over `ainvoke` for the worker:** `astream` with `stream_mode="updates"` yields a dict per node as it completes. Each yielded dict contains the node name as the key. This is how the worker detects which node just ran and writes the next status to `jobs.db` — no external callbacks required.

```python
# orchestrator/worker/runner.py
import asyncio
import logging
from orchestrator.persistence.job_store import JobStore

# Status mapping: after node N completes, set status to the NEXT node's "in progress" label
_NODE_COMPLETE_TO_STATUS = {
    "research": "PLANNING",    # research done → now planning
    "plan":     "EXECUTING",   # plan done → now executing
    "execute":  "DONE",        # execute done → job complete
}

async def worker_loop(queue: asyncio.Queue, graph, jobs_db_path: str):
    store = JobStore(jobs_db_path)
    while True:
        job_id, goal = await queue.get()
        job_logger = _get_job_logger(job_id)
        try:
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
            }

            # Use astream to get per-node completion events
            async for chunk in graph.astream(initial_state, config=config, stream_mode="updates"):
                # chunk is {node_name: partial_state_dict}
                for node_name in chunk:
                    next_status = _NODE_COMPLETE_TO_STATUS.get(node_name)
                    if next_status == "DONE":
                        # Extract result from final state
                        final_state = await graph.aget_state(config)
                        result = final_state.values.get("execution_result")
                        await store.set_complete(job_id, result)
                        job_logger.info(f"DONE: {node_name} complete")
                    elif next_status:
                        await store.set_status(job_id, next_status)
                        job_logger.info(f"{next_status} started (after {node_name})")

        except asyncio.CancelledError:
            # Worker task was cancelled (service shutdown) — let it propagate
            await store.set_status(job_id, "PENDING")  # re-queued on next start
            raise
        except Exception as exc:
            await store.set_failed(job_id, str(exc))
            job_logger.exception(f"FAILED: {exc}")
        finally:
            queue.task_done()

def _get_job_logger(job_id: str) -> logging.Logger:
    """Per-job logger writing to logs/{job_id}.log. Does not clobber uvicorn root logger."""
    import os
    logger = logging.getLogger(f"job.{job_id}")
    if not logger.handlers:
        log_path = os.path.join("logs", f"{job_id}.log")
        os.makedirs("logs", exist_ok=True)
        fh = logging.FileHandler(log_path)
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(fh)
        logger.propagate = False  # CRITICAL: do not propagate to uvicorn root logger
        logger.setLevel(logging.DEBUG)
    return logger
```

### Pattern 5: Job Status Surfaced via jobs.db (not checkpoint)

**Decision (resolves open question 3):** Write job status to `jobs.db` from the worker, not derived from checkpoint state. The HTTP status endpoint reads `jobs.db` — a simple `SELECT status FROM jobs WHERE id=?`. This is fast, decoupled, and needs no checkpoint access.

**Rationale:** Deriving status from the checkpoint requires `graph.aget_state()` per request and understanding LangGraph's internal state shape (which can change across versions). Writing explicit status to `jobs.db` from the worker is simpler, faster, and more reliable.

**jobs.db Schema (PERSIST-02):**

```sql
CREATE TABLE IF NOT EXISTS jobs (
    id          TEXT PRIMARY KEY,
    goal        TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'PENDING',
    result      TEXT,         -- JSON or plain string from execution_result field
    error       TEXT,
    created_at  TEXT NOT NULL,  -- ISO 8601 UTC
    updated_at  TEXT NOT NULL   -- ISO 8601 UTC, updated on every status transition
);
```

**Status state machine:**
```
POST /goals submitted    → PENDING
Worker picks up          → RESEARCHING
research node complete   → PLANNING
plan node complete       → EXECUTING
execute node complete    → DONE (result written)
Any exception            → FAILED (error written)
DELETE /jobs/{id}        → CANCELLED (see cancel pattern below)
```

**aiosqlite access pattern:** Enable WAL mode at connection open for concurrent readers (HTTP handlers) and single writer (worker). The worker is the only writer.

```python
# orchestrator/persistence/job_store.py
import aiosqlite
from datetime import datetime, timezone

class JobStore:
    def __init__(self, db_path: str):
        self.db_path = db_path

    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    async def _connect(self):
        conn = await aiosqlite.connect(self.db_path)
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA synchronous=NORMAL")  # faster than FULL; safe with WAL
        return conn

    async def create_job(self, job_id: str, goal: str) -> None:
        now = self._now()
        async with await self._connect() as conn:
            await conn.execute(
                "INSERT INTO jobs (id, goal, status, created_at, updated_at) VALUES (?,?,?,?,?)",
                (job_id, goal, "PENDING", now, now)
            )
            await conn.commit()

    async def set_status(self, job_id: str, status: str) -> None:
        async with await self._connect() as conn:
            await conn.execute(
                "UPDATE jobs SET status=?, updated_at=? WHERE id=?",
                (status, self._now(), job_id)
            )
            await conn.commit()

    async def set_complete(self, job_id: str, result: str | None) -> None:
        async with await self._connect() as conn:
            await conn.execute(
                "UPDATE jobs SET status='DONE', result=?, updated_at=? WHERE id=?",
                (result, self._now(), job_id)
            )
            await conn.commit()

    async def set_failed(self, job_id: str, error: str) -> None:
        async with await self._connect() as conn:
            await conn.execute(
                "UPDATE jobs SET status='FAILED', error=?, updated_at=? WHERE id=?",
                (error, self._now(), job_id)
            )
            await conn.commit()

    async def get_job(self, job_id: str) -> dict | None:
        async with await self._connect() as conn:
            conn.row_factory = aiosqlite.Row
            async with conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)) as cur:
                row = await cur.fetchone()
                return dict(row) if row else None

    async def get_jobs_by_status(self, status: str) -> list[dict]:
        async with await self._connect() as conn:
            conn.row_factory = aiosqlite.Row
            async with conn.execute("SELECT * FROM jobs WHERE status=?", (status,)) as cur:
                return [dict(r) for r in await cur.fetchall()]
```

**Note on connection per-operation:** Opening a new aiosqlite connection per operation is slightly less efficient than a persistent connection pool but is simpler and avoids shared-connection lifecycle bugs. For a single-operator service processing one job at a time, this is acceptable. If write contention becomes a concern, cache a single connection per coroutine context.

### Pattern 6: Cancel Pattern (API-04)

**Decision (resolves open question 6):** Cancel is app-level only. The worker tracks the running graph's asyncio task. A cancel request sets a cancellation flag + job status, and the worker task is cancelled after the current `astream` chunk completes (not mid-node).

**LangGraph checkpoint-consistency caveat:** Cancelling `graph.astream()` / `graph.ainvoke()` mid-execution does NOT guarantee the in-flight node's state is checkpointed. The checkpoint reflects the last **completed** node. A cancel mid-research means the checkpoint has no research data; a cancel mid-execute means research and plan are checkpointed but execute is not. This is expected behavior and must be documented to users.

**Implementation approach:**

```python
# In runner.py — track current graph task per job
_cancel_flags: dict[str, asyncio.Event] = {}  # job_id → cancellation event

async def worker_loop(queue, graph, jobs_db_path):
    store = JobStore(jobs_db_path)
    while True:
        job_id, goal = await queue.get()
        cancel_event = asyncio.Event()
        _cancel_flags[job_id] = cancel_event
        try:
            # ... astream loop with cancel check ...
            async for chunk in graph.astream(initial_state, config=config, stream_mode="updates"):
                if cancel_event.is_set():
                    await store.set_status(job_id, "CANCELLED")
                    break
                for node_name in chunk:
                    # ... status updates ...
        finally:
            _cancel_flags.pop(job_id, None)
            queue.task_done()

def request_cancel(job_id: str) -> bool:
    """Called from DELETE /jobs/{id} route. Returns True if job was cancellable."""
    event = _cancel_flags.get(job_id)
    if event:
        event.set()
        return True
    return False
```

**For queued (not yet running) jobs:** Update `jobs.db` status to CANCELLED immediately. The worker checks status at pick-up and skips already-cancelled jobs.

### Pattern 7: Per-Run Log File (OBS-01)

**What:** Each job gets a dedicated `FileHandler` on a child logger `logging.getLogger(f"job.{job_id}")`. The handler is added once and `propagate=False` prevents log records from reaching uvicorn's root logger (which would duplicate them in stdout).

**Why `propagate=False` is critical:** uvicorn installs handlers on the root logger. Without `propagate=False`, every log line from `logger.info(...)` in job code would appear both in the per-job file AND in uvicorn stdout, creating duplicate entries and potentially leaking job content to shared logs.

**Log file location:** `logs/{job_id}.log` — created at job start by the worker, one file per job.

**Format:** `%(asctime)s %(levelname)s %(message)s` — ISO 8601 timestamps match the `updated_at` field in jobs.db for correlation.

```python
def _get_job_logger(job_id: str) -> logging.Logger:
    logger = logging.getLogger(f"job.{job_id}")
    if not logger.handlers:  # idempotent
        os.makedirs("logs", exist_ok=True)
        fh = logging.FileHandler(f"logs/{job_id}.log")
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(fh)
        logger.propagate = False  # do not send to uvicorn root logger
        logger.setLevel(logging.DEBUG)
    return logger
```

### Pattern 8: Restart-Resume Minimal Proof (resolves open question 4)

**Scope for Phase 1:** Phase 1 must PROVE that checkpoints survive a process kill+restart. Full recovery-on-startup (re-enqueue interrupted jobs) is PERSIST-03 scope (Phase 5). The minimal Phase 1 proof is:

1. Submit a job, let it complete → `checkpoints.db` has a checkpoint for that `thread_id`.
2. Kill the server process (`kill -9` or `Ctrl-C`).
3. Restart the server.
4. `GET /jobs/{id}/status` returns DONE (from `jobs.db` — which survived).
5. `GET /jobs/{id}/result` returns the result (from `jobs.db`).
6. BONUS: verify the checkpoint is in `checkpoints.db` via `sqlite3 data/checkpoints.db "SELECT thread_id, channel_name FROM checkpoints"`.

**Resume mechanics (for partial jobs — Phase 5 pattern, but Phase 1 must understand it):** When a job was in-flight at crash, its `jobs.db` status is `RESEARCHING` or `PLANNING` or `EXECUTING`. Phase 5 re-enqueues these. When the worker picks up the same `job_id` with the same `thread_id`, it calls:

```python
# Resume from checkpoint: pass None (or empty state dict) as input
# LangGraph detects the existing checkpoint and resumes from the last completed node
config = {"configurable": {"thread_id": job_id}}
async for chunk in graph.astream(None, config=config, stream_mode="updates"):
    ...
```

**Passing `None` as input:** When resuming a thread that already has a checkpoint, LangGraph loads the checkpointed state and continues from the next node. Passing `None` as the first argument signals "no new input — resume from checkpoint." Passing the initial state dict again would re-run from the beginning of the graph (not desired for resume). **EMPIRICAL VERIFICATION NEEDED** during Phase 1 test execution — the API docs confirm this behavior but it should be tested with the actual `langgraph==1.2.6` library.

### Pattern 9: REST Endpoint Shapes

```python
# orchestrator/api/routes.py (shape only)
from fastapi import APIRouter, Request, HTTPException
from orchestrator.api.models import GoalRequest, JobStatusResponse

router = APIRouter()

@router.post("/goals", status_code=202)
async def submit_goal(req: GoalRequest, request: Request):
    job_id = str(uuid.uuid4())
    await request.app.state.job_queue.put((job_id, req.goal))
    # Also write to jobs.db so status is visible before worker picks up
    store = JobStore("data/jobs.db")
    await store.create_job(job_id, req.goal)
    return {"job_id": job_id, "status": "PENDING"}

@router.get("/jobs/{job_id}/status")
async def get_status(job_id: str):
    store = JobStore("data/jobs.db")
    job = await store.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return {"job_id": job_id, "status": job["status"]}

@router.get("/jobs/{job_id}/result")
async def get_result(job_id: str):
    store = JobStore("data/jobs.db")
    job = await store.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job["status"] != "DONE":
        raise HTTPException(status_code=409, detail=f"Job status is {job['status']}, not DONE")
    return {"job_id": job_id, "result": job["result"]}

@router.delete("/jobs/{job_id}", status_code=200)
async def cancel_job(job_id: str):
    store = JobStore("data/jobs.db")
    job = await store.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job["status"] in ("DONE", "FAILED", "CANCELLED"):
        return {"job_id": job_id, "status": job["status"], "note": "already terminal"}
    # Signal cancellation to running worker
    from orchestrator.worker.runner import request_cancel
    cancelled = request_cancel(job_id)
    if not cancelled:
        # Job is queued but not running — update status directly
        await store.set_status(job_id, "CANCELLED")
    return {"job_id": job_id, "status": "CANCELLED",
            "caveat": "checkpoint reflects last completed node, not mid-node state"}

@router.get("/health")
async def health(request: Request):
    # Verify SQLite liveness by running a no-op query on jobs.db
    import aiosqlite
    try:
        async with aiosqlite.connect("data/jobs.db") as conn:
            await conn.execute("SELECT 1")
        return {"status": "ok", "sqlite": "reachable"}
    except Exception as exc:
        return {"status": "degraded", "sqlite": str(exc)}, 503
```

### Anti-Patterns to Avoid

- **`yield` outside `async with AsyncSqliteSaver.from_conn_string(...):`** — closes the SQLite connection before any request is served. All graph calls fail.
- **`MemorySaver` in any non-test code** — state lost on process restart; defeats PERSIST-01/02.
- **Storing `messages` list in OrchestratorState** — use typed string fields instead; avoids state bloat pitfall (PITFALLS.md §4).
- **Calling `store.create_job()` AFTER `queue.put()`** — the worker may pick up the job before the row exists, causing a missing status write. Always write to `jobs.db` first, then enqueue.
- **`logger.propagate = True` (default) for job loggers** — duplicates logs in uvicorn stdout and may leak sensitive content.
- **Opening a new `aiosqlite.connect()` without WAL mode** — default journal mode causes writer-reader contention; always set `PRAGMA journal_mode=WAL` at connection open.
- **`graph.compile()` inside a request handler** — expensive; compile once in lifespan, reuse via `app.state.graph`.

---

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| SQLite checkpoint persistence | Custom serialization of graph state | `AsyncSqliteSaver` from `langgraph-checkpoint-sqlite` | Thread-safe, handles schema migrations internally, resume semantics built-in |
| Job queue with status tracking | Custom queue + shared dict | `asyncio.Queue` + `aiosqlite` jobs table | asyncio.Queue is stdlib; jobs.db provides durability across restarts |
| Async SQLite access | `sqlite3` sync calls in `async def` | `aiosqlite` | Sync sqlite3 in async context blocks the event loop; aiosqlite runs SQLite in a background thread transparently |
| Per-node status callbacks | Polling `graph.aget_state()` per request | `graph.astream(stream_mode="updates")` in worker | astream yields per-node dicts; cleaner than polling and works in the worker context without extra HTTP round-trips |
| Request/response validation | Manual JSON parsing | Pydantic v2 BaseModel via FastAPI | FastAPI+Pydantic handles validation, serialization, OpenAPI docs automatically |

**Key insight:** LangGraph's checkpoint machinery is the hardest part to get right (connection lifecycle, schema, resume semantics). Don't replicate it. AsyncSqliteSaver + the lifespan pattern in Pattern 1 handles all of it.

---

## Common Pitfalls

### Pitfall 1: yield Outside async with Saver Block (CRITICAL)

**What goes wrong:** The `AsyncSqliteSaver` context closes its aiosqlite connection when the `async with` block exits. If `yield` is placed outside/after the block, the connection is closed before FastAPI begins serving requests. All `graph.ainvoke()` / `graph.astream()` calls raise `aiosqlite.ProgrammingError: Cannot operate on a closed database`.

**Why it happens:** `from_conn_string()` is an `asynccontextmanager` — the underlying `aiosqlite.connect()` is wrapped in `async with aiosqlite.connect(...) as conn: yield cls(conn)`. When the `async with AsyncSqliteSaver.from_conn_string(...)` block exits, the connection closes.

**How to avoid:** Lifespan `yield` MUST be inside the `async with` block (Pattern 1 above).

**Warning signs:** `ProgrammingError: Cannot operate on a closed database` on first graph invocation.

### Pitfall 2: create_job After queue.put

**What goes wrong:** Worker picks up `(job_id, goal)` from the queue before the `jobs.db` row exists. `store.set_status(job_id, "RESEARCHING")` fires an UPDATE on a nonexistent row — silently updates 0 rows. `GET /jobs/{id}/status` returns 404.

**Why it happens:** `asyncio.Queue.put()` is synchronous-equivalent (non-blocking when queue has room). The worker may run immediately in the next event loop iteration before `store.create_job()` is awaited.

**How to avoid:** Always call `await store.create_job(job_id, goal)` BEFORE `await queue.put((job_id, goal))` in the POST handler.

### Pitfall 3: aiosqlite Connection Without WAL Mode

**What goes wrong:** Default SQLite journal mode (`DELETE` / rollback journal) allows only one concurrent reader+writer. FastAPI HTTP handlers (readers) and the worker (writer) contend for the write lock, causing `sqlite3.OperationalError: database is locked` under load.

**Why it happens:** SQLite default mode serializes all access including reads during writes.

**How to avoid:** Set `PRAGMA journal_mode=WAL` at every connection open. WAL allows concurrent readers with a single writer without locking contention.

### Pitfall 4: Missing `propagate = False` on Job Loggers

**What goes wrong:** Log records from `logger.info(...)` in job code propagate to uvicorn's root logger handlers, appearing twice: once in `logs/{job_id}.log` and again in uvicorn stdout. If job content (goal text, research results) is logged, it appears in the shared service log.

**Why it happens:** Python logging propagates to ancestor loggers by default. Uvicorn installs a `StreamHandler` on the root logger.

**How to avoid:** Set `logger.propagate = False` immediately after creating the `FileHandler` for the job logger.

### Pitfall 5: `graph.compile()` Called Per Request

**What goes wrong:** Compiling a StateGraph is relatively expensive (graph validation, type checking). Compiling per request creates noticeable latency and re-creates the aiosqlite connection chain unnecessarily.

**How to avoid:** Compile exactly once in the lifespan function; store in `app.state.graph`; retrieve via `request.app.state.graph` in routes and worker.

### Pitfall 6: Passing Initial State on Resume (instead of None)

**What goes wrong:** When a crashed job is re-enqueued and the worker calls `graph.astream(initial_state, config=config)` (same as a new job), LangGraph detects an existing checkpoint and may merge the new initial_state with the checkpointed state, causing duplicate entries in `event_log` or overwriting partial results.

**Why it happens:** LangGraph merges input with checkpoint state using registered reducers. For `event_log` with `operator.add` reducer, passing `event_log: []` merges (appends nothing) — OK. But passing `goal` and `job_id` values re-merges them — also OK since they're the same. The safe pattern is to pass `None` as input for resume.

**How to avoid:** For resume invocations, pass `None` as input:
```python
async for chunk in graph.astream(None, config=config, stream_mode="updates"):
```
**NEEDS EMPIRICAL VERIFICATION with langgraph==1.2.6** — test both patterns (None vs initial_state) during Phase 1 execution.

---

## Code Examples

### Minimal Init for jobs.db

```python
# orchestrator/persistence/job_store.py (init function)
# Source: verified aiosqlite pattern from official aiosqlite PyPI docs

async def init_jobs_db(db_path: str) -> None:
    """Create jobs.db schema on first run. Idempotent (IF NOT EXISTS)."""
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                id          TEXT PRIMARY KEY,
                goal        TEXT NOT NULL,
                status      TEXT NOT NULL DEFAULT 'PENDING',
                result      TEXT,
                error       TEXT,
                created_at  TEXT NOT NULL,
                updated_at  TEXT NOT NULL
            )
        """)
        await conn.commit()
```

### Complete Lifespan (reference shape)

```python
# Source: AsyncSqliteSaver.from_conn_string() is @asynccontextmanager per GitHub source
# langgraph/libs/checkpoint-sqlite/langgraph/checkpoint/sqlite/aio.py

@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_jobs_db("data/jobs.db")

    async with AsyncSqliteSaver.from_conn_string("data/checkpoints.db") as saver:
        # Optional explicit setup — called automatically on first op, but explicit is clearer
        await saver.setup()

        graph = build_stub_graph().compile(checkpointer=saver)
        app.state.graph = graph

        job_queue: asyncio.Queue = asyncio.Queue()
        app.state.job_queue = job_queue

        worker_task = asyncio.create_task(
            worker_loop(job_queue, graph, "data/jobs.db")
        )
        yield  # INSIDE async with — connection stays alive

        worker_task.cancel()
        try:
            await worker_task
        except asyncio.CancelledError:
            pass
    # Connection closes here (after yield), on service shutdown
```

### TestClient Pattern for Integration Tests

```python
# tests/test_api.py
# Source: FastAPI official docs / pytest-asyncio patterns

import pytest
from fastapi.testclient import TestClient
from orchestrator.main import app

# TestClient handles lifespan automatically (runs startup/shutdown)
def test_submit_goal_returns_202():
    with TestClient(app) as client:
        resp = client.post("/goals", json={"goal": "test goal"})
        assert resp.status_code == 202
        data = resp.json()
        assert "job_id" in data
        assert data["status"] == "PENDING"

def test_health_returns_200():
    with TestClient(app) as client:
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["sqlite"] == "reachable"
```

### Async Graph Test with Real AsyncSqliteSaver

```python
# tests/test_stub_graph.py
import pytest
import asyncio
import tempfile
import os
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from orchestrator.graph.stub_graph import build_stub_graph

@pytest.mark.asyncio
async def test_stub_graph_completes_and_checkpoints():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    try:
        async with AsyncSqliteSaver.from_conn_string(db_path) as saver:
            graph = build_stub_graph().compile(checkpointer=saver)
            config = {"configurable": {"thread_id": "test-job-001"}}
            initial = {
                "goal": "test", "job_id": "test-job-001",
                "research_findings": None, "plan": None,
                "execution_result": None, "execution_status": None,
                "event_log": [],
            }
            result = await graph.ainvoke(initial, config=config)

        # Re-open DB to verify checkpoint persists
        async with AsyncSqliteSaver.from_conn_string(db_path) as saver2:
            graph2 = build_stub_graph().compile(checkpointer=saver2)
            state = await graph2.aget_state(config)
            assert state.values["execution_result"] == "STUB: execution complete"
            assert len(state.values["event_log"]) == 3  # one per stub node
    finally:
        os.unlink(db_path)
```

---

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| `MemorySaver` (demo default) | `AsyncSqliteSaver` (production single-machine) | langgraph-checkpoint-sqlite 1.0+ | MemorySaver loses state on restart; AsyncSqliteSaver is zero-config persistent |
| `SqliteSaver` (sync) | `AsyncSqliteSaver` (async) | aio.py added to checkpoint-sqlite package | Sync saver blocks asyncio event loop on checkpoint writes; always use async variant in FastAPI |
| Manual checkpoint schema | `AsyncSqliteSaver.setup()` / auto-setup | Built into the library | setup() creates tables automatically; no need to manually CREATE TABLE for checkpoint schema |
| `on_event` startup decorator | `@asynccontextmanager` lifespan | FastAPI 0.93+ | Lifespan is the canonical way; startup/shutdown decorators are deprecated in newer FastAPI |
| `FastAPI BackgroundTasks` for long jobs | `asyncio.Queue` + lifespan worker | N/A | BackgroundTasks is not a queue; has no concurrency control, status tracking, or queue semantics |

**Deprecated/outdated:**
- `SqliteSaver` (sync variant): Not deprecated but should never be used in an asyncio context. Use `AsyncSqliteSaver`.
- `@app.on_event("startup")` / `@app.on_event("shutdown")`: Deprecated in FastAPI; use lifespan.
- `MemorySaver`: Still available for tests; never for production services.

---

## Open Questions

1. **`None` vs initial_state for resume invocation**
   - What we know: LangGraph resumes from the last checkpoint when `thread_id` matches an existing checkpoint. Passing `None` as input is the documented pattern for interrupt-based resume.
   - What's unclear: For a non-interrupt crash-resume (Phase 1's restart proof), whether `None` or the initial state dict is the correct first argument to `graph.astream()`.
   - Recommendation: Test BOTH during Phase 1 test execution. Expected: `None` resumes without re-running completed nodes; initial_state may re-merge and re-run from beginning.
   - Confidence: MEDIUM — confirmed by community sources but needs empirical verification with `langgraph==1.2.6`.

2. **`astream` `stream_mode="updates"` chunk key format**
   - What we know: `stream_mode="updates"` yields dicts where keys are node names that just ran.
   - What's unclear: Whether the key is exactly the string name passed to `builder.add_node()`, or wrapped in a namespace. 
   - Recommendation: Print raw chunks in first integration test to confirm: `async for chunk in graph.astream(...): print(chunk)`. Expected: `{"research": {...partial state...}}`.
   - Confidence: HIGH based on LangGraph documentation and community examples, but verify with the exact library version.

3. **`_cancel_flags` dict concurrency safety**
   - What we know: In a `uvicorn --workers 1` deployment, all code runs in one process and one asyncio event loop. The `_cancel_flags` dict is accessed from HTTP handlers (via `request_cancel()`) and the worker coroutine. In a single event loop, dict access is effectively single-threaded.
   - What's unclear: If multiple workers (e.g., `--workers 2`) were used, the dict would not be shared between processes. The design assumes `--workers 1`.
   - Recommendation: Document the `--workers 1` constraint in the uvicorn startup command. This is already in STACK.md.

---

## Sources

### Primary (HIGH confidence)

- `langgraph/libs/checkpoint-sqlite/langgraph/checkpoint/sqlite/aio.py` on GitHub main — `from_conn_string` is `@asynccontextmanager`, `__init__` signature, `is_setup` guard, no `aclose()` method. **Critical for Pattern 1 yield-inside-block rule.**
- [AsyncSqliteSaver API reference — LangChain Reference](https://reference.langchain.com/python/langgraph.checkpoint.sqlite/aio/AsyncSqliteSaver) — `from_conn_string`, `setup()`, async context manager semantics.
- `langgraph==1.2.6` on PyPI (2026-06-19) — version confirmed.
- `langgraph-checkpoint-sqlite==3.1.0` on PyPI (2026-06-19) — version confirmed; CVE-2025-67644 patched in 3.0.1.
- `fastapi==0.137.2` / `uvicorn[standard]==0.49.0` on PyPI — versions confirmed.
- STACK.md / ARCHITECTURE.md / PITFALLS.md in this project — authoritative prior domain research.

### Secondary (MEDIUM confidence)

- [Simple LangGraph + AsyncSqliteSaver + FastAPI (Medium, @devwithll)](https://medium.com/@devwithll/simple-langgraph-implementation-with-memory-asyncsqlitesaver-checkpointer-fastapi-54f4e4879a2e) — lifespan pattern, graph stored in app.state. **Confirmed yield-inside-block as the working pattern** (though article description is slightly ambiguous).
- [LangGraph State Management: Checkpoints, Thread State, and Failure Recovery (eastondev.com, 2026)](https://eastondev.com/blog/en/posts/ai/20260424-langgraph-agent-architecture/) — ainvoke resume with same thread_id, skip completed nodes.
- [SQLite WAL mode docs (sqlite.org)](https://sqlite.org/wal.html) — concurrent readers, single writer, WAL vs journal mode.
- [FastAPI lifespan events (fastapi.tiangolo.com)](https://fastapi.tiangolo.com/advanced/events/) — yield placement in lifespan, asynccontextmanager pattern.
- [LangGraph streaming (docs.langchain.com)](https://docs.langchain.com/oss/python/langgraph/streaming) — `stream_mode="updates"` yields per-node dicts.
- [Uvicorn adds handler to root logger (GitHub issue #630)](https://github.com/Kludex/uvicorn/issues/630) — `propagate=False` pattern for per-job loggers.

### Tertiary (LOW confidence — verify empirically during Phase 1)

- Community sources on `None` as input for resume: confirmed for interrupt-based flows; extrapolated to crash-resume. **Verify with langgraph==1.2.6**.
- `stream_mode="updates"` chunk key is exactly the node name string: consistent across multiple examples but not explicitly confirmed in official API spec for 1.2.6.

---

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH — all versions verified against PyPI 2026-06-19
- AsyncSqliteSaver lifecycle: HIGH — confirmed from GitHub source code (`aio.py`)
- asyncio.Queue worker pattern: HIGH — well-documented stdlib pattern with FastAPI lifespan
- aiosqlite jobs.db pattern: HIGH — standard aiosqlite CRUD pattern
- Per-node status via astream: HIGH — documented LangGraph streaming behavior
- Cancel pattern: MEDIUM — asyncio.Event based; LangGraph cancel semantics confirmed from GitHub issues
- Resume input (None vs initial_state): MEDIUM — needs empirical verification
- `stream_mode="updates"` chunk key format: HIGH — confirmed from multiple examples, verify in first test

**Research date:** 2026-06-19
**Valid until:** 2026-07-19 (LangGraph 1.x is stable; AsyncSqliteSaver API is unlikely to change in 30 days)
