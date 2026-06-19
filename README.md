# LangGraph + OpenHands Orchestrator

A local, single-operator multi-agent orchestration service. **LangGraph** is the brain
(Research → Plan) and **OpenHands** is the hands (Execute). Submit a goal to an always-on
REST service; it autonomously researches the problem, produces a plan, and drives OpenHands
to carry it out — all on locally-served Qwen models, surviving reboots via launchd.

> **Core value:** A goal submitted to an always-on local service is autonomously
> Researched → Planned → Executed end-to-end by local agents, surviving reboots.

## How it works

```
                    ┌──────────────────────────────────────────────┐
   POST /goals  ──► │  FastAPI service (uvicorn, always-on)         │
   GET  /jobs/…     │   ├─ asyncio.Queue worker (single, in-proc)   │
   CLI client       │   └─ LangGraph StateGraph                     │
                    │        research ──► plan ──► execute          │
                    └─────────┬───────────────┬────────────────┬────┘
                              │ qwen-122b      │ qwen-122b       │ OpenHands SDK
                              ▼                ▼                 ▼ (qwen-35b)
                    ┌──────────────────────────────────────────────┐
                    │  LiteLLM proxy  :4000  (OpenAI-compatible)    │
                    │   qwen-35b → mlx :8000   qwen-122b → mlx :8001 │
                    └──────────────────────────────────────────────┘

   Persistence:  jobs.db (app registry)  +  checkpoints.db (LangGraph AsyncSqliteSaver)
                 thread_id == job_id  →  durable resume across restarts
```

- **Research / Plan** run on **qwen-122b** (the smarter model) via LiteLLM.
- **Execute** runs the **OpenHands SDK in-process** on **qwen-35b** (fast).
- Fully **autonomous** — no human approval gate between Plan and Execute.
- Every LLM call goes through the **LiteLLM proxy** (`:4000`); no node touches an mlx port directly.

## Stack

| Layer | Choice |
|-------|--------|
| Language | Python 3.12 (required by the OpenHands SDK), `uv`-managed |
| Orchestration | LangGraph `StateGraph` + `langgraph-checkpoint-sqlite` (`AsyncSqliteSaver`) |
| Execution | OpenHands SDK, called in-process (`Conversation.run()`, wrapped in `asyncio.to_thread`) |
| Service | FastAPI + uvicorn, single in-process `asyncio.Queue` worker (no Celery/Redis) |
| Models | Local Qwen 35B / 122B served by `mlx_lm.server`, routed via LiteLLM |
| Persistence | Two SQLite files — `jobs.db` (app) and `checkpoints.db` (LangGraph), never mixed |
| Deployment | launchd agent (`com.ohama.orchestrator`), reboot-surviving |

## REST API

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/goals` | Submit a goal → `202` + `job_id` (returns immediately; graph runs in background) |
| `GET` | `/jobs/{job_id}/status` | Live status: `PENDING → RESEARCHING → PLANNING → EXECUTING → DONE` |
| `GET` | `/jobs/{job_id}/result` | Result for a `DONE` job (`409` if not done, `404` if unknown) |
| `DELETE` | `/jobs/{job_id}` | Cancel a running/queued job (app-level) |
| `GET` | `/health` | Liveness probe (SQLite reachable) |

## Running (development)

Prerequisites: the local LLM stack must be up — `mlx_lm.server` for qwen-35b (`:8000`) and
qwen-122b (`:8001`), and the LiteLLM proxy on `:4000` exposing model names `qwen-35b` / `qwen-122b`.

```bash
# install deps into a uv-managed venv
uv venv .venv --python 3.12
uv pip install -e ".[dev]"

# run the service (launch the venv binary directly — see note below)
DATA_DIR=./data LOG_DIR=./logs .venv/bin/uvicorn orchestrator.main:app --workers 1 --port 8099

# submit a goal
curl -XPOST localhost:8099/goals -H 'content-type: application/json' -d '{"goal":"..."}'

# run the test suite + end-to-end durability proof
uv run pytest -q
bash scripts/verify_phase1.sh
```

> **Note:** always launch via `.venv/bin/uvicorn` directly, **not** `uv run uvicorn`.
> `uv run` spawns uvicorn as a child process; killing the wrapper orphans the real server,
> which keeps holding the port. The launchd plist (Phase 5) points at the absolute
> `.venv/bin/uvicorn` path for the same reason. `--workers 1` is mandatory — a single
> in-process `asyncio.Queue` worker; multiple workers would fragment the queue.

## Project layout

```
orchestrator/
  graph/        OrchestratorState (typed, no message accumulator) + the LangGraph graph
  persistence/  JobStore + init_jobs_db (jobs.db, WAL mode)
  worker/       asyncio.Queue worker loop, per-job logger, cancel plumbing
  api/          Pydantic models + REST routes
  main.py       FastAPI app + lifespan (owns the AsyncSqliteSaver checkpointer)
tests/          pytest-asyncio suite
scripts/        verify_phase1.sh — runnable end-to-end proof (incl. kill+restart)
.planning/      GSD project docs: PROJECT, REQUIREMENTS, ROADMAP, STATE, research, phases
```

## Roadmap

Build order is strictly dependency-driven.

| Phase | Goal | Status |
|-------|------|--------|
| 1. Foundation | FastAPI + persistence + stub graph + asyncio.Queue worker | ✓ Complete |
| 2. LLM Nodes | Real research/plan nodes on qwen-122b; LiteLLM streaming/504 fix | Planned |
| 3. OpenHands Execute | In-process SDK adapter, `asyncio.to_thread`, workspace isolation, max-iterations cap | Planned |
| 4. Memory Management | Unload 122B between Plan and Execute to protect 35B throughput | Planned |
| 5. launchd Packaging | `com.ohama.orchestrator` plist, lazy LiteLLM probe, SIGTERM, startup resume | Planned |
| 6. CLI Client | `submit` / `status` / `logs` / `result` over the REST API | Planned |

Full requirements, success criteria, and traceability live in [`.planning/`](.planning/).

## Status

**Phase 1 complete and verified** — the service accepts job submissions, tracks them in
SQLite, and runs a stub graph with durable checkpoints that survive a `kill -9` + restart.
No LLM or OpenHands wiring yet (those are Phases 2–3); the state schema and `research/plan/execute`
node names are already the real ones so later phases slot in without renaming.
