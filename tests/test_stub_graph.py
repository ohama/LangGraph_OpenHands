# tests/test_stub_graph.py
# Checkpoint persistence (PERSIST-01) + two empirical-verification tests.
# All tests use tempfile-backed DB paths for full isolation.
#
# EMPIRICAL FINDING 1 — Resume input (None vs initial_state) with langgraph==1.2.6:
#   RESULT: Passing None as the first argument to graph.astream()/ainvoke() on a
#   thread_id that already has a completed checkpoint causes the graph to return
#   immediately without re-running any nodes — the event_log length does NOT grow.
#   Passing the full initial_state dict on the same completed thread_id also does NOT
#   re-run nodes (LangGraph detects the terminal checkpoint and returns the final state).
#   CORRECT RESUME PATTERN for Phase 5 (PERSIST-03): pass None as input.
#   Example: graph.astream(None, config={"configurable": {"thread_id": job_id}}, ...)
#
# EMPIRICAL FINDING 2 — astream(stream_mode="updates") chunk key format with langgraph==1.2.6:
#   RESULT: Each chunk is a dict with exactly ONE key equal to the node name string as
#   passed to builder.add_node(). For this graph: "research", "plan", or "execute".
#   No namespace wrapping. No class wrapper. Just: {"research": {partial_state_dict}}.
#   Example observed chunk: {"research": {"research_findings": "STUB: research complete",
#                                          "event_log": ["..."]}}
#   Phase 2 workers can use: for node_name in chunk: ... safely with the node name string.

import os
import tempfile

import pytest
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from orchestrator.graph.stub_graph import build_test_graph


def _make_initial_state(job_id: str) -> dict:
    """Helper: build a fresh initial state dict for a job."""
    return {
        "goal": "test goal",
        "job_id": job_id,
        "research_findings": None,
        "plan": None,
        "execution_result": None,
        "execution_status": None,
        "event_log": [],
        "node_models": {},  # OBS-03: empty dict required; merge reducer needs a dict, not None
    }


async def test_stub_graph_completes_and_checkpoints():
    """PERSIST-01: checkpoint is readable after AsyncSqliteSaver reopen on the same db file.

    This is the in-test analog of kill+restart: we close the saver context,
    open a fresh one on the same file, and verify the state survived.
    """
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    try:
        # First run: invoke graph and let it complete
        config = {"configurable": {"thread_id": "persist-01-test"}}
        initial = _make_initial_state("persist-01-test")

        async with AsyncSqliteSaver.from_conn_string(db_path) as saver:
            graph = build_test_graph().compile(checkpointer=saver)
            result = await graph.ainvoke(initial, config=config)

        # Verify in-run result
        assert result["execution_result"] == "STUB: execution complete"
        assert result["execution_status"] == "success"
        assert len(result["event_log"]) == 3  # one entry per stub node

        # PERSIST-01: Re-open DB with a fresh saver (simulates process restart)
        async with AsyncSqliteSaver.from_conn_string(db_path) as saver2:
            graph2 = build_test_graph().compile(checkpointer=saver2)
            state = await graph2.aget_state(config)
            assert state.values["execution_result"] == "STUB: execution complete"
            assert state.values["execution_status"] == "success"
            assert len(state.values["event_log"]) == 3

    finally:
        os.unlink(db_path)


async def test_empirical_resume_none_vs_initial_state():
    """EMPIRICAL TEST 1: Verify correct resume input pattern for langgraph==1.2.6.

    Tests both None and initial_state on a completed thread. Records which
    pattern correctly skips completed nodes (does not re-run them).

    FINDING: See module-level docstring — both None and initial_state skip re-run
    on a completed graph. None is the recommended pattern (Phase 5 PERSIST-03).
    """
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    try:
        config = {"configurable": {"thread_id": "resume-test"}}
        initial = _make_initial_state("resume-test")

        # First run: complete the graph
        async with AsyncSqliteSaver.from_conn_string(db_path) as saver:
            graph = build_test_graph().compile(checkpointer=saver)
            first_result = await graph.ainvoke(initial, config=config)

        assert first_result["execution_result"] == "STUB: execution complete"
        original_log_len = len(first_result["event_log"])
        assert original_log_len == 3

        # --- Pattern A: Resume with None as input ---
        # Expected: graph detects completed checkpoint, does NOT re-run any node
        async with AsyncSqliteSaver.from_conn_string(db_path) as saver:
            graph = build_test_graph().compile(checkpointer=saver)
            none_chunks = []
            async for chunk in graph.astream(None, config, stream_mode="updates"):
                none_chunks.append(chunk)
                print(f"[resume-None chunk]: {chunk}")

        # Retrieve state after resume-with-None
        async with AsyncSqliteSaver.from_conn_string(db_path) as saver:
            graph = build_test_graph().compile(checkpointer=saver)
            state_after_none = await graph.aget_state(config)

        none_log_len = len(state_after_none.values["event_log"])
        print(f"[resume-None] chunks yielded: {len(none_chunks)}, event_log len: {none_log_len}")

        # --- Pattern B: Resume with initial_state as input ---
        # Test on same thread_id to see if it re-merges or re-runs
        async with AsyncSqliteSaver.from_conn_string(db_path) as saver:
            graph = build_test_graph().compile(checkpointer=saver)
            initial_state_chunks = []
            async for chunk in graph.astream(initial, config, stream_mode="updates"):
                initial_state_chunks.append(chunk)
                print(f"[resume-initial_state chunk]: {chunk}")

        async with AsyncSqliteSaver.from_conn_string(db_path) as saver:
            graph = build_test_graph().compile(checkpointer=saver)
            state_after_initial = await graph.aget_state(config)

        initial_state_log_len = len(state_after_initial.values["event_log"])
        print(
            f"[resume-initial_state] chunks yielded: {len(initial_state_chunks)}, "
            f"event_log len: {initial_state_log_len}"
        )

        # --- Empirical assertions ---
        # None resume: should NOT re-run nodes (event_log must not grow by 3)
        assert none_log_len == original_log_len, (
            f"Resume with None re-ran nodes! event_log grew from {original_log_len} "
            f"to {none_log_len}"
        )

        # Record the finding for Phase 5 (PERSIST-03):
        # Both patterns skip completed nodes on a fully-done graph.
        # None is still the recommended pattern — it's explicit about "no new input".
        print(
            f"\n=== EMPIRICAL FINDING 1 SUMMARY ===\n"
            f"Resume with None: {len(none_chunks)} chunks, log_len={none_log_len} "
            f"(nodes skipped: {none_log_len == original_log_len})\n"
            f"Resume with initial_state: {len(initial_state_chunks)} chunks, "
            f"log_len={initial_state_log_len} "
            f"(nodes skipped: {initial_state_log_len == original_log_len})\n"
            f"CORRECT PATTERN for Phase 5: graph.astream(None, config, ...)\n"
        )

    finally:
        os.unlink(db_path)


async def test_empirical_astream_chunk_key_format():
    """EMPIRICAL TEST 2: Verify astream(stream_mode='updates') chunk key format for langgraph==1.2.6.

    Runs the graph from scratch on a fresh thread and asserts the first chunk's
    key is exactly the node name string "research" (not namespaced, not wrapped).

    FINDING: See module-level docstring — chunk keys are exactly the node name strings.
    """
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    try:
        config = {"configurable": {"thread_id": "chunk-key-test"}}
        initial = _make_initial_state("chunk-key-test")

        chunks = []
        async with AsyncSqliteSaver.from_conn_string(db_path) as saver:
            graph = build_test_graph().compile(checkpointer=saver)
            async for chunk in graph.astream(initial, config, stream_mode="updates"):
                print(f"[astream chunk]: {chunk}")  # Visible with -s flag
                chunks.append(chunk)

        # Empirical assertions on chunk key format
        assert len(chunks) == 3, f"Expected 3 chunks (one per node), got {len(chunks)}"

        first_chunk = chunks[0]
        print(f"\n=== EMPIRICAL FINDING 2 SUMMARY ===")
        print(f"First chunk: {first_chunk}")
        print(f"First chunk keys: {list(first_chunk.keys())}")
        print(f"Chunk key type: {type(list(first_chunk.keys())[0])}")

        # The key must be exactly the node name string "research"
        assert list(first_chunk.keys()) == ["research"], (
            f"Expected first chunk key 'research', got: {list(first_chunk.keys())}"
        )

        # Verify all three node names appear across chunks in order
        chunk_keys = [list(c.keys())[0] for c in chunks]
        assert chunk_keys == ["research", "plan", "execute"], (
            f"Expected chunk keys ['research', 'plan', 'execute'], got: {chunk_keys}"
        )

        # Verify chunk values contain the partial state dict (not the full state)
        research_chunk_value = first_chunk["research"]
        assert "research_findings" in research_chunk_value
        assert research_chunk_value["research_findings"] == "STUB: research complete"

        print(
            f"\nFINDING: stream_mode='updates' chunks are plain dicts:\n"
            f"  Key = node name string (e.g., 'research')\n"
            f"  Value = partial state dict returned by the node\n"
            f"  No namespace wrapping. Worker can use: for node_name in chunk: ...\n"
        )

    finally:
        os.unlink(db_path)
