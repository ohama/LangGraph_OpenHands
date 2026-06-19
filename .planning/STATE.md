# Project State

## Project Reference

See: .planning/PROJECT.md (updated 2026-06-19)

**Core value:** A goal submitted to an always-on local service is autonomously Researched → Planned → Executed end-to-end by local agents, surviving reboots.
**Current focus:** Phase 1 — Foundation

## Current Position

Phase: 1 of 6 (Foundation)
Plan: 3 of 3 in current phase (all executed)
Status: Plans complete — pending phase verification
Last activity: 2026-06-19 — Completed 01-03-PLAN.md; verify_phase1.sh PASSED (all 5 criteria incl. kill+restart); 18 tests green

Progress: [██░░░░░░░░] 25% (3/12 plans)

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

### Pending Todos

None yet.

### Blockers/Concerns

- Phase 3 research flag: OpenHands SDK v1.29.0 exact Python config class field names (workspace_base, max_iterations) need verification against SDK source before writing the adapter. Run /gsd:research-phase scoped to OpenHands SDK config API if Phase 3 planning is blocked.
- Phase 2 validation: Confirm ChatOpenAI.ainvoke() receives a complete response object when streaming is enabled upstream in LiteLLM (not just astream()).
- ENV HYGIENE: orphaned uvicorn processes from interrupted runs can linger on a port and answer stale requests (caused a confusing intermittent 500 during 01-03 verification). When debugging the service, `pkill -9 -f "uvicorn orchestrator.main:app"` between runs and prefer launching `.venv/bin/uvicorn` directly so PIDs are killable.

## Session Continuity

Last session: 2026-06-19
Stopped at: Phase 1 plans 01-01/01-02/01-03 all complete and committed; verify_phase1.sh PASSED. Ready for phase verification (gsd-verifier) then phase-completion bookkeeping.
Resume file: None
