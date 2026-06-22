# tests/test_real_graph.py
# Phase 2 live integration tests.
# Two tests covering the core Phase 2 must-haves:
#
#   A) test_resume_skips_research_with_real_data
#      Implements RESEARCH Pattern 6 exactly:
#      - Phase 1: astream(initial_state) breaks after research (first chunk).
#      - Phase 2: astream(None) resumes — only plan (and execute) run.
#      - Assert: research_calls["count"] unchanged on resume; node_models survives reopen.
#      Requires live LiteLLM :4000; SKIPPED (not failed) when :4000 is down.
#      Makes TWO real qwen-122b calls (research + plan) — allow 3-5 minutes total.
#
#   B) test_litellm_unavailable_sets_job_failed
#      Implements RESEARCH Pattern 5:
#      - Points make_llm at dead port (LITELLM_BASE_URL=http://localhost:4999/v1).
#      - Runs a real job through worker_loop with build_stub_graph().
#      - Assert: job reaches FAILED cleanly, no hang, error field non-empty.
#      Does NOT require live LiteLLM — runs offline (dead port is intentional).

import asyncio
import os
import socket
import tempfile
import uuid

import pytest
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

import orchestrator.graph.stub_graph as _stub_graph_module
from orchestrator.graph.stub_graph import build_stub_graph
from orchestrator.persistence.job_store import JobStore, init_jobs_db
from orchestrator.worker.runner import worker_loop


# ---------------------------------------------------------------------------
# Helper: probe LiteLLM :4000 (quick TCP connect, no HTTP overhead)
# ---------------------------------------------------------------------------

def _litellm_is_up(host: str = "localhost", port: int = 4000) -> bool:
    """Quick TCP socket check — returns True if :4000 accepts a connection."""
    try:
        with socket.create_connection((host, port), timeout=2.0):
            return True
    except OSError:
        return False


def _make_initial_state(job_id: str, goal: str = "learn async Python") -> dict:
    """Build a fresh initial state dict for a job (mirrors runner.py initial_state)."""
    return {
        "goal": goal,
        "job_id": job_id,
        "research_findings": None,
        "plan": None,
        "execution_result": None,
        "execution_status": None,
        "event_log": [],
        "node_models": {},  # OBS-03: empty dict, NOT None (merge reducer requires dict)
    }


# ---------------------------------------------------------------------------
# Test A: Resume with real data skips research (RESEARCH Pattern 6)
# ---------------------------------------------------------------------------

async def test_resume_skips_research_with_real_data(monkeypatch):
    """After research_node completes and graph is interrupted, astream(None) resumes
    from plan_node only. research_node is NOT re-called. node_models survives reopen.

    RESEARCH Pattern 6 — empirically verified: astream(None, config) on a thread_id
    with a partial checkpoint (research done, plan not started) resumes from plan.

    NOTE: This test makes TWO real qwen-122b calls (research + plan) through
    LiteLLM :4000. Each call may take 30-120s. Total runtime: 3-5 minutes.
    This is expected — do not interrupt.

    Skip (not fail) when :4000 is down.
    """
    if not _litellm_is_up():
        pytest.skip("LiteLLM :4000 not available — skipping live test")

    THREAD_ID = f"resume-real-{uuid.uuid4().hex[:8]}"
    research_calls = {"count": 0}

    # Monkeypatch research_node to count invocations while still calling real logic.
    original_research = _stub_graph_module.research_node

    async def counting_research(state):
        research_calls["count"] += 1
        print(f"\n[counting_research] called (total calls: {research_calls['count']})")
        return await original_research(state)

    monkeypatch.setattr(_stub_graph_module, "research_node", counting_research)

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    try:
        config = {"configurable": {"thread_id": THREAD_ID}}
        initial = _make_initial_state(THREAD_ID, goal="learn async Python")

        # ----------------------------------------------------------------
        # Phase 1: Run graph until research completes, then break (simulate crash).
        # research_node is a real qwen-122b call — may take 30-120s.
        # ----------------------------------------------------------------
        print("\n[Phase 1] Starting graph — will break after research node...")
        async with AsyncSqliteSaver.from_conn_string(db_path) as saver:
            graph = build_stub_graph().compile(checkpointer=saver)
            first_chunk_received = False
            async for chunk in graph.astream(initial, config=config, stream_mode="updates"):
                print(f"[Phase 1 chunk] keys={list(chunk.keys())}")
                first_chunk_received = True
                break  # Simulate crash AFTER first node (research) completes

            assert first_chunk_received, "Graph yielded no chunks in Phase 1"

            # Verify checkpoint: research done, plan is next
            mid_state = await graph.aget_state(config)

        print(f"[Phase 1] mid_state.next = {mid_state.next}")
        print(f"[Phase 1] research_calls count = {research_calls['count']}")
        print(f"[Phase 1] research_findings (first 100 chars): {str(mid_state.values.get('research_findings', ''))[:100]}")

        # The checkpoint must show plan as the next node to run
        assert mid_state.next == ("plan",), (
            f"Expected next==('plan',) after research, got {mid_state.next}"
        )
        assert mid_state.values.get("research_findings"), (
            "research_findings must be non-empty after research_node completes"
        )
        assert research_calls["count"] == 1, (
            f"Expected exactly 1 research call in Phase 1, got {research_calls['count']}"
        )

        # ----------------------------------------------------------------
        # Phase 2: Resume via astream(None) — research must NOT be re-called.
        # plan_node is a real qwen-122b call — may take 30-90s.
        # After plan completes, execute_stub runs (~2s).
        # ----------------------------------------------------------------
        print("\n[Phase 2] Resuming from checkpoint with astream(None, config)...")
        research_calls_before_resume = research_calls["count"]

        nodes_run_on_resume = []
        async with AsyncSqliteSaver.from_conn_string(db_path) as saver:
            graph = build_stub_graph().compile(checkpointer=saver)
            async for chunk in graph.astream(None, config=config, stream_mode="updates"):
                for node_name in chunk:
                    nodes_run_on_resume.append(node_name)
                    print(f"[Phase 2 chunk] node={node_name}")

        print(f"[Phase 2] nodes_run_on_resume = {nodes_run_on_resume}")
        print(f"[Phase 2] research_calls count after resume = {research_calls['count']}")

        # research must NOT have been re-called during resume
        assert research_calls["count"] == research_calls_before_resume, (
            f"research_node was re-called during resume! "
            f"count before={research_calls_before_resume}, after={research_calls['count']}"
        )
        print(
            f"[Phase 2] research_calls count == {research_calls_before_resume} "
            f"(unchanged — research NOT re-called on resume)"
        )

        # plan must have run during resume
        assert "plan" in nodes_run_on_resume, (
            f"plan node did not run during resume; nodes_run={nodes_run_on_resume}"
        )

        # research must NOT appear in the resume's node list
        assert "research" not in nodes_run_on_resume, (
            f"research ran during resume! nodes_run={nodes_run_on_resume}"
        )

        # ----------------------------------------------------------------
        # Phase 3: Verify final state — node_models and event_log survive reopen
        # ----------------------------------------------------------------
        print("\n[Phase 3] Verifying final state (node_models, event_log)...")
        async with AsyncSqliteSaver.from_conn_string(db_path) as saver:
            graph = build_stub_graph().compile(checkpointer=saver)
            final = await graph.aget_state(config)

        node_models = final.values.get("node_models", {})
        event_log = final.values.get("event_log", [])

        print(f"[Phase 3] node_models = {node_models}")
        print(f"[Phase 3] event_log ({len(event_log)} entries):")
        for entry in event_log:
            print(f"  {entry}")

        # node_models must record both research and plan with qwen-122b
        assert node_models.get("research") == "qwen-122b", (
            f"Expected node_models['research']=='qwen-122b', got {node_models}"
        )
        assert node_models.get("plan") == "qwen-122b", (
            f"Expected node_models['plan']=='qwen-122b', got {node_models}"
        )
        print(f"[PASS] node_models == {node_models}")

        # event_log must have entries from both research and plan
        # (execute_stub also logs; total >= 2 from the real nodes)
        assert len(event_log) >= 2, (
            f"Expected at least 2 event_log entries (research + plan), got {len(event_log)}: {event_log}"
        )

        # Verify timestamps: research entry must precede plan entry
        research_entries = [e for e in event_log if "research_node" in e]
        plan_entries = [e for e in event_log if "plan_node" in e]
        assert research_entries, f"No research_node entry in event_log: {event_log}"
        assert plan_entries, f"No plan_node entry in event_log: {event_log}"
        assert research_entries[0] < plan_entries[0], (
            f"Research entry must predate plan entry (proves research ran before the crash, "
            f"not during resume).\nresearch: {research_entries[0]}\nplan: {plan_entries[0]}"
        )
        print(f"[PASS] research entry predates plan entry (timestamps confirm correct order)")
        print(f"[PASS] test_resume_skips_research_with_real_data PASSED")

    finally:
        try:
            os.unlink(db_path)
        except FileNotFoundError:
            pass


# ---------------------------------------------------------------------------
# Test B: Dead LiteLLM sets job to FAILED cleanly (RESEARCH Pattern 5)
# ---------------------------------------------------------------------------

async def _poll_until_terminal(
    store: JobStore,
    job_id: str,
    timeout: float = 30.0,
    interval: float = 0.25,
) -> list[str]:
    """Poll get_job() until status reaches a terminal state (DONE/FAILED/CANCELLED).

    Returns list of all distinct statuses observed in order.
    Raises TimeoutError if no terminal state within timeout seconds.
    """
    observed: list[str] = []
    deadline = asyncio.get_event_loop().time() + timeout

    while True:
        job = await store.get_job(job_id)
        assert job is not None, f"Job {job_id} not found"
        status: str = job["status"]

        if not observed or observed[-1] != status:
            observed.append(status)

        if status in ("DONE", "FAILED", "CANCELLED"):
            return observed

        if asyncio.get_event_loop().time() > deadline:
            raise TimeoutError(
                f"Job {job_id} did not reach terminal status within {timeout}s; "
                f"last status={status!r}, observed={observed}"
            )
        await asyncio.sleep(interval)


async def test_litellm_unavailable_sets_job_failed(monkeypatch):
    """When LiteLLM :4000 is unreachable, job transitions to FAILED cleanly.

    RESEARCH Pattern 5: max_retries=0 means research_node raises APIConnectionError
    immediately (no retry storm) -> RuntimeError -> worker set_failed() -> FAILED.

    Implementation: monkeypatch LITELLM_BASE_URL to a dead port (:4999) so
    research_node.ainvoke() gets a connection refused immediately.

    This test does NOT require a live LiteLLM — it intentionally uses a dead port.
    Runs fully offline.
    """
    monkeypatch.setenv("LITELLM_BASE_URL", "http://localhost:4999/v1")

    job_id = f"test-litellm-down-{uuid.uuid4().hex[:8]}"
    goal = "test goal for dead LiteLLM"

    with (
        tempfile.NamedTemporaryFile(suffix=".db", delete=False) as ckpt_f,
        tempfile.NamedTemporaryFile(suffix=".db", delete=False) as jobs_f,
    ):
        ckpt_path = ckpt_f.name
        jobs_path = jobs_f.name

    worker_task = None
    try:
        # Init jobs.db
        await init_jobs_db(jobs_path)
        store = JobStore(jobs_path)

        async with AsyncSqliteSaver.from_conn_string(ckpt_path) as saver:
            await saver.setup()
            graph = build_stub_graph().compile(checkpointer=saver)

            # Create job row before enqueuing
            await store.create_job(job_id, goal)

            # Start worker
            queue: asyncio.Queue = asyncio.Queue()
            worker_task = asyncio.create_task(
                worker_loop(queue, graph, jobs_path)
            )

            # Enqueue the job
            await queue.put((job_id, goal))

            # Poll until terminal — with dead LiteLLM, APIConnectionError is raised
            # immediately (no retry), so FAILED should appear within ~10s.
            observed_statuses = await _poll_until_terminal(
                store, job_id, timeout=30.0
            )
            print(f"\n[test_litellm_unavailable] observed statuses: {observed_statuses}")

        # Assertions
        final_job = await store.get_job(job_id)
        assert final_job is not None

        print(f"[test_litellm_unavailable] final status: {final_job['status']}")
        print(f"[test_litellm_unavailable] error: {final_job.get('error', 'N/A')}")

        assert final_job["status"] == "FAILED", (
            f"Expected FAILED, got {final_job['status']!r}; observed={observed_statuses}"
        )

        # Error field must be non-empty and mention LLM/model context
        error_text = final_job.get("error") or ""
        assert error_text, (
            "Expected non-empty error field on FAILED job, got empty string"
        )
        # The RuntimeError message from research_node includes the model name and error type
        # "LLM call failed for qwen-122b: APIConnectionError: ..."
        assert any(kw in error_text.lower() for kw in ["llm", "connection", "qwen", "litellm"]), (
            f"Expected error to mention LLM/model/connection, got: {error_text!r}"
        )
        print(f"[PASS] Job FAILED cleanly with error: {error_text[:200]}")
        print(f"[PASS] test_litellm_unavailable_sets_job_failed PASSED")

    finally:
        # Cancel worker task cleanly
        if worker_task is not None and not worker_task.done():
            worker_task.cancel()
            try:
                await worker_task
            except asyncio.CancelledError:
                pass

        # Clean up temp DB files
        for path in (ckpt_path, jobs_path):
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
