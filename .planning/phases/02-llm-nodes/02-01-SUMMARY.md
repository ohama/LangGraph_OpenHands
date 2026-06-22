---
phase: 02-llm-nodes
plan: 01
subsystem: api
tags: [langchain-openai, litellm, chatopenai, langgraph, node_models, obs-03, orch-05]

# Dependency graph
requires:
  - phase: 01-foundation
    provides: AsyncSqliteSaver, worker_loop, build_stub_graph, REST API, job_store
provides:
  - make_llm() factory reading LITELLM_BASE_URL/RESEARCH_MODEL/PLAN_MODEL from env
  - real research_node + plan_node calling qwen-122b via LiteLLM (ORCH-02, ORCH-03)
  - node_models OBS-03 merge field in OrchestratorState
  - build_test_graph() all-stub builder for offline tests
  - ORCHESTRATOR_TEST_GRAPH env flag in main.py lifespan
  - GET /jobs/{id}/result enriched with plan + research_findings from aget_state
affects:
  - 02-02 (live proof of real LLM calls via build_stub_graph)
  - 03-execute (replaces execute_stub; uses build_stub_graph / runner initial_state)
  - Phase 5 persist (resume uses build_stub_graph; node_models survives checkpoint)

# Tech tracking
tech-stack:
  added:
    - langchain-openai==1.3.2 (ChatOpenAI for LLM nodes)
    - langchain-core==1.4.8 (HumanMessage, SystemMessage — auto-installed)
    - openai==2.43.0 (APIConnectionError, APITimeoutError, APIStatusError — auto-installed)
  patterns:
    - make_llm() factory: env-based config, never singleton, never at module level
    - Annotated[dict, _merge_dicts] reducer for bounded per-node attribution dict
    - build_stub_graph (real) / build_test_graph (offline) dual-builder split
    - ORCHESTRATOR_TEST_GRAPH env flag for offline-safe lifespan
    - aget_state() best-effort checkpoint enrichment in result API

key-files:
  created:
    - orchestrator/graph/llm.py
  modified:
    - orchestrator/graph/state.py
    - orchestrator/graph/stub_graph.py
    - orchestrator/worker/runner.py
    - orchestrator/main.py
    - orchestrator/api/models.py
    - orchestrator/api/routes.py
    - tests/test_stub_graph.py
    - tests/test_worker.py
    - tests/test_api.py
    - pyproject.toml
    - .env

key-decisions:
  - "ORCH-05: model names resolved via os.getenv(RESEARCH_MODEL/PLAN_MODEL, 'qwen-122b') in node bodies — never hardcoded literal in node body"
  - "streaming=False: LiteLLM 1.86.1 handles non-streaming correctly; no 504; ainvoke() returns complete AIMessage"
  - "max_retries=0: retries amplify load on wedged qwen-122b; let job FAIL instead"
  - "node_models: Annotated[dict, _merge_dicts] — bounded 3-entry dict, NOT unbounded accumulator (ORCH-04)"
  - "node_models initialized as {} (empty dict), NEVER None — merge reducer does {**left, **right}"
  - "build_stub_graph (real) vs build_test_graph (all-stub): offline tests use the latter via ORCHESTRATOR_TEST_GRAPH=1"
  - "aget_state enrichment in result API is best-effort: exceptions swallowed; result from jobs.db is the contract"

patterns-established:
  - "make_llm() called inside node functions only — never at module top-level (Pitfall 2 avoidance)"
  - "API errors from openai (APIConnectionError/APITimeoutError/APIStatusError) re-raised as RuntimeError for worker set_failed() path"
  - "test_api.py sets ORCHESTRATOR_TEST_GRAPH=1 before app/lifespan starts; restores in finally"

# Metrics
duration: 4min
completed: 2026-06-23
---

# Phase 2 Plan 01: LLM Nodes — Factory, State, Real Nodes, Offline Test Split Summary

**make_llm() factory + real research_node/plan_node via LiteLLM (ORCH-02/03/05) + node_models OBS-03 merge field + build_test_graph() offline split + result API enriched with plan/research_findings**

## Performance

- **Duration:** 4 min
- **Started:** 2026-06-22T22:00:32Z
- **Completed:** 2026-06-22T22:04:32Z
- **Tasks:** 3/3
- **Files modified:** 11

## Accomplishments

- make_llm() factory in orchestrator/graph/llm.py reads base_url and model entirely from env (ORCH-05); streaming=False, max_retries=0, timeout=300, api_key="dummy"
- research_node and plan_node are now real LLM nodes: model resolved via os.getenv("RESEARCH_MODEL"/"PLAN_MODEL", "qwen-122b") — no hardcoded literal in node body; openai API errors re-raised as RuntimeError for worker set_failed() path
- node_models: Annotated[dict, _merge_dicts] added to OrchestratorState (OBS-03, bounded, ORCH-04 compliant); initial_state in runner seeds node_models={}
- build_stub_graph() (real nodes) and build_test_graph() (all-stub) dual builders; ORCHESTRATOR_TEST_GRAPH=1 flag in main.py selects the all-stub graph so test_stub_graph.py / test_worker.py / test_api.py run offline without LiteLLM
- GET /jobs/{id}/result now returns plan + research_findings from checkpoint via aget_state() (best-effort); 404/409 guards preserved; JobResultResponse extended with Optional plan/research_findings fields

## Task Commits

Each task was committed atomically:

1. **Task 1: Pin langchain-openai + make_llm() factory** - `9f5ca8e` (feat)
2. **Task 2: node_models state field + real nodes + build_test_graph() + migrate tests** - `81af012` (feat)
3. **Task 3: ORCHESTRATOR_TEST_GRAPH flag + result API enrichment + API test offline** - `7930aac` (feat)

**Plan metadata:** (docs commit follows)

## Files Created/Modified

- `orchestrator/graph/llm.py` - NEW: make_llm() factory with env-based config (ORCH-05)
- `orchestrator/graph/state.py` - Added node_models Annotated[dict, _merge_dicts] (OBS-03)
- `orchestrator/graph/stub_graph.py` - Real research_node + plan_node; kept stubs; added build_test_graph()
- `orchestrator/worker/runner.py` - initial_state seeds node_models={}
- `orchestrator/main.py` - ORCHESTRATOR_TEST_GRAPH env flag selects graph builder in lifespan
- `orchestrator/api/models.py` - JobResultResponse extended with plan/research_findings
- `orchestrator/api/routes.py` - get_result reads checkpoint via aget_state() after DONE guard
- `tests/test_stub_graph.py` - Migrated to build_test_graph(); node_models={} in initial state
- `tests/test_worker.py` - Migrated to build_test_graph(); "STUB: execution complete" assertion kept
- `tests/test_api.py` - Sets ORCHESTRATOR_TEST_GRAPH=1; restores in finally
- `pyproject.toml` - Added langchain-openai==1.3.2
- `.env` - Added LITELLM_BASE_URL, RESEARCH_MODEL, PLAN_MODEL

## Decisions Made

**make_llm() env contract (ORCH-05):**
- LITELLM_BASE_URL: proxy base URL (default http://localhost:4000/v1)
- RESEARCH_MODEL: model alias for research_node (default qwen-122b)
- PLAN_MODEL: model alias for plan_node (default qwen-122b)
- "qwen-122b" appears ONLY as the os.getenv default — never as a bare hardcoded assignment in node bodies; grep-verifiable

**streaming=False chosen over streaming=True:**
LiteLLM 1.86.1 has no 504 issue on non-streaming; empirically confirmed 131s non-streaming request at 5454 tokens succeeded. ainvoke() always returns a complete AIMessage regardless of streaming flag. streaming=False is simpler (single HTTP round-trip).

**max_retries=0 mandatory:**
Retries amplify load on a wedged qwen-122b (memory pressure + metal::malloc errors). Let the job FAIL; the user resubmits. This is the correct failure mode for local 122B models.

**node_models schema (OBS-03):**
Annotated[dict, _merge_dicts] where _merge_dicts does {**left, **right}. Bounded at 3 entries max (one per graph node). Survives checkpoint+resume (tested empirically in research). Initial value MUST be {} (empty dict), never None — _merge_dicts(None, {...}) raises TypeError.

**build_stub_graph vs build_test_graph split:**
build_stub_graph() = production (real LLM nodes, execute stub). build_test_graph() = offline (all stubs). test_stub_graph.py, test_worker.py, test_api.py all use build_test_graph() so the test suite is LiteLLM-independent. The ORCHESTRATOR_TEST_GRAPH=1 env flag wires this in main.py lifespan so test_api.py sees the all-stub graph without modifying the import.

**Result API enrichment (best-effort):**
plan and research_findings are read from the checkpoint via aget_state() after the DONE guard. If aget_state raises for any reason, the exception is swallowed and both fields are None — the execution_result from jobs.db is the authoritative contract. This avoids breaking the result endpoint if the checkpoint is corrupt or missing.

## Deviations from Plan

**1. [Rule 2 - Missing Critical] Added node_models={"research": "stub"} and {"plan": "stub"} to stub nodes**

- **Found during:** Task 2 (writing research_stub and plan_stub for build_test_graph)
- **Issue:** The stub nodes did not return node_models in Phase 1. After adding node_models: Annotated[dict, _merge_dicts] to OrchestratorState, the _merge_dicts reducer receives the node's partial return dict merged into state. If stub nodes don't return node_models, the reducer still works (LangGraph only merges keys that are present in the return dict). However, for consistency with the real nodes and to avoid any edge-case reducer behavior, stubs also write node_models with a "stub" model value.
- **Fix:** Added `"node_models": {"research": "stub"}` and `"node_models": {"plan": "stub"}` to research_stub and plan_stub return dicts.
- **Files modified:** orchestrator/graph/stub_graph.py
- **Verification:** 18 tests pass; node_models accumulates correctly in stub graph runs
- **Committed in:** 81af012 (Task 2 commit)

---

**Total deviations:** 1 auto-fixed (1 missing critical for consistency)
**Impact on plan:** Minor addition to stub nodes for OBS-03 consistency. No scope creep.

## Issues Encountered

None - all 18 tests passed offline after each task.

## User Setup Required

None - no external service configuration required for this plan. LiteLLM and qwen-122b are already running; live proof of real LLM calls is in plan 02-02.

## Next Phase Readiness

- make_llm() factory ready; 02-02 can use it directly for live integration proofs
- build_stub_graph() is the production graph with real LLM nodes; 02-02 starts the service with build_stub_graph() (ORCHESTRATOR_TEST_GRAPH not set)
- node_models OBS-03 field in state; checkpoint survives reopen (empirically verified in research)
- ORCHESTRATOR_TEST_GRAPH=1 flag documented; Phase 3 must set this in any test that imports the app
- result API enrichment ready; 02-02 proves plan/research_findings are non-empty for a real job

---
*Phase: 02-llm-nodes*
*Completed: 2026-06-23*
