# orchestrator/graph/stub_graph.py
# LangGraph graph builder for the orchestrator.
#
# Node split (Phase 2):
#   research_node, plan_node  — REAL: call qwen-122b via LiteLLM :4000 (ORCH-02/03)
#   execute_stub              — STUB: unchanged from Phase 1; replaced in Phase 3
#   research_stub, plan_stub  — STUB versions kept for build_test_graph() (offline tests)
#
# Two builders:
#   build_stub_graph()  — PRODUCTION: real research + plan, stub execute
#   build_test_graph()  — OFFLINE TESTS: all three nodes are stubs (no LLM calls)
#
# Node string keys "research" / "plan" / "execute" MUST NOT change — the worker
# maps these to status transitions in _NODE_COMPLETE_TO_STATUS (runner.py).
#
# ORCH-05: model names resolved via os.getenv(RESEARCH_MODEL/PLAN_MODEL) inside
# each node function — NEVER hardcoded in the node body. The only allowed
# appearance of "qwen-122b" is as the os.getenv default string.
import asyncio
import os
from datetime import datetime, timezone

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import StateGraph, END
from openai import APIConnectionError, APIStatusError, APITimeoutError

from orchestrator.graph.llm import make_llm
from orchestrator.graph.state import OrchestratorState


# ---------------------------------------------------------------------------
# System prompts (kept concise to stay within token budget, RESEARCH Pitfall 4)
# ---------------------------------------------------------------------------

_RESEARCH_SYSTEM = (
    "You are a technical research assistant. Given a software development goal, "
    "provide a concise research summary covering key technical considerations, "
    "relevant tools or patterns, potential challenges, and prerequisites. "
    "Be specific and actionable. Keep your response under 600 words."
)

_PLAN_SYSTEM = (
    "You are a software planning assistant. Given a goal and research findings, "
    "produce a concrete numbered implementation plan. Each step must specify: "
    "the exact action (create/edit/run), the file path or command, and a done-check. "
    "5-8 steps. No vague steps like 'investigate' or 'update as needed'. Under 500 words."
)


# ---------------------------------------------------------------------------
# Real LLM nodes (ORCH-02, ORCH-03, OBS-03) — used by build_stub_graph()
# ---------------------------------------------------------------------------

async def research_node(state: OrchestratorState) -> dict:
    """REAL: calls qwen-122b via LiteLLM :4000 (ORCH-02, OBS-03).

    Model resolved from env (ORCH-05): os.getenv("RESEARCH_MODEL", "qwen-122b").
    Re-raises openai API errors as RuntimeError so worker.set_failed() handles them.
    max_tokens=2000, temperature=0.4 (creative research phase).
    """
    model = os.getenv("RESEARCH_MODEL", "qwen-122b")
    llm = make_llm(model=model, max_tokens=2000, temperature=0.4)
    messages = [
        SystemMessage(content=_RESEARCH_SYSTEM),
        HumanMessage(content=f"Goal: {state['goal']}\n\nResearch the technical approach."),
    ]
    try:
        response = await llm.ainvoke(messages)
    except (APIConnectionError, APITimeoutError, APIStatusError) as exc:
        # Let RuntimeError propagate to worker_loop → set_failed().
        # Do NOT retry here — max_retries=0 is already set on the client (RESEARCH Pattern 5).
        raise RuntimeError(f"LLM call failed for {model}: {type(exc).__name__}: {exc}") from exc
    return {
        "research_findings": response.content,
        "node_models": {"research": model},   # OBS-03
        "event_log": [
            f"{datetime.now(timezone.utc).isoformat()} research_node: complete model={model}"
        ],
    }


async def plan_node(state: OrchestratorState) -> dict:
    """REAL: calls qwen-122b via LiteLLM :4000 (ORCH-03, OBS-03).

    Model resolved from env (ORCH-05): os.getenv("PLAN_MODEL", "qwen-122b").
    Re-raises openai API errors as RuntimeError so worker.set_failed() handles them.
    max_tokens=1500, temperature=0.2 (deterministic planning phase).
    """
    model = os.getenv("PLAN_MODEL", "qwen-122b")
    llm = make_llm(model=model, max_tokens=1500, temperature=0.2)
    messages = [
        SystemMessage(content=_PLAN_SYSTEM),
        HumanMessage(
            content=(
                f"Goal: {state['goal']}\n\n"
                f"Research Findings:\n{state['research_findings']}\n\n"
                "Create a concrete implementation plan."
            )
        ),
    ]
    try:
        response = await llm.ainvoke(messages)
    except (APIConnectionError, APITimeoutError, APIStatusError) as exc:
        raise RuntimeError(f"LLM call failed for {model}: {type(exc).__name__}: {exc}") from exc
    return {
        "plan": response.content,
        "node_models": {"plan": model},   # OBS-03
        "event_log": [
            f"{datetime.now(timezone.utc).isoformat()} plan_node: complete model={model}"
        ],
    }


# ---------------------------------------------------------------------------
# Stub nodes — unchanged from Phase 1; used by build_test_graph() for offline
# unit, worker, and API tests (no LLM calls, no network).
# ---------------------------------------------------------------------------

async def research_stub(state: OrchestratorState) -> dict:
    """Stub: simulates ~1s research call, returns placeholder findings."""
    await asyncio.sleep(1.0)
    return {
        "research_findings": "STUB: research complete",
        "node_models": {"research": "stub"},   # OBS-03 compatible (avoids None merge)
        "event_log": [
            f"{datetime.now(timezone.utc).isoformat()} research_stub: complete"
        ],
    }


async def plan_stub(state: OrchestratorState) -> dict:
    """Stub: simulates ~1s plan call, returns placeholder plan."""
    await asyncio.sleep(1.0)
    return {
        "plan": "STUB: plan step 1, step 2, step 3",
        "node_models": {"plan": "stub"},   # OBS-03 compatible
        "event_log": [
            f"{datetime.now(timezone.utc).isoformat()} plan_stub: complete"
        ],
    }


async def execute_stub(state: OrchestratorState) -> dict:
    """STUB: simulates ~2s execute call. Replaced by OpenHands adapter in Phase 3.

    UNCHANGED from Phase 1 — do not modify. Phase 3 replaces this function.
    """
    await asyncio.sleep(2.0)
    return {
        "execution_result": "STUB: execution complete",
        "execution_status": "success",
        "event_log": [
            f"{datetime.now(timezone.utc).isoformat()} execute_stub: complete"
        ],
    }


# ---------------------------------------------------------------------------
# Graph builders
# ---------------------------------------------------------------------------

def build_stub_graph() -> StateGraph:
    """PRODUCTION graph: research + plan are REAL (qwen-122b), execute is STUB.

    Name kept for compatibility with main.py lifespan and worker runner.
    Node string keys "research"/"plan"/"execute" match _NODE_COMPLETE_TO_STATUS
    in runner.py — do NOT rename.

    Returns the UNcompiled builder. Compile in lifespan or tests:
        graph = build_stub_graph().compile(checkpointer=saver)
    """
    builder = StateGraph(OrchestratorState)
    builder.add_node("research", research_node)
    builder.add_node("plan", plan_node)
    builder.add_node("execute", execute_stub)
    builder.set_entry_point("research")
    builder.add_edge("research", "plan")
    builder.add_edge("plan", "execute")
    builder.add_edge("execute", END)
    return builder


def build_test_graph() -> StateGraph:
    """OFFLINE TEST graph: all three nodes are stubs (no LLM calls, no network).

    Used by:
      - tests/test_stub_graph.py  (unit tests for graph / checkpoint behavior)
      - tests/test_worker.py      (worker integration test — offline)
      - tests/test_api.py via ORCHESTRATOR_TEST_GRAPH=1 env flag in main.py

    Graph shape is identical to build_stub_graph() — same node keys, same edges.
    Stub nodes return "STUB: execution complete" and "STUB: research complete" so
    all Phase 1 assertions remain valid.

    Returns the UNcompiled builder. Compile in lifespan or tests:
        graph = build_test_graph().compile(checkpointer=saver)
    """
    builder = StateGraph(OrchestratorState)
    builder.add_node("research", research_stub)
    builder.add_node("plan", plan_stub)
    builder.add_node("execute", execute_stub)
    builder.set_entry_point("research")
    builder.add_edge("research", "plan")
    builder.add_edge("plan", "execute")
    builder.add_edge("execute", END)
    return builder
