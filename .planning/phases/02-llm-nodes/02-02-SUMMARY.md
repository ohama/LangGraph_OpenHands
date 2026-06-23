# Summary: 02-02 — LLM Nodes live validation

**Plan:** 02-02
**Status:** Complete (human-verify checkpoint approved 2026-06-22)
**Completed:** 2026-06-22

## What was built

Runnable proofs that Phase 2 works against the live qwen-122b / LiteLLM stack, plus the one-time schema reset for the new `node_models` field.

- `scripts/smoke_litellm.sh` — >90s qwen-122b non-streaming generation through LiteLLM `:4000`, asserts HTTP 200 / no 504 (criterion 3).
- `tests/test_real_graph.py` — (a) resume-skips-research with real data + `node_models` survives checkpoint reopen; (b) dead-LiteLLM → clean FAILED job.
- `scripts/resume_real_data.sh` — end-to-end live kill-after-research resume proof (criterion 4), service stopped before the inline checkpoint resume (B4 single-writer), no orphan.

## Empirical results (this run)

**Criterion 3 — smoke_litellm.sh:** HTTP **200**, elapsed **144s** (>90s), completion_tokens 6000, **no 504**, no 122B wedge. PASS.

**Criterion 4 — test_real_graph.py:**
- `test_resume_skips_research_with_real_data` PASSED (41s): research call counter **0 increment on resume**; `node_models == {'research':'qwen-122b','plan':'qwen-122b'}` after `AsyncSqliteSaver` reopen; research ts `22:16:53` < plan ts `22:17:09`.
- `test_litellm_unavailable_sets_job_failed` PASSED (0.49s, offline/dead-port): `APIConnectionError → RuntimeError → set_failed()`; job FAILED in <1s, error `"LLM call failed for qwen-122b: APIConnectionError: Connection error."`; no retry storm (max_retries=0).

**Criterion 4 e2e — resume_real_data.sh:** PASS. PLANNING reached in 20s (research done) → service stopped, port free in 0s → inline `astream(None, config)` ran plan(22s)+execute(2s) only. `research_not_recalled=True`, `node_models={research:qwen-122b, plan:qwen-122b}`, plan 2636 chars. No orphaned uvicorn.

**Full suite:** 20 tests pass (18 offline Phase 1 + 2 new). Independently re-confirmed (77s, including the live resume test).

## Schema reset

`data/checkpoints.db` + `data/jobs.db` (+ wal/shm) deleted at Task 1 start so old Phase 1 stub checkpoints (pre-`node_models`) cannot poison runs; recreated by the lifespan (`init_jobs_db` + `saver.setup()`). `data/` is gitignored — reset not in git.

## Deviations

- **Port 8080 in resume_real_data.sh** (not 8000): `mlx_lm.server qwen-35b` occupies `:8000` on this machine. The script comments this. **Action item for Phase 5:** the orchestrator's production launchd port must avoid 8000 (qwen-35b), 8001 (qwen-122b), 4000 (LiteLLM). `verify_phase1.sh` used 8099; pick a stable non-colliding port for the plist.
- No 122B wedge/kickstart events this run (the model was respawned earlier during planning and stayed healthy).

## Commits

- `cada532` feat(02-02): reset Phase 1 DBs + write >90s LiteLLM smoke proof (criterion 3)
- `3d3c3a4` feat(02-02): add test_real_graph.py — resume-skip-research + dead-LiteLLM FAILED (criterion 4)
- `44c3586` feat(02-02): write + run live kill-after-research resume proof script (criterion 4)

## Carry-forward for later phases

- Resume is currently triggered manually (out-of-band `astream(None, …)`). **Automatic startup resume is Phase 5 (PERSIST-03).**
- Production service port must be chosen to avoid 8000/8001/4000 collisions (Phase 5 plist).
- The dead-LiteLLM clean-FAILED path is proven; Phase 5's lazy LiteLLM probe (OPS-03) builds on this.
