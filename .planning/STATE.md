# Project State

## Project Reference

See: .planning/PROJECT.md (updated 2026-06-19)

**Core value:** A goal submitted to an always-on local service is autonomously Researched → Planned → Executed end-to-end by local agents, surviving reboots.
**Current focus:** Phase 2 — LLM Nodes

## Current Position

Phase: 2 of 6 (LLM Nodes)
Plan: 2 of 2 in current phase (all executed; live checkpoint approved)
Status: Plans complete — pending phase verification
Last activity: 2026-06-23 — Executed 02-01 (real nodes, offline split) + 02-02 (live proofs); criterion 3 (144s, no 504) & criterion 4 (resume skips research, node_models survives) PASSED; 20 tests green

Progress: [████░░░░░░] 42% (5/12 plans)

## Performance Metrics

**Velocity:**
- Total plans completed: 3
- Average duration: ~5 min (excl. post-run verification debugging)
- Total execution time: ~0.25 hours

**By Phase:**

| Phase | Plans | Total | Avg/Plan |
|-------|-------|-------|----------|
| 1. Foundation | 3/3 | ~15 min | 5 min |

**Recent Trend:**
- Last 5 plans: 01-01 (5 min), 01-02 (5 min), 01-03 (5 min + verify debug)
- Trend: Stable

*Updated after each plan completion*

## Accumulated Context

### Decisions

Decisions are logged in PROJECT.md Key Decisions table.
Recent decisions affecting current work:

- Roadmap: AsyncSqliteSaver wired in Phase 1 (never MemorySaver, even in smoke tests)
- Roadmap: asyncio.to_thread wrapper established as the execute_node contract in Phase 3 (not Phase 1, but its necessity is documented from day one)
- Roadmap: OPS-05 (122B unload) isolated to Phase 4 — cannot measure dual-model pressure before Execute node exists
- Roadmap: launchd plist deferred to Phase 5 — deploy a known-working service, not debug plist and behavior simultaneously
- 01-01: EMPIRICAL — Resume with None skips completed nodes; resume with initial_state re-runs all nodes. Phase 5 PERSIST-03 must use None.
- 01-01: EMPIRICAL — astream(stream_mode="updates") chunks are {node_name_str: partial_state_dict}; no namespace wrapping. 01-02 worker loop confirmed safe.
- 01-01: aiosqlite.connect() must be used as async context manager directly, not pre-awaited then re-entered (thread-reuse RuntimeError).
- 01-02: FastAPI lifespan yield MUST be inside async with AsyncSqliteSaver.from_conn_string() — proven structurally via inspect.getsource index assertion.
- 01-02: asyncio.Queue worker is the ONLY graph invocation path; never FastAPI BackgroundTasks.
- 01-02: _get_job_logger propagate=False is critical (OBS-01); prevents duplication to uvicorn root logger.
- 01-02: On asyncio.CancelledError in worker, reset job to PENDING so Phase 5 can re-enqueue on restart.
- 01-03: REST contract — POST /goals 202; status/result read ONLY from jobs.db (never checkpoint); cancel returns mid-node caveat string; request_cancel imported lazily in DELETE to avoid import cycle.
- 01-03: OPERATIONAL — start the service via the venv uvicorn binary directly (.venv/bin/uvicorn), NEVER `uv run uvicorn`. `uv run` spawns uvicorn as a child; killing the wrapper orphans the real server, which keeps holding the port and answers stale requests. Phase 5 launchd must point ProgramArguments at the absolute .venv/bin/uvicorn path. Tooling that stops the service must kill the real process/process-group.
- 02-01: make_llm() reads LITELLM_BASE_URL + RESEARCH_MODEL/PLAN_MODEL from env (ORCH-05); streaming=False; max_retries=0; api_key="dummy". Never instantiated at module level.
- 02-01: node_models Annotated[dict, _merge_dicts] is BOUNDED (3 entries max); seeded as {} in initial_state (never None); OBS-03 / ORCH-04 compliant.
- 02-01: build_test_graph() (all-stub, offline) vs build_stub_graph() (real LLM nodes); ORCHESTRATOR_TEST_GRAPH=1 env flag selects test graph in main.py lifespan. Phase 3 tests MUST set this flag.
- 02-01: aget_state() enrichment in GET /jobs/{id}/result is best-effort (exception swallowed); result from jobs.db is the authoritative contract.
- 02-02: EMPIRICAL — RESOLVED the 504 question. LiteLLM 1.86.1 has NO 504 on long non-streaming calls (proved 144s/6000-token generation, HTTP 200). ChatOpenAI.ainvoke() returns a COMPLETE response regardless of streaming flag. Use streaming=False.
- 02-02: EMPIRICAL — node_models attribution {research:qwen-122b, plan:qwen-122b} survives AsyncSqliteSaver reopen; resume via astream(None) runs plan+execute only (research NOT re-called) with real data.
- 02-02: B4 — the inline resume must run with the live service STOPPED (single writer on checkpoints.db); two AsyncSqliteSaver writers on one file → lock/hang. Automatic startup resume is Phase 5 (PERSIST-03).
- 02-02: dead-LiteLLM path proven clean FAILED (APIConnectionError→RuntimeError→set_failed, <1s, max_retries=0, no hang). Phase 5 OPS-03 lazy probe builds on this.
- 02-02: PORT — orchestrator must NOT use :8000 (qwen-35b), :8001 (qwen-122b), :4000 (LiteLLM). resume_real_data.sh used :8080; verify_phase1.sh used :8099. Phase 5 plist must pick a stable non-colliding port.

### Pending Todos

None yet.

### Blockers/Concerns

- Phase 3 research flag: OpenHands SDK v1.29.0 exact Python config class field names (workspace_base, max_iterations) need verification against SDK source before writing the adapter. Run /gsd:research-phase scoped to OpenHands SDK config API if Phase 3 planning is blocked.
- ENV HYGIENE: orphaned uvicorn processes from interrupted runs can linger on a port and answer stale requests (caused a confusing intermittent 500 during 01-03 verification, and qwen-122b can wedge with a metal::malloc error — respawn via `launchctl kickstart -k gui/501/com.ohama.qwen122b`). When debugging the service, `pkill -9 -f "uvicorn orchestrator.main:app"` between runs and prefer launching `.venv/bin/uvicorn` directly.
- RESOLVED (was Phase 2 validation): ChatOpenAI.ainvoke() DOES return a complete response with LiteLLM upstream; no streaming needed (see 02-02 empirical above).

## Session Continuity

Last session: 2026-06-23
Stopped at: Completed 02-01-PLAN.md; 18 tests green offline; ready for 02-02 (live qwen-122b proof)
Resume file: None
