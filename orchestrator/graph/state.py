# orchestrator/graph/state.py
# OrchestratorState — the REAL schema for Phases 2/3.
# ORCH-04: Typed string fields only. No unbounded list accumulator.
from typing import TypedDict, Annotated, Optional
import operator


def _merge_dicts(left: dict, right: dict) -> dict:
    """Reducer for node_models: new entries from right merge into left.

    Safe on checkpoint resume: resumed nodes add their entries to the
    existing set without erasing other nodes' entries.
    Pattern: {**left, **right}

    OBS-03 / ORCH-04: node_models is bounded (max 3 entries, one per node).
    It is NOT an unbounded accumulator. Each node writes exactly one key.
    """
    return {**left, **right}


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

    # OBS-03: per-node model attribution. Each node writes {"node_name": "model_id"}.
    # _merge_dicts reducer: new writes merge with existing entries (no erasure on resume).
    # Initialized as {} in initial_state (runner.py). Bounded: 3 entries max, one per node.
    # ORCH-04 compliant: NOT an unbounded accumulator.
    node_models: Annotated[dict, _merge_dicts]
