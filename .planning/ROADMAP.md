# Roadmap: LangGraph + OpenHands Orchestrator

## Overview

Build a single-process, always-on macOS service that takes a goal, runs it through a Research → Plan → Execute LangGraph pipeline on local Qwen models via LiteLLM, and survives reboots via launchd. The build order is strictly dependency-driven: foundation and persistence first so every later phase has a stable, crash-resumable base; LLM nodes next to validate LiteLLM routing before OpenHands is touched; OpenHands isolation in its own phase as the riskiest integration; dual-model memory management after Execute is proven; launchd packaging once the full stack runs end-to-end; CLI last as a thin REST wrapper.

## Phases

**Phase Numbering:**
- Integer phases (1, 2, 3): Planned milestone work
- Decimal phases (2.1, 2.2): Urgent insertions (marked with INSERTED)

- [ ] **Phase 1: Foundation** - Types, persistence, FastAPI service scaffold with stub graph and asyncio.Queue worker
- [ ] **Phase 2: LLM Nodes** - Real research and plan nodes calling qwen-122b via LiteLLM; validate streaming/timeout fix
- [ ] **Phase 3: OpenHands Execute** - In-process SDK adapter, asyncio.to_thread wrap, workspace isolation, max_iterations cap
- [ ] **Phase 4: Memory Management** - Measure dual-model pressure; wire launchctl 122B unload between Plan and Execute
- [ ] **Phase 5: launchd Packaging** - com.ohama.orchestrator plist, lazy LiteLLM probe, SIGTERM handler, startup resume scan
- [ ] **Phase 6: CLI Client** - submit / status / logs / result subcommands wrapping the REST API

## Phase Details

### Phase 1: Foundation
**Goal**: A running FastAPI service accepts job submissions, tracks them in SQLite, and runs a stub graph with durable checkpoints — proving persistence and the event-loop-safe worker pattern before any LLM call is made.
**Depends on**: Nothing (first phase)
**Requirements**: ORCH-04, PERSIST-01, PERSIST-02, API-01, API-02, API-03, API-04, API-05, API-06, OBS-01
**Success Criteria** (what must be TRUE):
  1. `POST /goals` returns a `job_id` immediately (202 Accepted) while the stub graph runs asynchronously in the background worker — the HTTP response arrives before the graph finishes.
  2. `GET /jobs/{id}/status` reflects the correct status (PENDING → RESEARCHING → PLANNING → EXECUTING → DONE) as the stub graph advances through nodes.
  3. After killing and restarting the process, a previously submitted job's status and checkpoint are still readable from `jobs.db` and `checkpoints.db` — no data loss on restart.
  4. `GET /health` returns 200 with SQLite liveness confirmed.
  5. A per-run log file is created for each job and contains timestamped node-transition entries.
**Plans**: 3 plans

Plans:
- [ ] 01-01-PLAN.md — OrchestratorState TypedDict + stub graph (research/plan/execute) + jobs.db JobStore; checkpoint-survives-reopen + two empirical-verification tests
- [ ] 01-02-PLAN.md — FastAPI lifespan (AsyncSqliteSaver yield-inside-async-with) + asyncio.Queue worker + per-job logger + status transitions via astream
- [ ] 01-03-PLAN.md — REST endpoints (POST /goals 202, status, result, cancel, health) + TestClient test + verify_phase1.sh kill/restart durability proof

### Phase 2: LLM Nodes
**Goal**: The Research and Plan nodes produce real LLM outputs using qwen-122b through the LiteLLM proxy, LiteLLM streaming is verified to prevent 504 timeouts, and checkpoint persistence is confirmed across a simulated restart with real data.
**Depends on**: Phase 1
**Requirements**: ORCH-02, ORCH-03, ORCH-05, OBS-03
**Success Criteria** (what must be TRUE):
  1. Submitting a goal causes `research_node` to call qwen-122b via `http://localhost:4000/v1` (never an mlx port directly) and write non-empty research findings into graph state.
  2. `plan_node` takes the research findings and goal and produces an actionable plan in graph state using qwen-122b — the plan text is visible in the job status response.
  3. A `curl` test generating more than 90 seconds of 122B output completes without a 504 — confirming streaming is enabled end-to-end before any real workload.
  4. After a simulated restart mid-job (process killed after Research completes), the resumed run skips Research and starts from Plan — node attribution (model name per node) is recorded in state.
**Plans**: TBD

Plans:
- [ ] 02-01: graph/llm.py make_llm() factory; research_node and plan_node with ChatOpenAI base_url=localhost:4000/v1; per-node model attribution in state
- [ ] 02-02: LiteLLM streaming validation (curl smoke test >90s); checkpoint resume test; LiteLLM unavailability handling

### Phase 3: OpenHands Execute
**Goal**: The Execute node drives the OpenHands SDK in-process on qwen-35b, never blocks the event loop, writes artifacts to an isolated per-job workspace, enforces a max-iterations cap, and the full Research → Plan → Execute pipeline completes autonomously end-to-end.
**Depends on**: Phase 2
**Requirements**: ORCH-01, EXEC-01, EXEC-02, EXEC-03, EXEC-04, EXEC-05, OBS-02
**Success Criteria** (what must be TRUE):
  1. A submitted goal runs the complete Research → Plan → Execute pipeline to completion with no human approval gate — `GET /jobs/{id}/status` transitions from PLANNING to EXECUTING to DONE automatically.
  2. While a job is in the EXECUTING phase (OpenHands running), `GET /jobs/{id}/status` remains responsive with sub-second latency — the event loop is not blocked.
  3. Each job's artifacts (`research.md`, `plan.md`, `execution_transcript.jsonl`) are written to a per-job workspace directory (`~/projs/langgraph-jobs/<job_id>/workspace/`) — the orchestrator's own source files are never modified.
  4. An Execute run that would otherwise loop indefinitely terminates at the configured max-iterations cap and transitions to DONE (or FAILED) rather than running forever.
  5. Importing the OpenHands SDK occurs only inside `execution/openhands_adapter.py` — the execute_node can be tested with the adapter swapped for a stub without importing `openhands.*`.
**Plans**: TBD

Plans:
- [ ] 03-01: execution/openhands_adapter.py with asyncio.to_thread wrap, per-job workspace_base, max_iterations config, loop-detection logic
- [ ] 03-02: execute_node wired into graph; intermediate artifact writes (research.md, plan.md, execution_transcript.jsonl); SDK error propagation to job status
- [ ] 03-03: End-to-end integration test: submit goal → verify DONE status, workspace files written, event loop responsive during Execute

### Phase 4: Memory Management
**Goal**: Dual-model memory pressure is measured with both models loaded simultaneously, and the 122B model is unloaded between Plan and Execute so 35B throughput is not degraded during the Execute phase.
**Depends on**: Phase 3
**Requirements**: OPS-05
**Success Criteria** (what must be TRUE):
  1. With both 122B and 35B loaded, the measured 35B throughput (tok/s) is recorded as baseline — the degradation from simultaneous loading is documented.
  2. After the Plan node completes, `launchctl kickstart -k gui/501/com.ohama.qwen122b` is issued automatically by the worker runner before Execute starts — the 122B mlx process is no longer resident during the Execute phase.
  3. With 122B unloaded, 35B throughput during Execute is at or above the degraded baseline measured in criterion 1 (target: closer to ~35 tok/s than ~5-8 tok/s).
**Plans**: TBD

Plans:
- [ ] 04-01: Throughput measurement script (35B tok/s with and without 122B loaded); launchctl kickstart -k wired into worker runner between Plan and Execute

### Phase 5: launchd Packaging
**Goal**: The orchestrator runs as a persistent launchd agent that survives reboots, waits for LiteLLM to be ready before accepting work, resumes orphaned in-progress jobs on startup, and flushes checkpoints cleanly on SIGTERM.
**Depends on**: Phase 4
**Requirements**: OPS-01, OPS-02, OPS-03, OPS-04, PERSIST-03
**Success Criteria** (what must be TRUE):
  1. After a machine reboot, the orchestrator service is running and `GET /health` returns 200 without any manual intervention.
  2. If LiteLLM is slow to start (e.g., still loading 35B), the orchestrator does not crash or exit — it retries the `:4000/health` probe for up to 120 seconds before accepting its first job.
  3. A job that was in EXECUTING status at the time of a SIGTERM is re-enqueued on the next process start and resumes from the last completed checkpoint node rather than restarting from scratch.
  4. On SIGTERM, the service completes the current LLM call, flushes the checkpoint, updates job status, and exits cleanly within the launchd ExitTimeout window.
**Plans**: TBD

Plans:
- [ ] 05-01: launchd/com.ohama.orchestrator.plist (RunAtLoad, KeepAlive, ThrottleInterval: 30, ExitTimeOut: 30, absolute .venv/bin/uvicorn path, log paths)
- [ ] 05-02: Lazy LiteLLM connectivity probe (poll :4000/health with retry up to 120s); SIGTERM handler (checkpoint flush + graceful exit); startup resume scan (re-enqueue jobs with status running from jobs.db)

### Phase 6: CLI Client
**Goal**: The operator can submit goals, check status, stream logs, and retrieve results from a terminal without constructing raw curl commands.
**Depends on**: Phase 5
**Requirements**: CLI-01, CLI-02, CLI-03, CLI-04
**Success Criteria** (what must be TRUE):
  1. `orchestrator submit "build a todo app"` prints a job id and exits immediately.
  2. `orchestrator status <job_id>` prints the current status and active node in human-readable form.
  3. `orchestrator logs <job_id>` prints the per-run log file contents for a job.
  4. `orchestrator result <job_id>` prints the execution artifact or workspace path for a completed job.
**Plans**: TBD

Plans:
- [ ] 06-01: CLI entry point with submit / status / logs / result subcommands; human-readable output; error handling for non-existent job ids

## Progress

**Execution Order:** 1 → 2 → 3 → 4 → 5 → 6

| Phase | Plans Complete | Status | Completed |
|-------|----------------|--------|-----------|
| 1. Foundation | 0/3 | Not started | - |
| 2. LLM Nodes | 0/2 | Not started | - |
| 3. OpenHands Execute | 0/3 | Not started | - |
| 4. Memory Management | 0/1 | Not started | - |
| 5. launchd Packaging | 0/2 | Not started | - |
| 6. CLI Client | 0/1 | Not started | - |
