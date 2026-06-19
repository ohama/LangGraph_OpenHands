# orchestrator/graph/state.py
# OrchestratorState — the REAL schema for Phases 2/3.
# ORCH-04: Typed string fields only. No unbounded list accumulator.
from typing import TypedDict, Annotated, Optional
import operator


class OrchestratorState(TypedDict):
    # Set at job submission, never mutated by nodes
    goal: str
    job_id: str

    # Populated by research_node (Phase 2); stub returns placeholder string
    research_findings: Optional[str]

    # Populated by plan_node (Phase 2); stub returns placeholder string
    plan: Optional[str]

    # Populated by execute_node (Phase 3); stub returns placeholder string
    execution_result: Optional[str]

    # "success" | "partial" | "failed" — written by execute_node (Phase 3)
    execution_status: Optional[str]

    # Append-only audit trail; operator.add means LangGraph appends, never replaces.
    # Each node appends one ISO-8601 UTC entry. One entry per node, never grows unboundedly.
    event_log: Annotated[list[str], operator.add]
