# orchestrator/graph/stub_graph.py
# Stub LangGraph graph with the REAL node names (research/plan/execute).
# Phases 2/3 replace node implementations; graph shape + state schema stay the same.
# Returns an UNcompiled StateGraph builder — caller compiles with checkpointer.
import asyncio
from datetime import datetime, timezone

from langgraph.graph import StateGraph, END

from orchestrator.graph.state import OrchestratorState


async def research_stub(state: OrchestratorState) -> dict:
    """Stub: simulates ~1s research call, returns placeholder findings."""
    await asyncio.sleep(1.0)
    return {
        "research_findings": "STUB: research complete",
        "event_log": [
            f"{datetime.now(timezone.utc).isoformat()} research_stub: complete"
        ],
    }


async def plan_stub(state: OrchestratorState) -> dict:
    """Stub: simulates ~1s plan call, returns placeholder plan."""
    await asyncio.sleep(1.0)
    return {
        "plan": "STUB: plan step 1, step 2, step 3",
        "event_log": [
            f"{datetime.now(timezone.utc).isoformat()} plan_stub: complete"
        ],
    }


async def execute_stub(state: OrchestratorState) -> dict:
    """Stub: simulates ~2s execute call. Replaced by OpenHands adapter in Phase 3."""
    await asyncio.sleep(2.0)
    return {
        "execution_result": "STUB: execution complete",
        "execution_status": "success",
        "event_log": [
            f"{datetime.now(timezone.utc).isoformat()} execute_stub: complete"
        ],
    }


def build_stub_graph() -> StateGraph:
    """Build the stub graph with nodes research->plan->execute->END.

    Returns the UNcompiled builder. Compile in lifespan or tests:
        graph = build_stub_graph().compile(checkpointer=saver)
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
