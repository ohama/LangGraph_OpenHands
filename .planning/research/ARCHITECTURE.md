# Architecture Research

**Domain:** Local LangGraph + OpenHands orchestrator (Research → Plan → Execute) — FastAPI/launchd service, macOS
**Researched:** 2026-06-19
**Confidence:** HIGH

---

## Standard Architecture

### System Overview

```
┌──────────────────────────────────────────────────────────────────────┐
│  launchd (macOS)                                                      │
│  ┌─────────────────────────┐   ┌───────────────────────────────────┐ │
│  │ com.ohama.qwen122b      │   │ com.ohama.litellm                 │ │
│  │ (mlx_lm.server :8001)   │   │ (litellm proxy :4000)             │ │
│  └─────────────────────────┘   └────────────────┬──────────────────┘ │
│                                                  │ routes to 122b/35b │
│  ┌───────────────────────────────────────────────┼──────────────────┐ │
│  │ com.ohama.orchestrator  (uvicorn :8080)        │                  │ │
│  │                                                │                  │ │
│  │  ┌────────────────────────────────────────────▼──────────────┐   │ │
│  │  │  API Layer (FastAPI)                                       │   │ │
│  │  │  POST /goals → enqueue job, return job_id                 │   │ │
│  │  │  GET  /jobs/{id} → return status + result                 │   │ │
│  │  └───────────────────────────┬───────────────────────────────┘   │ │
│  │                              │ asyncio.Queue                      │ │
│  │  ┌───────────────────────────▼───────────────────────────────┐   │ │
│  │  │  Worker Loop (single asyncio coroutine, lifespan)         │   │ │
│  │  │  drains queue, runs graph.ainvoke per job                 │   │ │
│  │  └───────────────────────────┬───────────────────────────────┘   │ │
│  │                              │                                    │ │
│  │  ┌───────────────────────────▼───────────────────────────────┐   │ │
│  │  │  Orchestration Layer (LangGraph StateGraph)               │   │ │
│  │  │                                                           │   │ │
│  │  │  ┌──────────────┐  ┌──────────────┐  ┌────────────────┐  │   │ │
│  │  │  │ research_node│→ │  plan_node   │→ │ execute_node   │  │   │ │
│  │  │  │ (qwen-122b)  │  │ (qwen-122b)  │  │ (OpenHands SDK │  │   │ │
│  │  │  │              │  │              │  │  + qwen-35b)   │  │   │ │
│  │  │  └──────────────┘  └──────────────┘  └────────────────┘  │   │ │
│  │  │                                                           │   │ │
│  │  │  AsyncSqliteSaver checkpointer (thread_id = job_id)       │   │ │
│  │  └───────────────────────────────────────────────────────────┘   │ │
│  │                                                                   │ │
│  │  ┌────────────────────────────────────────────────────────────┐  │ │
│  │  │  Persistence Layer (SQLite)                                │  │ │
│  │  │  ├── checkpoints.db  (LangGraph AsyncSqliteSaver)          │  │ │
│  │  │  └── jobs.db         (job registry: id/status/result/ts)   │  │ │
│  │  └────────────────────────────────────────────────────────────┘  │ │
│  └───────────────────────────────────────────────────────────────────┘ │
└──────────────────────────────────────────────────────────────────────┘
```

### Component Responsibilities

| Component | Responsibility | Communicates With |
|-----------|---------------|-------------------|
| FastAPI API layer | Accept `POST /goals`, return `job_id`; serve `GET /jobs/{id}` status | asyncio.Queue (enqueue), jobs.db (read status) |
| asyncio.Queue worker | Single coroutine; drains queue; serializes graph runs; updates job registry | LangGraph StateGraph, jobs.db |
| LangGraph StateGraph | Orchestrate Research → Plan → Execute as a linear graph with checkpointed state | research_node, plan_node, execute_node, AsyncSqliteSaver |
| research_node | Call qwen-122b via LiteLLM :4000; produce structured research findings | LiteLLM proxy, StateGraph state |
| plan_node | Call qwen-122b via LiteLLM :4000; produce structured multi-step plan | LiteLLM proxy, StateGraph state |
| execute_node | Invoke OpenHands SDK `Conversation` in a thread; produce execution result | OpenHands SDK (in-process), LiteLLM proxy (qwen-35b) |
| AsyncSqliteSaver | Persist graph state snapshot after every node; enable crash-resume by thread_id | checkpoints.db |
| Job registry (jobs.db) | Track job_id → {status, result, created_at, updated_at}; survive restarts | asyncio.Queue worker, FastAPI API layer |
| LiteLLM proxy (:4000) | Route model name to correct mlx_lm.server port; present single OpenAI-compat endpoint | mlx_lm.server(s), research_node, plan_node, OpenHands SDK LLM |
| launchd | Keep orchestrator, litellm, and mlx services alive; boot in dependency order | OS process manager |

---

## Graph State Schema

The `OrchestratorState` TypedDict flows through the graph. Each node reads what it needs and returns a partial dict that LangGraph merges back into state.

```python
from typing import TypedDict, Annotated, Optional
from operator import add

class ResearchFindings(TypedDict):
    summary: str
    key_facts: list[str]
    sources: list[str]

class PlanStep(TypedDict):
    step_number: int
    action: str
    expected_output: str

class ExecutionResult(TypedDict):
    status: str          # "success" | "partial" | "failed"
    output: str
    files_changed: list[str]
    error: Optional[str]

class OrchestratorState(TypedDict):
    # Set at entry, never mutated
    goal: str
    job_id: str

    # Written by research_node, read by plan_node
    research_findings: Optional[ResearchFindings]

    # Written by plan_node, read by execute_node
    plan: Optional[list[PlanStep]]

    # Written by execute_node
    execution_result: Optional[ExecutionResult]

    # Running log — each node appends, never overwrites
    # Annotated with operator.add means LangGraph appends to this list
    event_log: Annotated[list[str], add]
```

**State flow:**

```
Entry: {goal, job_id}
         ↓
research_node: reads {goal}
               writes {research_findings, event_log += "research complete"}
         ↓
plan_node: reads {goal, research_findings}
           writes {plan, event_log += "plan created"}
         ↓
execute_node: reads {goal, research_findings, plan}
              writes {execution_result, event_log += "execution done"}
         ↓
END: full state in checkpointer; job registry updated to "complete"
```

**Design rules:**
- `goal` and `job_id` are immutable after entry — no node should write them.
- `research_findings`, `plan`, `execution_result` are optional at entry and populated exactly once.
- `event_log` uses the `Annotated[list, add]` reducer so appends accumulate rather than replace.
- Each node returns **only the keys it changed** — LangGraph merges partials automatically.

---

## Recommended Project Structure

```
orchestrator/
├── orchestrator/
│   ├── __init__.py
│   ├── api/
│   │   ├── __init__.py
│   │   ├── routes.py           # FastAPI routers: /goals, /jobs/{id}, /jobs/{id}/state
│   │   └── models.py           # Pydantic: GoalRequest, JobStatus, JobListItem
│   ├── graph/
│   │   ├── __init__.py
│   │   ├── state.py            # OrchestratorState TypedDict + sub-types
│   │   ├── graph.py            # build_graph() → CompiledGraph; compiled once at startup
│   │   ├── nodes/
│   │   │   ├── research.py     # research_node(state) → partial state dict
│   │   │   ├── plan.py         # plan_node(state) → partial state dict
│   │   │   └── execute.py      # execute_node(state) → partial state dict
│   │   └── llm.py              # make_llm(model_name) → ChatOpenAI pointed at LiteLLM :4000
│   ├── execution/
│   │   ├── __init__.py
│   │   └── openhands_adapter.py # run_openhands(goal, plan, workspace) → ExecutionResult
│   ├── persistence/
│   │   ├── __init__.py
│   │   └── job_store.py        # aiosqlite job registry: create/update/get job rows
│   ├── worker/
│   │   ├── __init__.py
│   │   └── runner.py           # asyncio.Queue, worker coroutine, job lifecycle
│   └── main.py                 # FastAPI app + lifespan (starts worker, opens checkpointer)
├── cli/
│   └── submit.py               # CLI: POST /goals + poll /jobs/{id} until done
├── tests/
│   ├── test_graph.py           # Unit test individual nodes with fake LLM
│   ├── test_api.py             # FastAPI TestClient: submit goal, poll status
│   └── test_job_store.py       # aiosqlite job registry CRUD
├── launchd/
│   └── com.ohama.orchestrator.plist
├── pyproject.toml
└── requirements.txt
```

### Structure Rationale

- **`graph/` is self-contained:** The StateGraph and its nodes have no FastAPI dependency. They can be tested standalone with `pytest-asyncio` and a fake LLM.
- **`execution/openhands_adapter.py` is a seam:** All OpenHands SDK calls live here. The rest of the codebase never imports openhands directly. This isolates the blocking/threading concern and makes the Execute node easy to stub in tests.
- **`worker/runner.py` owns the queue:** The FastAPI layer only enqueues job IDs. The worker owns the graph run lifecycle. Separation prevents the HTTP handler from ever awaiting a long-running operation.
- **`persistence/job_store.py` is separate from the LangGraph checkpointer:** The checkpointer stores graph node state; the job store tracks user-visible status (pending/running/complete/failed). They serve different consumers (LangGraph vs REST clients) and should remain independent even though both use SQLite.

---

## Architectural Patterns

### Pattern 1: Lifespan-Owned Worker + asyncio.Queue

**What:** FastAPI's lifespan context manager starts a single worker coroutine on startup that drains an `asyncio.Queue`. HTTP handlers enqueue job descriptors and return `job_id` immediately. The worker processes one job at a time.

**When to use:** Single-operator, single-machine, no concurrency requirement. One goal at a time is the stated design. No Celery, no Redis, no subprocess isolation needed.

**Trade-offs:** Simple, zero extra services. Cannot run goals concurrently without changing the worker model. But for a single-operator autonomous pipeline, serial execution is safer — prevents resource contention on the local GPU.

**Example:**

```python
# main.py
from contextlib import asynccontextmanager
import asyncio
from fastapi import FastAPI

@asynccontextmanager
async def lifespan(app: FastAPI):
    queue = asyncio.Queue()
    app.state.job_queue = queue
    # Start graph + checkpointer
    graph, checkpointer = await build_graph_with_checkpointer()
    app.state.graph = graph
    # Worker runs until shutdown
    worker_task = asyncio.create_task(run_worker(queue, graph))
    yield
    worker_task.cancel()
    await checkpointer.aclose()

app = FastAPI(lifespan=lifespan)
```

```python
# worker/runner.py
async def run_worker(queue: asyncio.Queue, graph):
    while True:
        job_id, goal = await queue.get()
        await job_store.set_status(job_id, "running")
        config = {"configurable": {"thread_id": job_id}}
        try:
            result = await graph.ainvoke(
                {"goal": goal, "job_id": job_id, "event_log": []},
                config=config
            )
            await job_store.set_complete(job_id, result["execution_result"])
        except Exception as exc:
            await job_store.set_failed(job_id, str(exc))
        finally:
            queue.task_done()
```

### Pattern 2: thread_id = job_id for Durable Resume

**What:** Use the job's UUID as the LangGraph `thread_id`. Because the AsyncSqliteSaver checkpoints after every node, a crashed process can resume from the last completed node by reinvoking with the same config.

**When to use:** Always — there is no reason to use a different key. The job registry and the checkpointer are thus keyed identically.

**Trade-offs:** Resume means the interrupted node re-executes in full (including its LLM call). Nodes must be idempotent with respect to their side effects. For Research and Plan this is free — they only write to state. For Execute, the OpenHands run may re-run partially completed work; the adapter should detect this from the plan's step completion tracking.

**Resume on restart:**

```python
# On service restart, the worker drains any jobs in "running" state from jobs.db
# (they were interrupted mid-run) and re-enqueues them.
# graph.ainvoke() with the same thread_id picks up from the last checkpoint.
async def requeue_interrupted_jobs(queue, job_store):
    running_jobs = await job_store.get_by_status("running")
    for job in running_jobs:
        await job_store.set_status(job.id, "pending")
        await queue.put((job.id, job.goal))
```

### Pattern 3: OpenHands Adapter via ThreadPoolExecutor

**What:** `conversation.run()` is synchronous and blocking. Running it directly inside an `async def` execute_node would block the asyncio event loop for the duration of the OpenHands run (minutes). Offload it to a ThreadPoolExecutor so the event loop stays responsive.

**When to use:** Always, for any blocking call inside an async context. OpenHands SDK v1.29.0 has no native async execution path — `run()` blocks its calling thread.

**Trade-offs:** A single worker coroutine calling `run_in_executor` means the event loop is free for FastAPI health checks while OpenHands runs in a background thread. The executor uses a ThreadPoolExecutor with `max_workers=1` — prevents accidentally starting two OpenHands runs if the worker ever becomes parallel.

**Example:**

```python
# execution/openhands_adapter.py
import asyncio
from concurrent.futures import ThreadPoolExecutor
from openhands.sdk import LLM, Conversation
from openhands.tools.preset.default import get_default_agent

_executor = ThreadPoolExecutor(max_workers=1)

def _run_openhands_sync(goal: str, plan: list, workspace: str) -> ExecutionResult:
    """Synchronous — runs in thread. All OpenHands SDK calls here."""
    llm = LLM(
        model="openai/qwen-35b",     # "openai/" prefix for OpenAI-compat endpoint
        api_key="not-needed",
        api_base="http://localhost:4000/v1",
    )
    agent = get_default_agent(llm=llm)
    conversation = Conversation(
        agent=agent,
        workspace=workspace,
        conversation_id=goal[:40],  # used for persistence_dir namespacing
    )
    # Build prompt from plan
    prompt = _plan_to_prompt(goal, plan)
    conversation.send_message(prompt)
    conversation.run()
    return _extract_result(conversation)

async def run_openhands(goal: str, plan: list, workspace: str) -> ExecutionResult:
    """Async wrapper. Returns when OpenHands run completes."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        _executor,
        _run_openhands_sync,
        goal, plan, workspace
    )
```

### Pattern 4: LiteLLM Proxy as Single Model Endpoint

**What:** Both the LangGraph nodes (ChatOpenAI) and the OpenHands SDK LLM are configured to call `http://localhost:4000/v1`. The LiteLLM proxy plist configures the `model_list` to map `qwen-122b` → mlx port 8001 and `qwen-35b` → mlx port 8000 (or whichever port). The orchestrator never hardcodes mlx port numbers.

**When to use:** Always — this is the existing pattern from the blueCode project and the named LiteLLM launchd agent already exists.

**Trade-offs:** Single point of failure (LiteLLM proxy). Mitigated by KeepAlive in launchd plist. Model name aliasing is in one place (litellm config.yaml), not scattered across application code.

**Node LLM configuration:**

```python
# graph/llm.py
from langchain_openai import ChatOpenAI

def make_llm(model_alias: str, temperature: float = 0.7) -> ChatOpenAI:
    """All nodes use this. model_alias is the name registered in litellm config."""
    return ChatOpenAI(
        model=model_alias,          # e.g. "qwen-122b" or "qwen-35b"
        base_url="http://localhost:4000/v1",
        api_key="not-needed",       # LiteLLM proxy doesn't require auth for local use
        temperature=temperature,
        timeout=300,                # 122b can be slow on long research tasks
    )

# In research_node and plan_node:
llm = make_llm("qwen-122b")

# In openhands_adapter.py (OpenHands LLM):
# model="openai/qwen-35b", api_base="http://localhost:4000/v1"
# The "openai/" prefix tells OpenHands SDK to use the OpenAI-compat path via LiteLLM.
```

### Pattern 5: Job Registry Separate from LangGraph Checkpointer

**What:** Maintain a minimal `jobs` table in a separate SQLite file (`jobs.db`). Columns: `id TEXT PRIMARY KEY, goal TEXT, status TEXT, result JSON, created_at TEXT, updated_at TEXT`. The LangGraph checkpointer (checkpoints.db) stores full graph state per node. The job registry stores user-visible status.

**When to use:** Always. Conflating them creates coupling — the API layer would need to understand LangGraph checkpoint internals to answer "is this job done?"

**Schema:**

```sql
CREATE TABLE IF NOT EXISTS jobs (
    id          TEXT PRIMARY KEY,
    goal        TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending',  -- pending/running/complete/failed
    result      TEXT,                              -- JSON-encoded ExecutionResult
    error       TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
```

**State transitions:**
```
(enqueue) → pending
(worker picks up) → running
(graph END reached) → complete + result written
(exception) → failed + error written
(service restart, was running) → pending (re-enqueued for resume)
```

---

## Data Flow

### Goal Submission Flow

```
[CLI: submit.py POST /goals {"goal": "..."}]
         ↓ HTTP 202 Accepted + {"job_id": "uuid"}
[FastAPI route: creates job row (status=pending), enqueues (job_id, goal)]
         ↓ asyncio.Queue.put()
[Worker coroutine: dequeues, sets status=running]
         ↓ graph.ainvoke({"goal":..., "job_id":...}, config={"configurable":{"thread_id": job_id}})
[research_node: ChatOpenAI("qwen-122b") → LiteLLM :4000 → mlx 122b]
         ↓ AsyncSqliteSaver checkpoint after node
[plan_node: ChatOpenAI("qwen-122b") → LiteLLM :4000 → mlx 122b]
         ↓ AsyncSqliteSaver checkpoint after node
[execute_node: loop.run_in_executor → _run_openhands_sync thread]
         │    OpenHands SDK: LLM("openai/qwen-35b", api_base=:4000) → mlx 35b
         │    conversation.run() → agent loop → tool executions
         ↓ returns ExecutionResult
[Worker: sets job status=complete, writes result to jobs.db]
         ↓
[CLI poll: GET /jobs/{id} → 200 {"status":"complete", "result":...}]
```

### Status Query Flow

```
[CLI: GET /jobs/{id}]
         ↓
[FastAPI route: aiosqlite SELECT FROM jobs WHERE id=?]
         ↓ returns status row immediately (no graph involvement)
[Optional: GET /jobs/{id}/state]
         ↓
[FastAPI route: graph.aget_state({"configurable":{"thread_id": id}})]
         ↓ returns LangGraph StateSnapshot (full event_log, partial results)
```

### Crash-Resume Flow

```
[Service crashes mid execute_node]
         ↓
[launchd KeepAlive restarts uvicorn process]
         ↓
[lifespan startup: job_store.get_by_status("running") → [job_A, ...]]
         ↓
[Re-enqueue job_A with status=pending]
         ↓
[Worker picks up job_A → graph.ainvoke with same thread_id]
         ↓
[AsyncSqliteSaver loads last checkpoint: research + plan nodes already done]
         ↓
[Graph resumes at execute_node — skips research and plan entirely]
```

---

## Concurrency Model for Long Runs

This is the most important design decision. The problem: `graph.ainvoke()` runs for minutes (research + plan + execute). FastAPI must remain responsive during this time.

**Solution: single asyncio worker coroutine + run_in_executor for blocking code.**

```
Main asyncio event loop (uvicorn thread):
  ├── FastAPI HTTP handlers (POST /goals, GET /jobs) — always responsive
  ├── Worker coroutine — awaiting graph.ainvoke()
  │     ├── research_node: await llm.ainvoke() — non-blocking (HTTP I/O, yields to event loop)
  │     ├── plan_node: await llm.ainvoke() — non-blocking
  │     └── execute_node: await loop.run_in_executor(_executor, _run_openhands_sync)
  │           └── [ThreadPoolExecutor thread]
  │                 └── conversation.run() — blocking, runs OpenHands agent loop
  └── AsyncSqliteSaver checkpoint writes — non-blocking (aiosqlite)
```

**Why this works:**
- `ChatOpenAI.ainvoke()` is fully async — HTTP calls use httpx async under the hood. The event loop is free during model inference.
- `conversation.run()` is synchronous/blocking — it must run in a thread. The `_executor` ThreadPoolExecutor with `max_workers=1` ensures only one OpenHands run at a time.
- The asyncio event loop thread is never blocked — FastAPI health checks and status queries answer immediately even during a 10-minute Execute phase.

**Why not asyncio.create_task() per job:**
The worker is intentionally serial. Running two goals in parallel would double GPU RAM usage and cause LiteLLM to queue requests anyway. Serial processing matches the single-operator, single-GPU constraint.

**Why not a separate process for OpenHands:**
The project spec calls for in-process Python SDK. A subprocess approach would require IPC for state handoff. In-process with run_in_executor is simpler and the threading overhead is negligible compared to LLM inference time.

---

## launchd Service Wrapper

### Three-Service Architecture

```
launchd manages three LaunchAgent plists (~/Library/LaunchAgents/):
  com.ohama.qwen122b.plist     — mlx_lm.server for 122b (already exists)
  com.ohama.litellm.plist      — litellm proxy :4000 (already exists)
  com.ohama.orchestrator.plist — this project (new)
```

### Dependency Ordering Strategy

launchd has no formal dependency DAG. The standard approach for localhost services is **retry-on-connection-failure at startup**, not declarative ordering.

```python
# main.py — health check on startup before accepting requests
async def wait_for_litellm(retries=30, delay=2.0):
    """Probe LiteLLM :4000 /health until ready. Called in lifespan before yield."""
    import httpx
    async with httpx.AsyncClient() as client:
        for _ in range(retries):
            try:
                resp = await client.get("http://localhost:4000/health", timeout=2.0)
                if resp.status_code == 200:
                    return
            except httpx.ConnectError:
                pass
            await asyncio.sleep(delay)
    raise RuntimeError("LiteLLM proxy at :4000 not available after retries")
```

The orchestrator plist uses a short `ExitTimeout` so launchd restarts it quickly if the health check fails at first boot. The 122b model takes ~37s (warm cache) to load; the orchestrator will retry for 60s before declaring failure.

### Orchestrator Plist

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
    "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.ohama.orchestrator</string>

    <key>ProgramArguments</key>
    <array>
        <string>/Users/ohama/projs/LangGraph_OpenHands/.venv/bin/uvicorn</string>
        <string>orchestrator.main:app</string>
        <string>--host</string>
        <string>127.0.0.1</string>
        <string>--port</string>
        <string>8080</string>
    </array>

    <key>WorkingDirectory</key>
    <string>/Users/ohama/projs/LangGraph_OpenHands</string>

    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/Users/ohama/projs/LangGraph_OpenHands/.venv/bin:/usr/local/bin:/usr/bin:/bin</string>
        <key>LITELLM_BASE_URL</key>
        <string>http://localhost:4000/v1</string>
    </dict>

    <key>KeepAlive</key>
    <true/>

    <key>StandardOutPath</key>
    <string>/Users/ohama/.orchestrator/logs/stdout.log</string>

    <key>StandardErrorPath</key>
    <string>/Users/ohama/.orchestrator/logs/stderr.log</string>

    <key>RunAtLoad</key>
    <true/>
</dict>
</plist>
```

**Notes on the plist:**
- `KeepAlive true` restarts the service on crash — the lifespan startup probe handles "LiteLLM not ready yet" gracefully.
- `RunAtLoad true` starts it on login, matching the existing 122b and litellm agents.
- Absolute path to `.venv/bin/uvicorn` — launchd does not source shell profiles; relative paths fail silently.
- The orchestrator should NOT use `OtherJobEnabled` — that key controls whether the job runs based on another job's **enabled** (not running) state, which is not the dependency we need.

---

## Suggested Build Order (Dependency-Driven)

Components have strict dependencies. The order below ensures each phase produces a runnable/testable artifact.

```
Phase 1 — Foundation (no external dependencies)
  ├── graph/state.py        OrchestratorState TypedDict
  ├── api/models.py         GoalRequest, JobStatus Pydantic models
  └── persistence/job_store.py  aiosqlite job registry
  Testable: job registry CRUD with pytest-asyncio

Phase 2 — Graph skeleton (depends on Phase 1)
  ├── graph/llm.py          make_llm() → ChatOpenAI pointing at :4000
  ├── graph/nodes/research.py  research_node stub (real LLM or fake)
  ├── graph/nodes/plan.py   plan_node stub
  ├── graph/nodes/execute.py   execute_node stub (no OpenHands yet)
  └── graph/graph.py        build_graph() → compiled StateGraph + AsyncSqliteSaver
  Testable: graph.ainvoke() end-to-end with stub nodes and real checkpointer

Phase 3 — Worker + API (depends on Phase 1 + 2)
  ├── worker/runner.py      asyncio.Queue + worker coroutine
  └── main.py               FastAPI app + lifespan
  Testable: POST /goals → poll GET /jobs/{id} with stub graph

Phase 4 — Real LLM nodes (depends on Phase 2, needs LiteLLM :4000 up)
  ├── graph/nodes/research.py  real ChatOpenAI research prompt
  └── graph/nodes/plan.py   real ChatOpenAI plan prompt
  Testable: integration test with live LiteLLM proxy

Phase 5 — OpenHands Execute (depends on Phase 2 + 4)
  ├── execution/openhands_adapter.py  run_openhands() with ThreadPoolExecutor
  └── graph/nodes/execute.py   real execute_node calling adapter
  Testable: end-to-end goal → research → plan → execute → result

Phase 6 — launchd + CLI (depends on Phase 3 + 5)
  ├── launchd/com.ohama.orchestrator.plist
  └── cli/submit.py         submit goal, poll until done, print result
  Testable: launchctl load plist, submit.py "write hello world to test.py"
```

**Dependency rule:** Never start Phase N until Phase N-1 is passing its own tests. The execute_node stub in Phase 2 is crucial — it lets the graph, worker, and API be tested without OpenHands SDK installed or LiteLLM running.

---

## Anti-Patterns

### Anti-Pattern 1: Blocking the Event Loop with conversation.run()

**What people do:** Call `conversation.run()` directly inside `async def execute_node(state)`.

**Why it's wrong:** This blocks the entire uvicorn event loop for the duration of the OpenHands run (potentially 10-30 minutes). FastAPI cannot respond to `/jobs/{id}` status queries. The health check endpoint goes dark. launchd may think the service is unhealthy.

**Do this instead:** Use `loop.run_in_executor(_executor, _run_openhands_sync, ...)` in the execute_node. The sync function runs in a ThreadPoolExecutor thread; the event loop remains free.

### Anti-Pattern 2: Using FastAPI BackgroundTasks for the Graph Run

**What people do:** `background_tasks.add_task(run_graph, goal)` inside the POST /goals handler.

**Why it's wrong:** FastAPI BackgroundTasks run after the response is sent but are still tied to the request lifecycle. They do not persist across requests and provide no job_id tracking. More critically, they still run in the event loop — if `ainvoke` calls any blocking code (like conversation.run()), the loop blocks.

**Do this instead:** asyncio.Queue + dedicated worker coroutine launched in lifespan. The worker is decoupled from individual requests and owns the full job lifecycle.

### Anti-Pattern 3: Committing Checkpointer and Job Registry to the Same SQLite File

**What people do:** Use a single `state.db` for both LangGraph checkpoints and job status.

**Why it's wrong:** LangGraph's checkpoint schema is internal and may change across versions. Mixing it with application tables creates version-coupling: a LangGraph upgrade might require a migration that breaks your job registry schema, or vice versa.

**Do this instead:** Two separate SQLite files (`checkpoints.db` for LangGraph, `jobs.db` for application state). Both are tiny and fast. The separation clarifies ownership.

### Anti-Pattern 4: Hardcoding mlx Port Numbers in Graph Nodes

**What people do:** `ChatOpenAI(base_url="http://localhost:8001/v1", model="qwen-35-...")` directly in node code.

**Why it's wrong:** The existing setup already has a LiteLLM proxy that abstracts port numbers. Bypassing it means if the mlx port changes (e.g., 122b moves from 8001 to 8003), you must update application code instead of just the litellm config.yaml.

**Do this instead:** All nodes call `http://localhost:4000/v1` with a logical model name. Port assignments live exclusively in the litellm config.yaml.

### Anti-Pattern 5: Starting the OpenHands Conversation Inside research_node or plan_node

**What people do:** Create an `LLM` and `Conversation` in the Research or Plan nodes for "better integration."

**Why it's wrong:** Research and Plan nodes need simple ChatCompletion calls — a question → structured answer loop. OpenHands SDK adds agent loop overhead, tool execution infrastructure, and workspace state for what is essentially a raw LLM call. It also couples Research/Plan to the OpenHands dependency.

**Do this instead:** Research and Plan nodes use `ChatOpenAI` (langchain-openai) directly. Only the Execute node uses OpenHands SDK. The SDK boundary is `execution/openhands_adapter.py`.

---

## Integration Points

### External Services

| Service | Integration Pattern | Notes |
|---------|---------------------|-------|
| LiteLLM proxy :4000 | `ChatOpenAI(base_url="http://localhost:4000/v1")` in LangGraph nodes; `LLM(api_base="http://localhost:4000/v1")` in OpenHands SDK | Single endpoint for all LLM calls. Health probe at startup. 300s timeout for long requests. |
| mlx_lm.server :8001 (122b) | Via LiteLLM proxy — never direct | LiteLLM routes `qwen-122b` model alias to this port |
| mlx_lm.server :8000 (35b) | Via LiteLLM proxy — never direct | LiteLLM routes `qwen-35b` model alias to this port |
| checkpoints.db | `AsyncSqliteSaver.from_conn_string("checkpoints.db")` in lifespan | Thread_id = job_id. Created automatically. Survives restarts. |
| jobs.db | `aiosqlite.connect("jobs.db")` in lifespan | Application-level job registry. Schema migrated on first run. |
| Filesystem (workspace) | OpenHands `LocalWorkspace` in adapter | OpenHands works directly on the project directory. No Docker. |

### Internal Boundaries

| Boundary | Communication | Notes |
|----------|---------------|-------|
| FastAPI ↔ Worker | `asyncio.Queue.put((job_id, goal))` | One-way. HTTP handler never awaits worker completion. |
| Worker ↔ Graph | `await graph.ainvoke(state, config)` | Async. Worker is the only caller of ainvoke. |
| execute_node ↔ OpenHands | `await loop.run_in_executor(...)` | Thread boundary. Event loop stays free. |
| Graph nodes ↔ LiteLLM | `await llm.ainvoke(messages)` over HTTP | Async HTTP. ChatOpenAI uses httpx internally. |
| FastAPI ↔ jobs.db | `async with aiosqlite.connect(...)` | Direct aiosqlite. No ORM needed for 4-column table. |
| FastAPI ↔ graph (state query) | `await graph.aget_state(config)` | Optional advanced endpoint. Returns StateSnapshot. |

---

## Scaling Considerations

This is single-operator, localhost-only. Traditional concurrency scaling is irrelevant. Relevant "scaling" is operational complexity as goals grow larger.

| Concern | Current (single operator) | If goals get longer | If goals become parallel |
|---------|--------------------------|---------------------|--------------------------|
| LLM inference time | 122b research: 2-5 min; execute: 5-20 min | Add timeout per node; surface progress via event_log | Not in scope; serial is intentional |
| Context window (research findings) | Qwen 122b 128k context is ample | Chunk research output; add summarization node | N/A |
| SQLite concurrency | Single writer (worker); readers (HTTP handlers) fine | SQLite WAL mode for concurrent reads | Switch to Postgres if ever multi-process |
| Crash recovery | Resume from last checkpoint; re-run execute if it was interrupted | Execute node must track sub-step completion | N/A |
| Workspace conflicts | Single active goal — no conflict | Sub-goals in separate workspace directories | Each goal gets its own workspace dir |

---

## Sources

- [OpenHands Software Agent SDK paper (arXiv:2511.03690)](https://arxiv.org/html/2511.03690v1) — ConversationState, LocalConversation, threading model, in-process vs remote architecture
- [openhands-sdk PyPI](https://pypi.org/project/openhands-sdk/) — version 1.29.0, Python >=3.12 requirement
- [OpenHands SDK Conversation API reference](https://docs.openhands.dev/sdk/api-reference/openhands.sdk.conversation) — constructor parameters, run() semantics, ask_agent() thread-safety
- [AsyncSqliteSaver + LangGraph + FastAPI (Medium)](https://medium.com/@devwithll/simple-langgraph-implementation-with-memory-asyncsqlitesaver-checkpointer-fastapi-54f4e4879a2e) — AsyncSqliteSaver in lifespan, thread_id config, aget_state pattern
- [LangGraph durable execution (vadim.blog)](https://vadim.blog/durable-execution-agents-that-survive-failure-and-resume-where-they-left-off) — thread_id as primary key, checkpoint resume after crash, run_id observability decoupling
- [FastAPI + LangGraph production template (ranjankumar.in)](https://ranjankumar.in/building-production-ready-ai-agent-services-fastapi-langgraph-template-deep-dive) — async execution model, thread_id namespacing, project structure
- [LiteLLM proxy client usage](https://docs.litellm.ai/docs/providers/litellm_proxy) — api_base=localhost:4000, model prefix pattern
- [FastAPI BackgroundTasks vs asyncio (sentry.io)](https://sentry.io/answers/fastapi-difference-between-run-in-executor-and-run-in-threadpool/) — run_in_executor vs run_in_threadpool tradeoffs
- [launchd.plist man page](https://keith.github.io/xcode-man-pages/launchd.plist.5.html) — KeepAlive, OtherJobEnabled, RunAtLoad, no explicit dependency model
- [LangGraph StateGraph TypedDict patterns (machinelearningplus.com)](https://machinelearningplus.com/langgraph-state-management-typeddict-reducers) — Annotated reducers, partial state returns, last-write-wins vs append

---
*Architecture research for: Local LangGraph + OpenHands orchestrator (Research → Plan → Execute)*
*Researched: 2026-06-19*
