# Stack Research

**Domain:** Local multi-agent orchestrator — LangGraph (Research/Plan) + OpenHands SDK (Execute) + FastAPI + launchd on macOS
**Researched:** 2026-06-19
**Confidence:** HIGH

---

## Recommended Stack

### Core Technologies

| Technology | Package | Version | Purpose | Why Recommended |
|------------|---------|---------|---------|-----------------|
| LangGraph | `langgraph` | 1.2.6 | StateGraph orchestration (Research → Plan nodes) | Latest stable (June 18 2026). 1.x line is the stable API; 0.x is EOL. Provides durable graph execution, checkpointed supersteps, conditional edges, and thread-scoped state — exactly the primitives needed for a multi-node Research/Plan pipeline. |
| LangGraph SQLite checkpointer | `langgraph-checkpoint-sqlite` | 3.1.0 | Persist graph state to disk, survive reboots | 3.1.0 (May 12 2026). Single-machine use: SQLite has zero operational overhead (no Postgres service, no config). AsyncSqliteSaver provides aiosqlite-backed async checkpointing. Critical patch in 3.0.1 fixed CVE-2025-67644 SQL injection — use >=3.0.1. |
| LangChain OpenAI integration | `langchain-openai` | 1.3.2 | ChatOpenAI for LLM nodes | 1.3.2 (June 13 2026). ChatOpenAI accepts `base_url` + `api_key` directly, routing to any OpenAI-compatible endpoint including LiteLLM :4000. The clean way to call qwen-122b (Research/Plan) vs qwen-35b in separate graph nodes. |
| OpenHands SDK | `openhands-sdk` | 1.29.0 | In-process Execute agent | 1.29.0 (June 18 2026). Python >=3.12 required. Provides `LLM`, `Agent`, `Conversation`, `Tool` in a single import surface; `Conversation` with a local workspace runs fully in-process — no subprocess, no headless CLI, no ACP protocol. Internally uses LiteLLM for routing. |
| FastAPI | `fastapi` | 0.137.2 | HTTP service layer | 0.137.2 (June 18 2026). The always-on service surface: `POST /goals` to enqueue a goal, `GET /jobs/{id}` to poll status. Lifespan pattern launches the asyncio worker on startup. Trivially served by uvicorn; no WSGI, no gunicorn needed for single-operator localhost. |
| Uvicorn | `uvicorn[standard]` | 0.49.0 | ASGI server | 0.49.0 (June 2026). `[standard]` extras include uvloop (faster event loop) and httptools. The binary that launchd's ProgramArguments invokes directly. |
| aiosqlite | `aiosqlite` | >=0.19 | Async SQLite access | Transitive dep of langgraph-checkpoint-sqlite. Also used directly for the job-status store (job_id → status/result rows), keeping the entire persistence surface in a single SQLite file without a Postgres service. |

### Supporting Libraries

| Library | Package | Version | Purpose | When to Use |
|---------|---------|---------|---------|-------------|
| LangChain Core | `langchain-core` | >=0.3 | BaseMessage, HumanMessage, AIMessage types | Pulled in by langchain-openai; required for constructing messages passed to ChatOpenAI invoke. |
| Python standard `asyncio` | stdlib | 3.12+ | asyncio.Queue worker inside FastAPI | No extra dep. Use `asyncio.Queue` as the in-process job queue: single worker coroutine drains it, running one goal at a time. One machine, one process, one queue. |
| `python-dotenv` | `python-dotenv` | >=1.0 | Load env vars from .env | Useful during dev; in production the launchd plist EnvironmentVariables dict handles env. |
| `pydantic` | `pydantic` | >=2.7 | Request/response models for FastAPI endpoints | FastAPI v0.137 requires pydantic v2; already a transitive dep. Define `GoalRequest`, `JobStatus` as BaseModel subclasses. |
| `httpx` | `httpx` | >=0.27 | Optional: probe LiteLLM :4000 health at startup | If you want a startup health check before accepting goals. Not required for core flow. |

### Development Tools

| Tool | Purpose | Notes |
|------|---------|-------|
| `uv` | Project management + venv | Already on system. `uv venv .venv && uv pip install -r requirements.txt`. The launchd plist points ProgramArguments at `.venv/bin/uvicorn` (absolute path). |
| `pytest` + `pytest-asyncio` | Test async graph nodes and FastAPI endpoints | `pytest-asyncio` with `asyncio_mode = "auto"` in `pyproject.toml` handles `async def test_*` without boilerplate. |
| `ruff` | Linting + formatting | Fast, zero-config defaults. One tool replaces flake8 + black + isort. |

---

## Installation

```bash
# Create venv with uv (Python 3.12 required for openhands-sdk)
uv venv .venv --python 3.12
source .venv/bin/activate

# Core packages
uv pip install \
  langgraph==1.2.6 \
  langgraph-checkpoint-sqlite==3.1.0 \
  langchain-openai==1.3.2 \
  openhands-sdk==1.29.0 \
  fastapi==0.137.2 \
  "uvicorn[standard]==0.49.0" \
  aiosqlite \
  pydantic>=2.7 \
  python-dotenv

# Dev / test
uv pip install pytest pytest-asyncio ruff
```

---

## Alternatives Considered

| Recommended | Alternative | When to Use Alternative | Why NOT here |
|-------------|-------------|------------------------|--------------|
| `langgraph-checkpoint-sqlite` | `langgraph-checkpoint-postgres` | Multi-machine deployments, high write concurrency, need to query checkpoint history via SQL | Single operator, one machine, no concurrent writers. SQLite is zero-ops. Postgres requires a running service, connection string, migrations. The swap is one line when/if you ever need it. |
| `langchain-openai` ChatOpenAI | `ChatLiteLLM` from `langchain-community` | When you want LiteLLM routing logic inside LangChain (e.g., fallbacks, load balancing at the LangChain layer) | LiteLLM :4000 is already running as a proxy on the system. ChatOpenAI with `base_url="http://localhost:4000/v1"` uses that proxy transparently. No need for a second LiteLLM layer inside LangChain. |
| `asyncio.Queue` in-process worker | Celery + Redis | Distributed workers, multiple machines, retry queues, scheduled tasks | Single-machine, single-operator, no Redis service. Celery adds a broker (Redis/RabbitMQ), a worker process, and serialization overhead for zero benefit here. `asyncio.Queue` inside FastAPI's event loop is the correct scope. |
| `asyncio.Queue` in-process worker | ARQ (Async Request Queue) | Single machine but need Redis anyway for another reason, or want built-in retry/scheduling | ARQ still requires Redis. The job state (queued/running/done/error) lives in the SQLite DB; no separate broker needed. |
| `asyncio.Queue` in-process worker | FastAPI `BackgroundTasks` | Fire-and-forget tasks with <5s duration | BackgroundTasks has no status tracking, no queue, no concurrency control. Agent runs take minutes; you need to poll status. BackgroundTasks is not a queue — it runs tasks in the response cycle after the response is sent, with no way to limit concurrency. |
| `uvicorn` direct | Gunicorn + uvicorn workers | High-traffic multi-core production server | One operator, localhost only. Gunicorn adds process management complexity that launchd + KeepAlive already provides. `uvicorn --workers 1` is the right shape. |
| OpenHands in-process SDK | OpenHands headless CLI subprocess | When you can't install the SDK (e.g., version conflicts) | Subprocess spawning loses structured return values, requires parsing stdout, and can't share the LangGraph state object. In-process `Conversation.run()` returns cleanly and can write to the graph state dict directly. |

---

## What NOT to Use

| Avoid | Why | Use Instead |
|-------|-----|-------------|
| `Celery` + Redis | Adds two services (Redis + Celery worker), distributed task serialization, and deployment complexity. Zero benefit for a single-machine, single-operator setup. | `asyncio.Queue` worker inside FastAPI lifespan |
| `FastAPI BackgroundTasks` for agent runs | Not a queue. No status tracking. No concurrency limit. Tasks share the process but there is no way to bound how many run simultaneously. | `asyncio.Queue` with a single worker coroutine |
| `MemorySaver` checkpointer | In-memory only — loses all state on process restart. Defeats the "survive reboots" requirement. | `AsyncSqliteSaver` from `langgraph-checkpoint-sqlite` |
| `langgraph-checkpoint-sqlite` < 3.0.1 | CVE-2025-67644: SQL injection in thread_id parameter. Patched in 3.0.1. | Pin to >=3.0.1 (current: 3.1.0) |
| OpenHands ACP protocol / headless CLI | ACP is for remote agent communication between services; the headless CLI spawns a subprocess. Both are wrong for in-process orchestration from LangGraph. | `openhands-sdk` Python API: `LLM`, `Agent`, `Conversation` |
| `openhands-ai` (the full OpenHands application package on PyPI) | The heavy full-stack server package. The SDK is `openhands-sdk`. Don't confuse the two. | `openhands-sdk==1.29.0` |
| Postgres on a single machine | Operational overhead (install, configure, start service, migrations) for no gain when SQLite covers the single-writer, single-machine case. | `aiosqlite` + `langgraph-checkpoint-sqlite` SQLite |
| Raw `mlx_lm` Python import in orchestrator | Would load the full model weights into the orchestrator process, OOMing the system (as blueCode's eval harness learned in v2.1: HTTP-only adaptation is the correct pattern). | Call LiteLLM :4000 over HTTP via ChatOpenAI |

---

## Key Code Shapes

### LangGraph node calling LiteLLM :4000 with two different models

```python
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import StateGraph, END
from typing import TypedDict, Annotated
import operator

# Two ChatOpenAI instances — one per model tier
# api_key="dummy": LiteLLM proxy doesn't check it, but ChatOpenAI requires a non-empty value
research_llm = ChatOpenAI(
    model="qwen-122b",           # LiteLLM model alias
    base_url="http://localhost:4000/v1",
    api_key="dummy",
    temperature=0.7,
    max_tokens=8192,
)

plan_llm = ChatOpenAI(
    model="qwen-122b",           # Research + Plan both use 122b
    base_url="http://localhost:4000/v1",
    api_key="dummy",
    temperature=0.3,             # Lower temp for structured plan output
    max_tokens=4096,
)

# execute_llm is NOT called from LangGraph directly —
# it is invoked via the OpenHands SDK (see below)

# Graph state schema
class OrchestratorState(TypedDict):
    goal: str
    research_findings: str
    plan: str
    execution_result: str
    error: str

def research_node(state: OrchestratorState) -> dict:
    messages = [
        SystemMessage(content="You are a research agent. Gather findings relevant to the goal."),
        HumanMessage(content=f"Goal: {state['goal']}\n\nResearch this goal thoroughly."),
    ]
    response = research_llm.invoke(messages)
    return {"research_findings": response.content}

def plan_node(state: OrchestratorState) -> dict:
    messages = [
        SystemMessage(content="You are a planning agent. Produce a concrete execution plan."),
        HumanMessage(content=f"Goal: {state['goal']}\nResearch: {state['research_findings']}\n\nProduce a step-by-step plan."),
    ]
    response = plan_llm.invoke(messages)
    return {"plan": response.content}

# Build the StateGraph
builder = StateGraph(OrchestratorState)
builder.add_node("research", research_node)
builder.add_node("plan", plan_node)
builder.add_node("execute", execute_node)   # defined below
builder.set_entry_point("research")
builder.add_edge("research", "plan")
builder.add_edge("plan", "execute")
builder.add_edge("execute", END)
```

### AsyncSqliteSaver checkpointer — compile and run with thread_id

```python
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

async def run_goal(goal: str, thread_id: str) -> str:
    async with AsyncSqliteSaver.from_conn_string("~/.orchestrator/checkpoints.sqlite") as saver:
        graph = builder.compile(checkpointer=saver)
        config = {"configurable": {"thread_id": thread_id}}
        # astream_events gives per-node progress; use ainvoke for simpler case
        final_state = await graph.ainvoke(
            {"goal": goal, "research_findings": "", "plan": "", "execution_result": "", "error": ""},
            config=config,
        )
    return final_state["execution_result"]

# To resume a failed run: call run_goal with the same thread_id.
# LangGraph reconstructs state from the last checkpoint superstep.
```

### OpenHands SDK in-process execute node

```python
from openhands.sdk import LLM, Agent, Conversation, Tool
from openhands.tools.terminal import TerminalTool
from openhands.tools.file_editor import FileEditorTool
from openhands.tools.task_tracker import TaskTrackerTool

# LLM configured to use qwen-35b via LiteLLM :4000
# "openai/" prefix tells LiteLLM's internal routing to use the openai-compatible path
execute_llm = LLM(
    model="openai/qwen-35b",     # "openai/" prefix required by LiteLLM routing inside openhands-sdk
    api_key="dummy",
    base_url="http://localhost:4000/v1",
)

execute_agent = Agent(
    llm=execute_llm,
    tools=[
        Tool(name=TerminalTool.name),
        Tool(name=FileEditorTool.name),
        Tool(name=TaskTrackerTool.name),
    ],
)

def execute_node(state: OrchestratorState) -> dict:
    """Run OpenHands execute agent in-process. Blocks until done."""
    workspace_path = "/path/to/project"   # target workspace for the agent
    conversation = Conversation(agent=execute_agent, workspace=workspace_path)
    conversation.send_message(
        f"Goal: {state['goal']}\n\nPlan:\n{state['plan']}\n\nExecute the plan."
    )
    conversation.run()   # blocking — runs full agent loop in-process
    # Extract result from conversation state; exact API depends on SDK version
    result = conversation.state.last_output if hasattr(conversation.state, "last_output") else "done"
    return {"execution_result": result}
```

**Important:** `agent_settings.json` at `~/.openhands/agent_settings.json` is only read by the OpenHands CLI/UI. The SDK uses `LLM(...)` constructed programmatically. The existing `agent_settings.json` on this machine already has `"model": "openai/qwen-35b"` and `"base_url": "http://localhost:4000/v1"` — this confirms the routing pattern works and can be mirrored exactly in SDK code.

The `"openai/"` prefix on the model name is the LiteLLM routing signal. Without it, LiteLLM defaults to the OpenAI provider; with it, it routes to the configured openai-compatible backend (your LiteLLM proxy's `qwen-35b` alias).

### FastAPI service with asyncio.Queue job runner

```python
import asyncio
import uuid
import sqlite3
from contextlib import asynccontextmanager
from fastapi import FastAPI
from pydantic import BaseModel

# --- Job state (SQLite) ---
JOB_DB = "/Users/ohama/orchestrator/jobs.db"

def init_job_db():
    con = sqlite3.connect(JOB_DB)
    con.execute("""CREATE TABLE IF NOT EXISTS jobs (
        id TEXT PRIMARY KEY,
        status TEXT NOT NULL,
        goal TEXT NOT NULL,
        result TEXT,
        error TEXT,
        created_at TEXT DEFAULT (datetime('now')),
        updated_at TEXT DEFAULT (datetime('now'))
    )""")
    con.commit(); con.close()

# --- asyncio queue + worker ---
job_queue: asyncio.Queue = asyncio.Queue()

async def worker():
    """Single worker: processes goals one at a time."""
    while True:
        job_id, goal = await job_queue.get()
        _set_job_status(job_id, "running")
        try:
            result = await run_goal(goal, thread_id=job_id)  # LangGraph call
            _set_job_status(job_id, "done", result=result)
        except Exception as exc:
            _set_job_status(job_id, "error", error=str(exc))
        finally:
            job_queue.task_done()

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_job_db()
    asyncio.create_task(worker())   # start single worker
    yield
    # worker drains naturally on shutdown

app = FastAPI(lifespan=lifespan)

class GoalRequest(BaseModel):
    goal: str

@app.post("/goals")
async def submit_goal(req: GoalRequest):
    job_id = str(uuid.uuid4())
    _insert_job(job_id, req.goal)
    await job_queue.put((job_id, req.goal))
    return {"job_id": job_id, "status": "queued"}

@app.get("/jobs/{job_id}")
async def get_job(job_id: str):
    return _get_job(job_id)
```

### launchd plist for the orchestrator service

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.ohama.orchestrator</string>

    <key>ProgramArguments</key>
    <array>
        <!-- Use absolute path to venv uvicorn binary — never rely on PATH for launchd -->
        <string>/Users/ohama/projs/LangGraph_OpenHands/.venv/bin/uvicorn</string>
        <string>orchestrator.main:app</string>
        <string>--host</string>
        <string>127.0.0.1</string>
        <string>--port</string>
        <string>8080</string>
        <string>--workers</string>
        <string>1</string>
    </array>

    <key>WorkingDirectory</key>
    <string>/Users/ohama/projs/LangGraph_OpenHands</string>

    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/Users/ohama/projs/LangGraph_OpenHands/.venv/bin:/opt/homebrew/bin:/usr/bin:/bin</string>
        <!-- LiteLLM proxy is already running at :4000 via com.ohama.litellm -->
        <key>LITELLM_BASE_URL</key>
        <string>http://localhost:4000/v1</string>
        <key>OPENHANDS_SETTINGS_PATH</key>
        <string>/Users/ohama/.openhands/agent_settings.json</string>
    </dict>

    <key>RunAtLoad</key>
    <true/>

    <key>KeepAlive</key>
    <true/>

    <!-- Matches the existing pattern: throttle restart if process exits immediately -->
    <key>ThrottleInterval</key>
    <integer>10</integer>

    <key>StandardOutPath</key>
    <string>/Users/ohama/projs/LangGraph_OpenHands/logs/orchestrator.log</string>

    <key>StandardErrorPath</key>
    <string>/Users/ohama/projs/LangGraph_OpenHands/logs/orchestrator.err.log</string>
</dict>
</plist>
```

**Deploy pattern (matching existing com.ohama.* convention):**
```bash
# Place plist
cp com.ohama.orchestrator.plist ~/Library/LaunchAgents/

# Load (survives reboots via RunAtLoad)
launchctl load ~/Library/LaunchAgents/com.ohama.orchestrator.plist

# Restart after code changes
launchctl kickstart -k gui/$(id -u)/com.ohama.orchestrator

# Check status
launchctl print gui/$(id -u)/com.ohama.orchestrator
```

---

## Version Compatibility

| Package | Version | Python | Notes |
|---------|---------|--------|-------|
| `langgraph` | 1.2.6 | >=3.10 | Use >=1.0.10 to pick up SQL injection patch chain |
| `langgraph-checkpoint-sqlite` | 3.1.0 | >=3.10 | Must be >=3.0.1 for CVE-2025-67644 fix; 3.1.0 is current |
| `langchain-openai` | 1.3.2 | >=3.10 | Requires `langchain-core>=0.3` (auto-resolved) |
| `openhands-sdk` | 1.29.0 | **>=3.12** | Python 3.12 minimum — higher than LangGraph's 3.10. Pin your venv to 3.12+. |
| `fastapi` | 0.137.2 | >=3.10 | Requires `pydantic>=2.7` (auto-resolved) |
| `uvicorn[standard]` | 0.49.0 | >=3.10 | `[standard]` adds uvloop + httptools |
| `aiosqlite` | >=0.19 | >=3.10 | Transitive of langgraph-checkpoint-sqlite; also usable directly |

**Critical constraint:** `openhands-sdk` requires Python >=3.12. Use Python 3.12 for the project venv — this is more restrictive than LangGraph's 3.10 floor.

---

## Stack Patterns by Scenario

**If you need Research node and Plan node to use different temperatures:**
- Instantiate two separate `ChatOpenAI` objects (`research_llm` and `plan_llm`) both pointing at `qwen-122b` on LiteLLM :4000.
- Each graph node closes over its own llm instance. This is the standard LangGraph pattern — nodes are plain Python functions.

**If `conversation.run()` blocks the asyncio event loop:**
- The OpenHands SDK's `Conversation.run()` is synchronous (blocking). Wrap it with `asyncio.get_event_loop().run_in_executor(None, conversation.run)` inside the async worker coroutine to avoid starving FastAPI's event loop.
- Alternatively, use `asyncio.to_thread(conversation.run)` (Python 3.9+ stdlib).

**If a goal survives a launchd restart (KeepAlive restart scenario):**
- The `asyncio.Queue` is lost on restart. Items not yet written to SQLite are lost.
- Mitigation: persist "queued" state to the SQLite jobs DB at `POST /goals` time (before `queue.put`). On startup, re-enqueue all jobs with status `"queued"` or `"running"` from the DB. LangGraph resumes from the last checkpoint via the same `thread_id`.

**If the OpenHands execute agent needs a different workspace per goal:**
- Pass workspace path as part of the `GoalRequest` body, or derive it from the job_id: `f"/tmp/orchestrator/jobs/{job_id}"`. Each `Conversation` gets its own workspace.

---

## Sources

- [langgraph PyPI — v1.2.6, June 18 2026](https://pypi.org/project/langgraph/) — version confirmed (HIGH confidence)
- [langgraph-checkpoint-sqlite PyPI — v3.1.0, May 12 2026](https://pypi.org/project/langgraph-checkpoint-sqlite) — version confirmed (HIGH confidence)
- [CVE-2025-67644 / LangGraph checkpointer SQLi — Check Point Research 2026](https://research.checkpoint.com/2026/from-sqli-to-rce-exploiting-langgraphs-checkpointer/) — SQL injection patch in 3.0.1 confirmed (HIGH confidence)
- [AsyncSqliteSaver API reference — LangChain Reference](https://reference.langchain.com/python/langgraph.checkpoint.sqlite/aio/AsyncSqliteSaver) — from_conn_string, compile pattern confirmed (HIGH confidence)
- [langchain-openai PyPI — v1.3.2, June 13 2026](https://pypi.org/project/langchain-openai/) — version confirmed (HIGH confidence)
- [ChatOpenAI constructor — LangChain Reference](https://reference.langchain.com/python/langchain-openai/chat_models/base/ChatOpenAI) — base_url, api_key, model params confirmed (HIGH confidence)
- [openhands-sdk PyPI — v1.29.0, June 18 2026](https://pypi.org/project/openhands-sdk/) — version, Python >=3.12 requirement confirmed (HIGH confidence)
- [OpenHands SDK getting-started docs](https://docs.openhands.dev/sdk/getting-started) — LLM/Agent/Conversation import surface confirmed (HIGH confidence)
- [OpenHands local LLMs docs](https://docs.openhands.dev/openhands/usage/llms/local-llms) — "openai/" prefix model naming pattern confirmed (HIGH confidence)
- [~/.openhands/agent_settings.json on this machine] — `"model": "openai/qwen-35b"`, `"base_url": "http://localhost:4000/v1"` confirmed live config (HIGH confidence — ground truth)
- [fastapi PyPI — v0.137.2, June 18 2026](https://pypi.org/project/fastapi/) — version confirmed (HIGH confidence)
- [uvicorn PyPI — v0.49.0](https://pypi.org/project/uvicorn/) — version confirmed (HIGH confidence)
- [FastAPI lifespan + asyncio.Queue pattern](https://www.shiporkill.com/blog/fastapi-lifespan-pattern) — lifespan + asyncio.create_task confirmed pattern (HIGH confidence)
- [launchd plist for Python services](https://andypi.co.uk/2023/02/14/how-to-run-a-python-script-as-a-service-on-mac-os/) — KeepAlive, RunAtLoad, absolute paths pattern (HIGH confidence)
- [~/Library/LaunchAgents/com.ohama.qwen122b.plist on this machine] — ThrottleInterval, StandardOutPath, EnvironmentVariables structure confirmed (HIGH confidence — ground truth)
- [~/Library/LaunchAgents/com.ohama.litellm.plist on this machine] — confirms venv/bin absolute path pattern for ProgramArguments (HIGH confidence — ground truth)
- [LangGraph SQLite vs Postgres — Fast.io 2026](https://fast.io/resources/langgraph-persistence/) — SQLite appropriate for single-machine local (MEDIUM confidence)

---

*Stack research for: local multi-agent orchestrator (LangGraph + OpenHands SDK + FastAPI + launchd)*
*Researched: 2026-06-19*
