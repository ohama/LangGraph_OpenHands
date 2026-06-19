# Project Research Summary

**Project:** LangGraph_OpenHands — Local Multi-Agent Orchestrator
**Domain:** Local autonomous agent pipeline (Research → Plan → Execute) on macOS
**Researched:** 2026-06-19
**Confidence:** HIGH

## Executive Summary

This project is a single-operator, locally-hosted autonomous agent orchestration service. The architecture is a three-node LangGraph `StateGraph` (Research, Plan, Execute) served by FastAPI over uvicorn, with the entire stack managed by launchd as a persistent macOS service. Research and Plan nodes call the 122B model through the existing LiteLLM proxy at `:4000`; the Execute node delegates to the OpenHands SDK in-process (using the 35B model). The complete deployment runs inside one Python process — no subprocesses, no Docker, no external message brokers.

The recommended approach is to build the stack in strict dependency order: foundation types and persistence first, then the LangGraph graph skeleton with stub nodes, then the FastAPI worker layer, then real LLM nodes, then OpenHands integration, and finally the launchd plist and CLI. This order ensures every phase produces a runnable and testable artifact before the next phase introduces new dependencies. The single most important architectural rule is that `Conversation.run()` from the OpenHands SDK is synchronous and must always be dispatched via `asyncio.to_thread()` or `run_in_executor` — calling it directly inside an `async def` node blocks the entire uvicorn event loop for the duration of the Execute phase (potentially 10–30 minutes), making the service appear hung.

The two highest-severity risks are both operational rather than algorithmic. First, the LiteLLM proxy returns `504 Gateway Timeout` on non-streaming completions that take longer than ~60 seconds, which the 122B research prompt frequently exceeds — the fix is to enable streaming everywhere and verify with a curl test before wiring any graph node. Second, running the 122B and 35B models simultaneously consumes ~84 GB of unified memory on a 96 GB Mac, degrading 35B throughput from ~35 tok/s to ~5–8 tok/s; the mitigation is to issue a `launchctl kickstart -k` to unload 122B between the Plan and Execute phases, exactly as the blueCode v2.3 KV-cache flush pattern does.

---

## Key Findings

### Recommended Stack

The entire stack is Python 3.12 (required by `openhands-sdk>=1.29.0`, which has a hard `>=3.12` floor). LangGraph 1.2.6 provides the `StateGraph` orchestration primitives; `langgraph-checkpoint-sqlite==3.1.0` provides the `AsyncSqliteSaver` for durable, crash-resumable graph state (pin to `>=3.0.1` to avoid CVE-2025-67644 SQL injection). `langchain-openai==1.3.2` gives `ChatOpenAI` with `base_url` support, which routes all LLM calls through the existing LiteLLM proxy on `:4000` using logical model aliases rather than hardcoded mlx port numbers. FastAPI 0.137.2 with uvicorn 0.49.0 provides the HTTP surface. No Celery, no Redis, no Postgres — a single `asyncio.Queue` worker inside the FastAPI lifespan event and two SQLite files (one for LangGraph checkpoints, one for the job registry) cover all persistence needs.

**Core technologies:**
- `langgraph==1.2.6`: StateGraph orchestration, conditional edges, durable superstep checkpointing — the central coordination primitive
- `langgraph-checkpoint-sqlite==3.1.0`: `AsyncSqliteSaver` for crash-resume state persistence — zero-ops for single-machine use; must be >=3.0.1 (CVE-2025-67644)
- `langchain-openai==1.3.2`: `ChatOpenAI` with `base_url="http://localhost:4000/v1"` — routes Research and Plan nodes through LiteLLM proxy
- `openhands-sdk==1.29.0`: In-process `LLM`/`Agent`/`Conversation` for the Execute node — Python >=3.12 required
- `fastapi==0.137.2` + `uvicorn[standard]==0.49.0`: HTTP service layer with lifespan-managed asyncio worker
- `aiosqlite>=0.19`: Async job registry (separate from LangGraph checkpointer)
- `uv`: Venv and package management; launchd plist points at `.venv/bin/uvicorn` with absolute path

**What not to use:** `MemorySaver` (loses state on restart), `FastAPI BackgroundTasks` (not a queue), `Celery`+Redis (over-engineered for one machine), `openhands-ai` (the full server package — use `openhands-sdk`), raw `mlx_lm` Python imports in the orchestrator process (would OOM the machine).

### Expected Features

Research identified 16 P1 (must-have) features for a functional v1, 4 P2 features to add after validation, and a clear set of anti-features to avoid.

**Must have (table stakes — v1):**
- Job submission (`POST /goals`) returning `job_id` immediately with 202 Accepted
- Job status polling (`GET /jobs/{id}/status`) with node-phase granularity (PENDING / RESEARCHING / PLANNING / EXECUTING / DONE / FAILED)
- Result retrieval (`GET /jobs/{id}/result`) returning the execution artifact or path
- Cancel a run (`DELETE /jobs/{id}`) — long runs must be stoppable
- LangGraph SQLite checkpointer wired from the first commit — survive launchd restarts
- Startup resume scan — re-enqueue orphaned `running` jobs on process start
- Graceful SIGTERM handler — finish current LLM call before checkpointing and exiting
- Intermediate artifact storage: `research.md`, `plan.md`, `execution_transcript.jsonl` per job
- Per-run structured log file with timestamps and node transitions
- LiteLLM unavailable / timeout handling with `RetryPolicy` on graph nodes
- OpenHands SDK error propagation — capture and surface Execute node failures
- Health endpoint (`GET /health`) checking FastAPI, SQLite, and LiteLLM liveness
- launchd plist (`com.ohama.orchestrator`) with KeepAlive + RunAtLoad + ThrottleInterval >= 30
- CLI: `submit` / `status` / `logs` / `result` subcommands
- Typed `OrchestratorState` TypedDict (goal, research_findings, plan, execution_result, event_log)
- Per-node model attribution logged to state

**Should have (add after v1 is stable):**
- Structured artifact manifest (`manifest.json`) per job
- OpenTelemetry tracing for the Execute node (nearly free via OpenHands SDK OTEL support)
- `orchestrator status --watch` polling loop in CLI
- Job list endpoint (`GET /jobs`) with pagination

**Defer to v2+:**
- Plan approval gate (human-in-the-loop) — evaluate after seeing autonomous failure modes
- Web UI — CLI is sufficient for a single operator
- Parallel job execution — requires memory headroom that does not currently exist
- Supervisor / parallel sub-agents within a run

**Anti-features (deliberately excluded from v1):** web UI, multi-user auth, parallel job execution, Celery/Redis, ACP/headless-CLI OpenHands handoff, per-job model selection via API, real-time SSE streaming endpoint.

### Architecture Approach

The architecture is a single-process, event-loop-safe pipeline. FastAPI handles HTTP, enqueues job descriptors into an `asyncio.Queue`, and returns immediately. A single worker coroutine (started in the lifespan event) drains the queue, invokes `graph.ainvoke()` per job, and updates the job registry. The LangGraph `StateGraph` runs Research and Plan nodes with fully async `ChatOpenAI.ainvoke()` calls (non-blocking), then the Execute node dispatches `Conversation.run()` into a `ThreadPoolExecutor(max_workers=1)` so the event loop remains free. Two separate SQLite files maintain a clean boundary: `checkpoints.db` (LangGraph-internal, keyed by `thread_id = job_id`) and `jobs.db` (user-visible status, consumed by the REST API). The LiteLLM proxy at `:4000` is the single model endpoint; graph nodes never call mlx ports directly.

**Major components:**
1. **FastAPI API layer** — `POST /goals`, `GET /jobs/{id}`, `GET /health`; enqueues to asyncio.Queue; reads from jobs.db
2. **asyncio.Queue worker** — single coroutine; serializes graph runs; owns job lifecycle transitions
3. **LangGraph StateGraph** — `research_node` (qwen-122b) -> `plan_node` (qwen-122b) -> `execute_node` (OpenHands SDK + qwen-35b); `AsyncSqliteSaver` checkpoints after each node
4. **OpenHands adapter** (`execution/openhands_adapter.py`) — isolated seam; all SDK calls here; `run_in_executor` thread boundary
5. **Job registry** (`jobs.db` via aiosqlite) — user-visible status separate from LangGraph checkpoint internals
6. **LiteLLM proxy (:4000)** — external dependency; routes logical model aliases to mlx ports; never bypassed
7. **launchd** — manages three `com.ohama.*` agents; orchestrator uses retry-on-connection-failure at startup rather than declarative dependency ordering

**Key patterns:**
- `thread_id = job_id` throughout — LangGraph checkpointer and job registry use the same key
- `AsyncSqliteSaver` opened once in lifespan, compiled into the graph at startup — not per-job
- All nodes return only the keys they changed; LangGraph merges partial state dicts
- `event_log: Annotated[list[str], add]` reducer for append-only node event log
- Two SQLite files never merged — LangGraph schema is internal and may change across versions

### Critical Pitfalls

1. **LiteLLM 504 on long non-streaming completions** — 122B research/plan responses frequently exceed 60s; LiteLLM's httpx client fires a timeout at 60s regardless of `request_timeout` config in affected versions. Fix: enable `stream: true` everywhere in LiteLLM config; validate with a curl test generating >90s of output before wiring any graph node.

2. **OpenHands SDK blocks the asyncio event loop** — `Conversation.run()` is synchronous. Calling it directly inside `async def execute_node()` freezes uvicorn for 10–30 minutes; `/status` endpoints go dark; launchd may declare the service unhealthy. Fix: always wrap with `await asyncio.to_thread(_run_openhands_sync, ...)` — unconditionally, from the first commit.

3. **Dual-model memory pressure collapses 35B throughput** — 122B (~62 GB) + 35B (~22 GB) = ~84 GB RSS on a 96 GB Mac; macOS swap compression degrades 35B from 35 tok/s to <8 tok/s. Fix: issue `launchctl kickstart -k gui/501/com.ohama.qwen122b` between Plan and Execute to free 122B memory; the pattern is proven from blueCode v2.3.

4. **OpenHands agent infinite loop with `native_tool_calling: false`** — Qwen 35B produces malformed XML tool-call syntax ~21.7% of the time; the agent re-prompts on each failure, creating a tight loop. Fix: set hard `max_iterations` (15–20) in the OpenHands runtime config; add loop-detection logic in the Execute node adapter.

5. **launchd starts orchestrator before LiteLLM is ready** — all `com.ohama.*` agents start simultaneously at login; LiteLLM takes time to load 35B. Fix: implement lazy connectivity check (probe `:4000/health` with retry, up to 120s); never fail-fast at startup; always set `ThrottleInterval: 30` to bound restart storm rate.

6. **MemorySaver loses all job state on process restart** — the documentation default `MemorySaver` satisfies smoke tests but loses everything on launchd restart. Fix: wire `AsyncSqliteSaver` from the first commit; never use `MemorySaver` outside of test fixtures.

7. **Wrong workspace_base corrupts orchestrator source files** — without explicit per-job workspace config, OpenHands defaults to the process CWD (`~/projs/LangGraph_OpenHands/`); the Execute agent can modify the orchestrator's own files. Fix: set `workspace_base` to a per-job directory (`~/projs/langgraph-jobs/<job_id>/workspace/`) in the adapter; verify with a smoke test.

---

## Implications for Roadmap

The architecture research provides an explicit build order with six dependency-ordered phases. The pitfalls map cleanly to phases where each must be prevented. The recommended phase structure follows the ARCHITECTURE.md "Suggested Build Order" almost exactly, with pitfall-prevention checkpoints added at each gate.

### Phase 1: Foundation — Types, Persistence, and Service Scaffold

**Rationale:** Everything else depends on the state schema, the job registry, and the FastAPI/worker skeleton. Getting these right first means every subsequent phase has a stable, testable base. This is also where the two most dangerous defaults are eliminated: `MemorySaver` and the event loop blocking pattern.

**Delivers:** `OrchestratorState` TypedDict, `jobs.db` job registry (aiosqlite CRUD), FastAPI app with lifespan, `asyncio.Queue` worker wired but running stub graph, `AsyncSqliteSaver` checkpointer compiled into graph from day one, SIGTERM handler skeleton, health endpoint returning 200.

**Addresses:** Job submission, job status polling, health endpoint, launchd plist scaffold, per-run log file setup.

**Avoids:**
- Pitfall 7 (MemorySaver): `AsyncSqliteSaver` wired in this phase, not patched in later
- Pitfall 2 (event loop blocking): `asyncio.to_thread` wrapper established as the execute_node contract before any SDK integration
- Pitfall 4 (state bloat): TypedDict uses typed string fields (`research_findings: str`, `plan: str`) not accumulating `messages` lists

**Research flag:** Standard patterns — no additional research needed.

---

### Phase 2: LangGraph Graph Skeleton with Real LLM Nodes

**Rationale:** The graph structure and LLM routing must be validated against the live LiteLLM proxy before OpenHands is added. Stub nodes in Phase 1 become real `ChatOpenAI` nodes here, and the LiteLLM 504 pitfall is caught and fixed before it can block Execute integration.

**Delivers:** `graph/llm.py` with `make_llm()` factory, real `research_node` and `plan_node` calling qwen-122b via LiteLLM :4000, end-to-end `graph.ainvoke()` with live models, checkpoint persistence validated across a simulated restart, LiteLLM timeout fix verified with a long-generation curl test.

**Addresses:** LangGraph stateful TypedDict state, per-node model attribution, node-aware status in `/status` response, LiteLLM unavailable handling.

**Avoids:**
- Pitfall 1 (LiteLLM 504): Streaming enabled and verified before any real workload
- Pitfall 8 (vague 122B plans): Plan node system prompt enforces strict schema (specific files, actions, done-checks)
- Pitfall 4 (state bloat): Checkpoint size verified flat across 3 sequential test jobs

**Research flag:** Standard patterns. STACK.md provides exact code shapes. No additional research needed.

---

### Phase 3: OpenHands Execute Node Integration

**Rationale:** OpenHands is the riskiest integration — blocking SDK, XML tool-calling fragility, workspace isolation, condenser resource contention, and version mismatch all land here. Isolating it to its own phase after the graph and LLM nodes are proven means failures are attributable to the SDK, not to an unstable foundation.

**Delivers:** `execution/openhands_adapter.py` with `ThreadPoolExecutor(max_workers=1)`, real `execute_node` calling the adapter, per-job workspace isolation (`~/projs/langgraph-jobs/<job_id>/workspace/`), `max_iterations` enforced in SDK config, plan-validation logic (rejects vague steps before Execute runs), condenser pointed at qwen-122b (not 35B), intermediate artifact storage (research.md, plan.md, execution_transcript.jsonl).

**Addresses:** Execute node with OpenHands SDK, execution transcript capture, OpenHands SDK error propagation, cancel a run, result retrieval.

**Avoids:**
- Pitfall 2 (event loop blocking): `asyncio.to_thread` wrapper verified under load
- Pitfall 3 (OpenHands infinite loop): `max_iterations` set; loop-detection added in adapter
- Pitfall 9 (wrong workspace_base): Per-job workspace directory enforced; smoke test verifies file writes land correctly
- Pitfall 10 (destructive Execute): Plan pre-validation step checks for `rm -rf`, `git reset`, out-of-workspace paths
- Pitfall 11 (condenser contention): Condenser pointed at qwen-122b; verified no timeout during 30-step Execute run
- Pitfall 12 (SDK version mismatch): SDK version pinned; test constructs SDK config from agent_settings.json and asserts no unknown-field warnings

**Research flag:** Needs deeper research on exact OpenHands SDK v1.29.0 config API — specifically `workspace_base` parameter name and `max_iterations` config key (may differ between CLI JSON config and SDK Python config class). Run `/gsd:research-phase` scoped to OpenHands SDK config API if adapter integration is blocked.

---

### Phase 4: Memory Management — Dual-Model Unload Strategy

**Rationale:** The memory pressure pitfall only manifests when both models are loaded simultaneously during a real end-to-end run. It must be measured after Phase 3 produces a real Execute node. The fix modifies the worker runner, not the graph nodes.

**Delivers:** Throughput measurement (35B tok/s with and without 122B loaded), decision on kickstart strategy, `launchctl kickstart -k` call wired into worker runner between Plan completion and Execute start (if throughput degradation is unacceptable), `memory_pressure` CLI monitoring in integration tests.

**Avoids:**
- Pitfall 6 (dual-model memory pressure): Measured and mitigated in this dedicated phase

**Research flag:** Standard pattern — blueCode v2.3 already established the `launchctl kickstart -k` unload pattern on this machine. No additional research needed.

---

### Phase 5: launchd Service Packaging and Startup Hardening

**Rationale:** launchd wiring comes after the orchestrator is end-to-end functional, so the plist deploys a known-working service rather than debugging service behavior and plist config simultaneously. Startup hardening (lazy LiteLLM probe, ThrottleInterval, SIGTERM handler) only matters in the deployed service.

**Delivers:** `launchd/com.ohama.orchestrator.plist` following existing `com.ohama.*` convention (RunAtLoad, KeepAlive, ThrottleInterval: 30, absolute `.venv/bin/uvicorn` path, log paths), lazy LiteLLM connectivity check with retry (poll `:4000/health` up to 120s), startup resume scan (re-enqueue jobs with status `running` from jobs.db on process start), `ExitTimeOut: 30` for graceful SIGTERM, plist loaded and verified to survive reboot.

**Addresses:** launchd plist, startup resume scan, graceful SIGTERM handler, persist run state across restarts, resume after restart.

**Avoids:**
- Pitfall 5 (launchd startup before LiteLLM ready): Lazy probe with retry; never fail-fast at startup
- Pitfall 5 (restart storm): ThrottleInterval: 30 in plist

**Research flag:** Standard patterns — STACK.md and ARCHITECTURE.md provide exact plist XML and startup probe code. No additional research needed.

---

### Phase 6: CLI and Operator Tooling

**Rationale:** CLI wraps the REST API and belongs last. It is low-risk and can be done in parallel with Phase 5 if needed.

**Delivers:** `cli/submit.py` with `submit` / `status` / `logs` / `result` subcommands, human-readable output, `--watch` polling loop for long runs.

**Addresses:** CLI submit / status / logs / result (P1), CLI --watch polling (P2).

**Research flag:** Standard patterns — no additional research needed.

---

### Phase Ordering Rationale

- Foundation before graph before worker before SDK: Each layer depends on the one below. The most common mistake in agent pipeline projects is wiring the full stack in one phase and being unable to isolate failures.
- Pitfalls are phase-gated, not deferred: Every critical pitfall from PITFALLS.md is assigned to a specific phase where it must be verified before that phase is closed. MemorySaver and event-loop blocking are eliminated in Phase 1, not discovered in Phase 5.
- OpenHands isolation in Phase 3: The SDK is the most volatile dependency. Isolating it after the graph and LLM nodes are proven means the failure surface is bounded.
- Memory management after Execute integration: You cannot measure dual-model memory pressure before the Execute node exists.
- launchd after full stack is proven: Never debug service behavior and plist config simultaneously.

### Research Flags

Phases likely needing deeper research during planning:
- **Phase 3 (OpenHands Execute):** The exact Python API surface of `openhands-sdk==1.29.0` for `workspace_base`, `max_iterations`, and SDK config class field names vs CLI `agent_settings.json` field names needs verification against the installed SDK before writing the adapter. If initial integration is blocked, run `/gsd:research-phase` scoped to the OpenHands SDK config API.

Phases with standard patterns (skip additional research):
- **Phase 1 (Foundation):** LangGraph TypedDict state, FastAPI lifespan, aiosqlite CRUD — all well-documented with exact code in STACK.md and ARCHITECTURE.md.
- **Phase 2 (LLM Nodes):** `ChatOpenAI` with `base_url` and LangGraph graph wiring — exact code shapes provided in STACK.md.
- **Phase 4 (Memory Management):** blueCode v2.3 `launchctl kickstart -k` pattern already validated on this machine.
- **Phase 5 (launchd):** Exact plist XML and startup probe pattern in STACK.md and ARCHITECTURE.md.
- **Phase 6 (CLI):** Thin wrapper around REST API.

---

## Confidence Assessment

| Area | Confidence | Notes |
|------|------------|-------|
| Stack | HIGH | All versions verified against PyPI as of 2026-06-19; Python 3.12 constraint confirmed; existing `agent_settings.json` and launchd plists on this machine confirm LiteLLM routing patterns are live ground truth |
| Features | HIGH | Feature set derived from both general agent pipeline patterns and project-specific constraints (single operator, local models, launchd); anti-features are well-reasoned and internally consistent |
| Architecture | HIGH | Multiple corroborating sources; architecture is a standard FastAPI + LangGraph pattern with well-documented async concerns; the specific `run_in_executor` pattern for blocking SDKs is widely verified |
| Pitfalls | HIGH | Most pitfalls are directly observable from the blueCode project history on this machine; LiteLLM 504 and OpenHands XML SyntaxError rate sourced from active GitHub issues and arXiv paper respectively |

**Overall confidence:** HIGH

### Gaps to Address

- **OpenHands SDK v1.29.0 exact config API:** PITFALLS.md identifies risk that `agent_settings.json` CLI field names may not match the SDK Python config class field names at v1.29.0 (CLI was written against v1.16/v1.21). During Phase 3, verify by constructing the SDK config object from the JSON and asserting no unknown-field warnings before writing the adapter. If field names differ, the adapter must use the Python API directly rather than loading the JSON.
- **`conversation.run()` return value API in v1.29.0:** STACK.md notes the exact API for extracting result from `conversation.state.last_output` is version-dependent. Verify the correct attribute name against v1.29.0 SDK source or docs during Phase 3 adapter development.
- **LiteLLM streaming with `ChatOpenAI.ainvoke()`:** PITFALLS.md recommends enabling `stream: true` on LiteLLM side to avoid 504s. Validate during Phase 2 that `ChatOpenAI.ainvoke()` (not `astream()`) in LangGraph nodes receives a complete response object when streaming is enabled upstream.

---

## Sources

### Primary (HIGH confidence)

- langgraph PyPI v1.2.6 (2026-06-19) — version, API surface
- langgraph-checkpoint-sqlite PyPI v3.1.0 (2026-06-19) — version, CVE-2025-67644 patch confirmation
- CVE-2025-67644 — Check Point Research 2026: SQL injection in LangGraph checkpointer thread_id parameter, patched in 3.0.1
- langchain-openai PyPI v1.3.2 (2026-06-19) — version, `base_url` constructor param
- openhands-sdk PyPI v1.29.0 (2026-06-19) — version, Python >=3.12 requirement
- OpenHands SDK getting-started docs — `LLM`/`Agent`/`Conversation` import surface
- OpenHands local LLMs docs — `"openai/"` prefix model naming pattern for LiteLLM routing
- `~/.openhands/agent_settings.json` on this machine — `"model": "openai/qwen-35b"`, `"base_url": "http://localhost:4000/v1"` (live ground truth)
- `~/Library/LaunchAgents/com.ohama.qwen122b.plist` on this machine — ThrottleInterval, StandardOutPath, EnvironmentVariables structure (live ground truth)
- `~/Library/LaunchAgents/com.ohama.litellm.plist` on this machine — venv/bin absolute path pattern (live ground truth)
- fastapi PyPI v0.137.2, uvicorn PyPI v0.49.0 — versions confirmed
- AsyncSqliteSaver API reference (LangChain) — `from_conn_string`, compile pattern
- OpenHands SDK paper (arXiv:2511.03690) — ConversationState, threading model, XML SyntaxError rate (~21.7%)
- LiteLLM 504 on non-streaming (GitHub issue #9551) — confirmed bug, streaming workaround
- OpenHands native_tool_calling semantics (GitHub issue #8424) — confirmed XML fragility with smaller models
- blueCode v2.3 KV-cache / kickstart pattern — confirmed on this machine (live ground truth)
- LangGraph cancellation mid-node limitation (GitHub issue #5672) — checkpoint not guaranteed mid-node

### Secondary (MEDIUM confidence)

- LangGraph SQLite vs Postgres (Fast.io 2026) — SQLite appropriate for single-machine local deployment
- LangGraph state management undocumented issues (altersquare.io) — state bloat patterns
- mlx-lm dual-model memory (insiderllm.com) — RAM estimates for 122B and 35B models

### Tertiary (LOW confidence — needs validation during implementation)

- OpenHands SDK v1.29.0 Python config class field names vs CLI `agent_settings.json` field names — needs direct verification against SDK source
- Exact `conversation.state.last_output` attribute name in openhands-sdk v1.29.0 — needs verification against SDK source or docs

---
*Research completed: 2026-06-19*
*Ready for roadmap: yes*
