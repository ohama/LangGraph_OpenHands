---
phase: 02-llm-nodes
verified: 2026-06-23T00:00:00Z
status: passed
score: 6/6 must-haves verified
---

# Phase 2: LLM Nodes Verification Report

**Phase Goal:** The Research and Plan nodes produce real LLM outputs using qwen-122b through the LiteLLM proxy; LiteLLM streaming/timeout verified (no 504); checkpoint persistence confirmed across a simulated restart with real data. Execute stays a STUB until Phase 3.

**Verified:** 2026-06-23
**Status:** passed
**Re-verification:** No — initial verification

---

## Goal Achievement

### Observable Truths

| # | Truth | Status | Evidence |
|---|-------|--------|----------|
| 1 | research_node calls qwen-122b via http://localhost:4000/v1 (never an mlx port) and writes non-empty findings to state | VERIFIED | `research_node` calls `make_llm(model=os.getenv("RESEARCH_MODEL","qwen-122b"))` which reads `LITELLM_BASE_URL` from env (defaulting to `http://localhost:4000/v1`). No mlx port anywhere in node bodies. Return dict sets `"research_findings": response.content`. |
| 2 | plan_node produces an actionable plan in state from research+goal via qwen-122b; plan text visible in GET /jobs/{id}/result | VERIFIED | `plan_node` calls `make_llm(model=os.getenv("PLAN_MODEL","qwen-122b"))`, returns `{"plan": response.content, ...}`. `GET /jobs/{id}/result` reads `plan` from `aget_state()` snapshot and includes it in `JobResultResponse`. |
| 3 | A curl generating >90s of 122B output completes without a 504 | VERIFIED | `scripts/smoke_litellm.sh` exists with `max_tokens=6000`, `stream:false`, asserts HTTP 200 / no 504. Live run documented in 02-02-SUMMARY.md: elapsed=144s, HTTP 200, no 504. PASS. |
| 4 | After a simulated restart mid-job (killed after Research), the resumed run skips Research and starts from Plan; node_models attribution recorded in state | VERIFIED | `test_resume_skips_research_with_real_data` uses `astream(None, config)` to resume; asserts research call count unchanged; asserts `node_models=={"research":"qwen-122b","plan":"qwen-122b"}` survives AsyncSqliteSaver reopen. `scripts/resume_real_data.sh` proves this end-to-end against the live service (B4 single-writer enforced). Live run documented in 02-02-SUMMARY.md: PASS. |
| 5 | All existing Phase 1 tests still pass offline (no LLM calls) | VERIFIED | Full test suite: `20 passed in 75.52s`. All Phase 1 offline tests use `build_test_graph()` (all-stub). `ORCHESTRATOR_TEST_GRAPH=1` selects the stub graph in `test_api.py`. No network calls in offline path. |
| 6 | When LiteLLM :4000 is unreachable, job transitions to FAILED cleanly (no retry storm, no hang) | VERIFIED | `test_litellm_unavailable_sets_job_failed` monkeypatches `LITELLM_BASE_URL` to dead port `:4999`; asserts `status==FAILED` within 30s; asserts error field mentions "llm"/"connection"/"qwen". Documented in 02-02-SUMMARY.md: PASSED in 0.49s. |

**Score:** 6/6 truths verified

---

## Required Artifacts

| Artifact | Expected | Status | Details |
|----------|----------|--------|---------|
| `orchestrator/graph/llm.py` | `make_llm()` factory reading LITELLM_BASE_URL/RESEARCH_MODEL/PLAN_MODEL from env | VERIFIED | 59 lines. `make_llm()` reads `os.getenv("LITELLM_BASE_URL","http://localhost:4000/v1")`. `streaming=False`, `max_retries=0`, `api_key="dummy"`, `timeout=300`. No module-level instantiation. |
| `orchestrator/graph/state.py` | OrchestratorState with node_models merge-reducer field (OBS-03) | VERIFIED | 47 lines. `_merge_dicts` reducer: `{**left, **right}`. `node_models: Annotated[dict, _merge_dicts]`. No unbounded accumulator. `event_log` uses `operator.add` (bounded per-node entries). |
| `orchestrator/graph/stub_graph.py` | real research_node + plan_node, execute stub, build_stub_graph() + build_test_graph() | VERIFIED | 207 lines. `research_node` and `plan_node` call `make_llm().ainvoke()`, wrap errors as `RuntimeError`, return `node_models`/`event_log`. `execute_stub` unchanged (sleep 2.0, "STUB: execution complete"). Both builders present. |
| `orchestrator/main.py` | lifespan selects build_test_graph() when ORCHESTRATOR_TEST_GRAPH==1 else build_stub_graph() | VERIFIED | `if os.getenv("ORCHESTRATOR_TEST_GRAPH") == "1": graph_builder = build_test_graph else: graph_builder = build_stub_graph`. Control-transfer inside `async with AsyncSqliteSaver` block. |
| `orchestrator/api/models.py` | JobResultResponse extended with plan and research_findings | VERIFIED | `plan: str | None = None` and `research_findings: str | None = None` fields present. |
| `orchestrator/api/routes.py` | GET /jobs/{id}/result reads plan/research_findings via aget_state(); 404/409 preserved | VERIFIED | `await request.app.state.graph.aget_state(config)` inside try/except (best-effort enrichment). 404 (unknown) and 409 (not DONE) guards remain first, unchanged. |
| `orchestrator/worker/runner.py` | initial_state seeds node_models={} | VERIFIED | `"node_models": {},  # OBS-03: empty dict, NOT None` at line 145. |
| `tests/test_stub_graph.py` | uses build_test_graph() | VERIFIED | All graph usages import and use `build_test_graph()`. `node_models: {}` in `_make_initial_state()` helper. |
| `tests/test_worker.py` | migrated to build_test_graph() | VERIFIED | `from orchestrator.graph.stub_graph import build_test_graph`. `build_test_graph().compile(...)`. Existing `result == "STUB: execution complete"` assertion intact. |
| `tests/test_api.py` | sets ORCHESTRATOR_TEST_GRAPH=1 | VERIFIED | `os.environ["ORCHESTRATOR_TEST_GRAPH"] = "1"` set before `TestClient(app)` enters. 404/409 assertions intact. |
| `tests/test_real_graph.py` | has test_resume_skips_research_with_real_data and test_litellm_unavailable_sets_job_failed | VERIFIED | Both functions present. Resume test uses `astream(None, config)`, asserts research_calls count, `node_models` survival, timestamp ordering. LiteLLM-down test uses dead port, asserts FAILED within 30s. |
| `scripts/smoke_litellm.sh` | >90s no-504 curl proof | VERIFIED | 130 lines. `max_tokens=6000`, `stream:false`, asserts `http_code==200`, elapsed advisory check. Health precheck. Clear PASS/FAIL output. |
| `scripts/resume_real_data.sh` | kill-after-research resume proof with no-orphan launch and B4 single-writer guard | VERIFIED | 454 lines. Launched via `.venv/bin/uvicorn`. Killed via `pkill -9 -f "uvicorn orchestrator.main:app"`. Single-writer guard: `pgrep` + `lsof -ti tcp:${PORT}` checked before inline resume. `timeout 600` hard guard on Python resume block. EXIT trap. |
| `pyproject.toml` | langchain-openai==1.3.2 pinned | VERIFIED | `"langchain-openai==1.3.2"` at line 14. |

---

## Key Link Verification

| From | To | Via | Status | Details |
|------|----|-----|--------|---------|
| `stub_graph.py:research_node` | `llm.py:make_llm` | `make_llm(model=os.getenv("RESEARCH_MODEL","qwen-122b")).ainvoke(messages)` | WIRED | `make_llm` imported; called at line 62 with model from env; `await llm.ainvoke(messages)` at line 68. |
| `stub_graph.py:plan_node` | `llm.py:make_llm` | `make_llm(model=os.getenv("PLAN_MODEL","qwen-122b")).ainvoke(messages)` | WIRED | Called at line 90; `await llm.ainvoke(messages)` at line 102. |
| `llm.py:make_llm` | LiteLLM :4000 | `ChatOpenAI(base_url=os.getenv("LITELLM_BASE_URL","http://localhost:4000/v1"))` | WIRED | `base_url = os.getenv("LITELLM_BASE_URL", "http://localhost:4000/v1")` at line 48. No hardcoded port in node bodies. |
| `stub_graph.py` nodes | RESEARCH_MODEL/PLAN_MODEL env vars | `os.getenv("RESEARCH_MODEL","qwen-122b")` / `os.getenv("PLAN_MODEL","qwen-122b")` | WIRED | All four occurrences of "qwen-122b" in stub_graph.py are exclusively as `os.getenv(...)` defaults. No bare model assignment in node bodies. |
| `main.py:lifespan` | build_test_graph / build_stub_graph | `ORCHESTRATOR_TEST_GRAPH` env flag | WIRED | Conditional at lines 66-69 selects builder. |
| `routes.py:get_result` | checkpoint via aget_state | `await request.app.state.graph.aget_state(config)` | WIRED | Line 102; inside try/except; values extracted and returned in `JobResultResponse`. |
| `runner.py:initial_state` | OrchestratorState.node_models | `"node_models": {}` | WIRED | Line 145. |
| `test_real_graph.py:resume test` | `graph.astream(None, config)` | input=None skips completed research | WIRED | Line 146: `graph.astream(None, config=config, stream_mode="updates")`. |
| `research_node API error` | `JobStore.set_failed` | `RuntimeError` propagates to worker `except Exception -> set_failed()` | WIRED | `research_node` raises `RuntimeError` from `except (APIConnectionError, APITimeoutError, APIStatusError)`; `runner.py` line 193 catches `Exception` and calls `set_failed`. |
| `resume_real_data.sh:inline resume` | `data/checkpoints.db` (sole writer) | service STOPPED + port free BEFORE `astream(None, ...)` | WIRED | `pkill` at line 204; `pgrep` + `lsof` loop at lines 213-235; `timeout 600` guard at line 252. |

---

## Requirements Coverage

| Requirement | Status | Evidence |
|-------------|--------|----------|
| ORCH-02: research_node calls real LLM | SATISFIED | `research_node` calls `make_llm().ainvoke()` with model from `RESEARCH_MODEL` env; live run produced non-empty `research_findings`. |
| ORCH-03: plan_node calls real LLM | SATISFIED | `plan_node` calls `make_llm().ainvoke()` with model from `PLAN_MODEL` env; `plan` field populated in state and returned in result API. |
| ORCH-05: LLM calls route through LiteLLM env-configured proxy; no hardcoded ports in node bodies | SATISFIED | `base_url` exclusively from `LITELLM_BASE_URL` env var in `make_llm()`; node bodies call `os.getenv("RESEARCH_MODEL"/"PLAN_MODEL")`; no hardcoded `:4000` or `:llm_port` in stub_graph.py. |
| OBS-03: per-node model attribution in state | SATISFIED | `node_models: Annotated[dict, _merge_dicts]` in `OrchestratorState`; each real node returns `{"node_models": {"research"/"plan": model}}`; survives checkpoint reopen (proven by resume test). |

---

## Anti-Patterns Found

None blocking. The following items were checked and are clean:

- No `TODO`/`FIXME`/`placeholder` in any Phase 2 file.
- No module-level `ChatOpenAI(...)` instantiation in `llm.py` (only inside `make_llm()`).
- No `return null` / empty handler stubs in real nodes.
- `execute_stub` is intentionally a stub (Phase 3 replaces it) — correctly documented.
- "qwen-122b" literal appears only as `os.getenv(...)` defaults in node bodies, never as a bare assignment.

---

## Test Suite Result

```
20 passed, 1 warning in 75.52s
```

All 20 tests pass. The 75s runtime includes the live `test_resume_skips_research_with_real_data` test which makes two real qwen-122b calls (research + plan) through LiteLLM. The `test_litellm_unavailable_sets_job_failed` test runs offline (dead port, no LiteLLM needed) and passed in <1s. All Phase 1 offline tests (stub_graph, worker, API) remain green using `build_test_graph()` / `ORCHESTRATOR_TEST_GRAPH=1`.

---

## Live Proof Results (from 02-02-SUMMARY.md)

- **Criterion 3 (>90s no-504):** `scripts/smoke_litellm.sh` — HTTP 200, elapsed 144s, completion_tokens 6000, no 504. PASS.
- **Criterion 4 (resume skips research):** `test_resume_skips_research_with_real_data` PASSED (41s); research call counter unchanged on resume; `node_models=={'research':'qwen-122b','plan':'qwen-122b'}` after checkpoint reopen; research timestamp predates plan timestamp. `scripts/resume_real_data.sh` PASS — PLANNING reached in 20s, service stopped, port free, inline `astream(None)` ran plan(22s)+execute(2s) only, `plan=2636 chars`, no orphaned uvicorn.

---

## Gaps Summary

None. All 6 observable truths are verified. All required artifacts exist, are substantive, and are wired. All 4 requirements (ORCH-02, ORCH-03, ORCH-05, OBS-03) are satisfied. The full test suite passes including the live resume test.

---

_Verified: 2026-06-23_
_Verifier: Claude (gsd-verifier)_
