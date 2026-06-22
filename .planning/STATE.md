# Project State

## Project Reference

See: .planning/PROJECT.md (updated 2026-06-19)

**Core value:** A goal submitted to an always-on local service is autonomously Researched → Planned → Executed end-to-end by local agents, surviving reboots.
**Current focus:** Phase 1 — Foundation

## Current Position

Phase: 2 of 6 (LLM Nodes)
Plan: 1 of 2 in current phase
Status: In progress
Last activity: 2026-06-23 — Completed 02-01-PLAN.md; real LLM nodes + offline test split + result API enrichment; 18 tests green

Progress: [███░░░░░░░] 33% (4/12 plans)

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

### Pending Todos

None yet.

### Blockers/Concerns

- Phase 3 research flag: OpenHands SDK v1.29.0 exact Python config class field names (workspace_base, max_iterations) need verification against SDK source before writing the adapter. Run /gsd:research-phase scoped to OpenHands SDK config API if Phase 3 planning is blocked.
- Phase 2 validation: Confirm ChatOpenAI.ainvoke() receives a complete response object when streaming is enabled upstream in LiteLLM (not just astream()).
- ENV HYGIENE: orphaned uvicorn processes from interrupted runs can linger on a port and answer stale requests (caused a confusing intermittent 500 during 01-03 verification). When debugging the service, `pkill -9 -f "uvicorn orchestrator.main:app"` between runs and prefer launching `.venv/bin/uvicorn` directly so PIDs are killable.

## Session Continuity

Last session: 2026-06-23
Stopped at: Completed 02-01-PLAN.md; 18 tests green offline; ready for 02-02 (live qwen-122b proof)
Resume file: None
