---
phase: 01-foundation
plan: "01"
subsystem: database
tags: [langgraph, asyncsqlitesaver, aiosqlite, sqlite, pydantic, fastapi, pytest-asyncio, checkpoint, persistence]

# Dependency graph
requires: []
provides:
  - OrchestratorState TypedDict (goal/job_id/research_findings/plan/execution_result/execution_status/event_log)
  - build_stub_graph() with nodes research/plan/execute wired to END; returns uncompiled builder
  - JobStore + init_jobs_db for WAL-mode jobs.db CRUD
  - pyproject.toml with all Phase 1 pinned dependencies
  - Empirically verified: resume input pattern (None) and astream chunk key format
affects: [01-02, 01-03, 02-01, 02-02, 05-02]

# Tech tracking
tech-stack:
  added:
    - langgraph==1.2.6
    - langgraph-checkpoint-sqlite==3.1.0
    - fastapi==0.137.2
    - uvicorn[standard]==0.49.0
    - aiosqlite>=0.19 (resolved 0.22.1)
    - pydantic>=2.7 (resolved 2.13.4)
    - python-dotenv>=1.0 (resolved 1.2.2)
    - pytest>=8.0 (resolved 9.1.0)
    - pytest-asyncio>=0.24 (resolved 1.4.0)
    - ruff
  patterns:
    - OrchestratorState as flat TypedDict (no messages accumulator) per ORCH-04
    - build_stub_graph() returns uncompiled StateGraph; compiled with checkpointer in lifespan/tests
    - aiosqlite.connect() used as async context manager directly (not pre-awaited)
    - WAL mode set on every aiosqlite connection open
    - pytest-asyncio with asyncio_mode=auto (no per-test @pytest.mark.asyncio)

key-files:
  created:
    - pyproject.toml
    - .env
    - .gitignore
    - orchestrator/__init__.py
    - orchestrator/graph/__init__.py
    - orchestrator/graph/state.py
    - orchestrator/graph/stub_graph.py
    - orchestrator/persistence/__init__.py
    - orchestrator/persistence/job_store.py
    - tests/__init__.py
    - tests/test_job_store.py
    - tests/test_stub_graph.py
    - uv.lock
  modified: []

key-decisions:
  - "aiosqlite.connect() must be used as async context manager directly — pre-awaiting then re-entering causes thread-reuse RuntimeError"
  - "EMPIRICAL: Resume with None yields 0 new chunks on a completed graph (nodes NOT re-run); resume with initial_state re-runs all 3 nodes. Use None for Phase 5 PERSIST-03."
  - "EMPIRICAL: astream(stream_mode='updates') chunks are plain dicts: key = node name string (e.g. 'research'), value = partial state dict. No namespace wrapping."
  - "Comments in state.py must not contain the word 'messages' — ORCH-04 guard does a text search of the entire module source"

patterns-established:
  - "Pattern: Use 'async with aiosqlite.connect(path) as conn:' not 'async with await store._connect() as conn:'"
  - "Pattern: Compile graph with checkpointer in tests using 'build_stub_graph().compile(checkpointer=saver)'"
  - "Pattern: OrchestratorState event_log uses Annotated[list[str], operator.add] for append-only semantics"

# Metrics
duration: 5min
completed: 2026-06-19
---

# Phase 1 Plan 01: Data Foundation Summary

**OrchestratorState TypedDict + AsyncSqliteSaver stub graph (research/plan/execute) + WAL-mode aiosqlite JobStore; checkpoint persistence (PERSIST-01) and jobs.db CRUD (PERSIST-02) proven, with two empirical LangGraph 1.2.6 behaviors recorded**

## Performance

- **Duration:** 5 min
- **Started:** 2026-06-19T06:01:01Z
- **Completed:** 2026-06-19T06:06:04Z
- **Tasks:** 3 of 3
- **Files modified:** 13 created, 1 modified (job_store.py bug fix)

## Accomplishments

- Greenfield Python 3.12 project wired with all Phase 1 pinned deps via uv; langgraph==1.2.6 confirmed
- OrchestratorState TypedDict with 6 typed fields + append-only event_log; ORCH-04 compliant (no unbounded accumulator)
- build_stub_graph() with real node names (research/plan/execute) returning uncompiled StateGraph; compiles with AsyncSqliteSaver
- JobStore + init_jobs_db with WAL mode on every connection; full CRUD round-trips verified (PERSIST-02)
- Checkpoint survives AsyncSqliteSaver reopen on same db file (PERSIST-01 proven)
- Two empirical LangGraph 1.2.6 behaviors resolved: resume-input pattern and astream chunk key format

## Task Commits

Each task was committed atomically:

1. **Task 1: Project scaffold + pinned dependencies** - `d0fce55` (chore)
2. **Task 2: OrchestratorState + stub graph + JobStore** - `9e23eb8` (feat)
3. **Task 3: Tests — checkpoint persistence + two empirical verifications** - `3b51572` (test)

**Plan metadata:** (docs commit — see below)

## Files Created/Modified

- `pyproject.toml` - Project with all 7 pinned runtime deps + dev deps; asyncio_mode=auto
- `.env` - DATA_DIR=./data, LOG_DIR=./logs
- `.gitignore` - Excludes .venv/, data/, logs/, __pycache__/, .pytest_cache/
- `orchestrator/__init__.py` - Package root
- `orchestrator/graph/__init__.py` - Graph sub-package
- `orchestrator/graph/state.py` - OrchestratorState TypedDict (ORCH-04 compliant)
- `orchestrator/graph/stub_graph.py` - build_stub_graph() with 3 stub nodes + edges
- `orchestrator/persistence/__init__.py` - Persistence sub-package
- `orchestrator/persistence/job_store.py` - init_jobs_db + JobStore with WAL CRUD
- `tests/__init__.py` - Tests package
- `tests/test_job_store.py` - 7 CRUD tests including WAL mode verification
- `tests/test_stub_graph.py` - PERSIST-01 + 2 empirical verification tests
- `uv.lock` - Lockfile with 52 resolved packages

## Empirical Findings

**CRITICAL: Both findings are required by 01-02 and Phase 5 (PERSIST-03).**

### Finding 1: Resume Input — None vs initial_state (langgraph==1.2.6)

**Test:** After a thread_id completes all 3 nodes, reinvoke the graph on the same thread_id with both patterns.

**Results observed:**

| Input | Chunks yielded | event_log growth | Nodes re-run? |
|-------|---------------|------------------|---------------|
| `None` | 0 | 0 (stays at 3) | NO |
| `initial_state dict` | 3 | +3 (grows to 6) | YES — re-runs all nodes |

**Correct pattern for Phase 5 PERSIST-03 (crash-resume):**
```python
# Resume from checkpoint — do NOT pass initial_state
async for chunk in graph.astream(None, config={"configurable": {"thread_id": job_id}}, stream_mode="updates"):
    ...
```

**Implication:** Passing the initial_state dict on a completed thread_id causes LangGraph to treat it as a fresh run (re-merges state and re-runs nodes). Only `None` correctly skips completed nodes.

### Finding 2: astream(stream_mode="updates") Chunk Key Format (langgraph==1.2.6)

**Test:** Print raw chunks from `graph.astream(initial, config, stream_mode="updates")`.

**Observed output:**
```
{'research': {'research_findings': 'STUB: research complete', 'event_log': ['...']}}
{'plan': {'plan': 'STUB: plan step 1, step 2, step 3', 'event_log': ['...']}}
{'execute': {'execution_result': 'STUB: execution complete', 'execution_status': 'success', 'event_log': ['...']}}
```

**Structure:**
- Each chunk = `{node_name_string: partial_state_dict}`
- Key = exactly the string passed to `builder.add_node()` ("research", "plan", "execute")
- Value = partial state dict returned by the node (only the keys it changed)
- No namespace wrapping, no class wrapper, no metadata wrapper

**Worker pattern confirmed for 01-02:**
```python
async for chunk in graph.astream(initial, config, stream_mode="updates"):
    for node_name in chunk:  # node_name is "research", "plan", or "execute"
        next_status = _NODE_COMPLETE_TO_STATUS.get(node_name)
        ...
```

## Decisions Made

1. **aiosqlite connection pattern:** `async with aiosqlite.connect(path) as conn:` not `async with await self._connect() as conn:`. The latter pre-awaits the connection (starting its background thread) then tries to re-enter it as a context manager, causing `RuntimeError: threads can only be started once`.
2. **ORCH-04 guard scope:** The plan's ORCH-04 verification (`assert 'messages' not in inspect.getsource(state)`) searches the entire source file including comments. Removed the word "messages" from all comments in state.py.
3. **Two SQLite files confirmed:** checkpoints.db (AsyncSqliteSaver-owned) and jobs.db (app-owned) kept strictly separate.
4. **uv lock file committed:** uv.lock included in Task 1 commit for reproducible installs.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] aiosqlite connection reuse RuntimeError in JobStore._connect()**

- **Found during:** Task 3 (running test_job_store.py for the first time)
- **Issue:** `_connect()` returned a pre-awaited `aiosqlite.Connection`; using it with `async with await self._connect() as conn:` starts the internal thread (via `await`) then tries to start it again when entering `__aenter__`, causing `RuntimeError: threads can only be started once`
- **Fix:** Removed `_connect()` helper; replaced all usages with `async with aiosqlite.connect(self.db_path) as conn:` directly, calling `_setup_conn(conn)` inside each block
- **Files modified:** `orchestrator/persistence/job_store.py`
- **Verification:** All 7 job_store tests pass; WAL mode test confirms PRAGMA set correctly
- **Committed in:** `3b51572` (Task 3 commit — the fix was discovered during test writing)

**2. [Rule 1 - Bug] ORCH-04 guard false positive from comment text**

- **Found during:** Task 2 verification (running ORCH-04 check)
- **Issue:** Comments in state.py contained the word "messages" (in phrases like "conversation messages") causing the source-level guard to fail even though no `messages` field exists in the TypedDict
- **Fix:** Rewrote comments in state.py to avoid the word "messages"
- **Files modified:** `orchestrator/graph/state.py`
- **Verification:** `assert 'messages' not in inspect.getsource(state)` passes
- **Committed in:** `9e23eb8` (Task 2 commit)

---

**Total deviations:** 2 auto-fixed (2 bugs)
**Impact on plan:** Both fixes necessary for correctness. No scope creep. aiosqlite fix is a correct-usage pattern change; ORCH-04 fix is a comment wording change.

## Issues Encountered

None — both deviations were caught and fixed inline during the same task execution.

## User Setup Required

None - no external service configuration required. All deps install via `uv pip install -e ".[dev]"`.

## Next Phase Readiness

- **01-02 is unblocked.** The two empirical findings (resume pattern = None; chunk key = node name string) are confirmed and recorded here.
- OrchestratorState schema is final — 01-02's worker and lifespan use it directly without renaming fields.
- build_stub_graph() is ready for compilation inside the FastAPI lifespan context.
- JobStore is ready for the worker to call set_status/set_complete/set_failed.
- No blockers or concerns for 01-02 or 01-03.

---
*Phase: 01-foundation*
*Completed: 2026-06-19*
