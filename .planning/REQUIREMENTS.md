# Requirements: LangGraph + OpenHands Orchestrator

**Defined:** 2026-06-19
**Core Value:** A goal submitted to an always-on local service is autonomously Researched → Planned → Executed end-to-end by local agents, surviving reboots.

## v1 Requirements

Requirements for initial release. Each maps to roadmap phases.

### Orchestration (ORCH)

- [ ] **ORCH-01**: Operator can submit a goal and the system runs a linear Research → Plan → Execute LangGraph graph to completion autonomously (no approval gate)
- [ ] **ORCH-02**: Research node produces research findings using qwen-122b via LiteLLM (`:4000`)
- [ ] **ORCH-03**: Plan node produces an actionable plan from goal + research using qwen-122b
- [ ] **ORCH-04**: Graph state carries typed fields (goal, research, plan, execution result/status) between nodes without unbounded message accumulation
- [ ] **ORCH-05**: All LLM calls route through LiteLLM `:4000`; no node talks to an mlx port directly

### Execution via OpenHands (EXEC)

- [ ] **EXEC-01**: Execute node drives OpenHands in-process via the Python SDK (`Conversation.run()`) on qwen-35b
- [ ] **EXEC-02**: The blocking OpenHands run is wrapped (`asyncio.to_thread` / executor) so it never blocks the service event loop
- [ ] **EXEC-03**: Each job runs in its own isolated workspace directory
- [ ] **EXEC-04**: Execute node enforces a max-iterations cap to prevent unbounded agent loops
- [ ] **EXEC-05**: OpenHands LLM access is isolated to a single adapter module (only file importing `openhands.*`), so the node is stubbable

### Persistence & Resume (PERSIST)

- [ ] **PERSIST-01**: Graph state is checkpointed per node via an SQLite checkpointer (keyed by job id)
- [ ] **PERSIST-02**: A separate SQLite job registry tracks id / goal / status / result / timestamps
- [ ] **PERSIST-03**: On service restart, in-progress jobs are re-enqueued and resume from the last completed node (Research/Plan not re-run if already done)

### Service API (API)

- [ ] **API-01**: Operator can submit a goal via `POST` and receive a job id
- [ ] **API-02**: Operator can query job status via REST
- [ ] **API-03**: Operator can retrieve a finished job's result/artifacts via REST
- [ ] **API-04**: Operator can cancel a running job via REST (app-level status set to cancelled; checkpoint-consistency caveat documented)
- [ ] **API-05**: Service exposes a health endpoint
- [ ] **API-06**: Long-running graph runs execute via an in-process asyncio.Queue worker without blocking API requests

### CLI Client (CLI)

- [ ] **CLI-01**: `submit` command sends a goal to the service and prints the job id
- [ ] **CLI-02**: `status` command shows a job's current status
- [ ] **CLI-03**: `logs` command streams/prints a job's run logs
- [ ] **CLI-04**: `result` command prints a finished job's result/artifacts

### Service Ops & Reboot Survival (OPS)

- [ ] **OPS-01**: Orchestrator runs as a launchd agent (`com.ohama.orchestrator`) with RunAtLoad + KeepAlive + ThrottleInterval, mirroring the existing `com.ohama.*` pattern
- [ ] **OPS-02**: Service auto-starts and survives reboot
- [ ] **OPS-03**: Service waits for LiteLLM `:4000` via a lazy connectivity probe (retry) instead of failing fast at startup
- [ ] **OPS-04**: Service handles SIGTERM gracefully (checkpoint flush) before launchd kills it
- [ ] **OPS-05**: Before the Execute step, 122B memory is flushed/unloaded (e.g. `launchctl kickstart -k`) to relieve memory pressure when both models would otherwise be resident

### Observability (OBS)

- [ ] **OBS-01**: Each run writes a per-run log file
- [ ] **OBS-02**: Intermediate artifacts (research notes, plan, execution transcript) are persisted to disk per job
- [ ] **OBS-03**: Run records which model was used at each node

## v2 Requirements

Deferred to future release. Tracked but not in current roadmap.

### Observability

- **OTEL-01**: OpenHands OTEL tracing (per-iteration / tool-call / LLM-call spans) on the Execute node
- **OTEL-02**: A collector + viewer for traces

### Orchestration

- **PAR-01**: Parallel / supervisor multi-agent execution of multiple plans
- **GATE-01**: Optional human-in-the-loop plan-approval gate (per-job toggle)

## Out of Scope

Explicitly excluded. Documented to prevent scope creep.

| Feature | Reason |
|---------|--------|
| Human plan-approval gate | Chose fully autonomous for v1; deferred to v2 (GATE-01) |
| Parallel / supervisor execution | v1 is a single linear pipeline; deferred to v2 (PAR-01) |
| Web UI | REST API + CLI sufficient for single operator |
| ACP / headless-CLI handoff to OpenHands | Chose in-process Python SDK for tightest coupling/debugging |
| Multi-user / auth / remote exposure | Single local operator, localhost-only |
| Celery / Redis / external broker | asyncio.Queue in-process is sufficient on one machine |
| Per-job model selection | Fixed roles (122B research/plan, 35B execute) for v1 |
| Building/serving the LLMs | mlx + LiteLLM stack pre-exists, owned separately |

## Traceability

Which phases cover which requirements. Updated during roadmap creation.

| Requirement | Phase | Status |
|-------------|-------|--------|
| ORCH-01 | Phase 3 — OpenHands Execute | Pending |
| ORCH-02 | Phase 2 — LLM Nodes | Pending |
| ORCH-03 | Phase 2 — LLM Nodes | Pending |
| ORCH-04 | Phase 1 — Foundation | Complete |
| ORCH-05 | Phase 2 — LLM Nodes | Pending |
| EXEC-01 | Phase 3 — OpenHands Execute | Pending |
| EXEC-02 | Phase 3 — OpenHands Execute | Pending |
| EXEC-03 | Phase 3 — OpenHands Execute | Pending |
| EXEC-04 | Phase 3 — OpenHands Execute | Pending |
| EXEC-05 | Phase 3 — OpenHands Execute | Pending |
| PERSIST-01 | Phase 1 — Foundation | Complete |
| PERSIST-02 | Phase 1 — Foundation | Complete |
| PERSIST-03 | Phase 5 — launchd Packaging | Pending |
| API-01 | Phase 1 — Foundation | Complete |
| API-02 | Phase 1 — Foundation | Complete |
| API-03 | Phase 1 — Foundation | Complete |
| API-04 | Phase 1 — Foundation | Complete |
| API-05 | Phase 1 — Foundation | Complete |
| API-06 | Phase 1 — Foundation | Complete |
| CLI-01 | Phase 6 — CLI Client | Pending |
| CLI-02 | Phase 6 — CLI Client | Pending |
| CLI-03 | Phase 6 — CLI Client | Pending |
| CLI-04 | Phase 6 — CLI Client | Pending |
| OPS-01 | Phase 5 — launchd Packaging | Pending |
| OPS-02 | Phase 5 — launchd Packaging | Pending |
| OPS-03 | Phase 5 — launchd Packaging | Pending |
| OPS-04 | Phase 5 — launchd Packaging | Pending |
| OPS-05 | Phase 4 — Memory Management | Pending |
| OBS-01 | Phase 1 — Foundation | Complete |
| OBS-02 | Phase 3 — OpenHands Execute | Pending |
| OBS-03 | Phase 2 — LLM Nodes | Pending |

**Coverage:**
- v1 requirements: 31 total (5 ORCH + 5 EXEC + 3 PERSIST + 6 API + 4 CLI + 5 OPS + 3 OBS)
- Mapped to phases: 31
- Unmapped: 0 ✓

**Note:** The orchestrator file header listed 27 but the actual requirement count is 31 (the API and OPS categories each have more entries than the header implied). All 31 are mapped.

---
*Requirements defined: 2026-06-19*
*Last updated: 2026-06-19 — traceability populated by roadmapper*
