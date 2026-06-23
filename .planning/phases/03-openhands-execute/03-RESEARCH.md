# Phase 3: OpenHands Execute — Research

**Researched:** 2026-06-23
**Domain:** OpenHands SDK 1.21.0 in-process execution, asyncio.to_thread wrapping, per-job workspace isolation, max-iterations cap, EXEC-05 import isolation
**Confidence:** HIGH (all critical questions resolved by reading installed SDK source at `/Users/ohama/.local/share/uv/tools/openhands/lib/python3.12/site-packages/openhands/sdk/` and running live construction tests against the uv-tools Python)

---

## Summary

Phase 3 replaces `execute_stub` in `stub_graph.py` with a real `execute_node` that drives the OpenHands SDK in-process on `qwen-35b`. The SDK version installed is `openhands-sdk==1.21.0` (NOT 1.29.0 as earlier domain research assumed). All API signatures confirmed by reading the installed source directly.

The SDK's `Conversation.run()` is synchronous/blocking — it calls `litellm_completion` (sync) internally with a `threading.FIFOLock` for state access. It does not start its own asyncio event loop. Wrapping it with `asyncio.to_thread(conversation.run)` inside `execute_node` keeps the FastAPI/worker event loop (and GET /jobs/{id}/status) fully responsive during the multi-minute OpenHands run.

The `max_iteration_per_run` kwarg on `Conversation(...)` is the cap control. When the limit is hit and the agent is not `FINISHED`, `run()` sets `state.execution_status = ConversationExecutionStatus.ERROR` and emits a `ConversationErrorEvent(code="MaxIterationsReached")` — it does NOT raise. The stuck detector (`stuck_detection=True`) exits with `ConversationExecutionStatus.STUCK` similarly without raising. Both ERROR and STUCK are terminal (`is_terminal() == True`) and are readable after `run()` returns. `ConversationRunError` (a `RuntimeError`) is only raised when an actual exception occurs inside `agent.step()`.

EXEC-05 isolation: all `from openhands.*` imports live exclusively in `execution/openhands_adapter.py`. The `execute_node` function in `stub_graph.py` imports only `run_openhands` from the adapter. Tests can swap the adapter for a stub function without any `openhands.*` import anywhere in the test path — controlled by the existing `ORCHESTRATOR_TEST_GRAPH=1` / `build_test_graph()` path.

**Primary recommendation:** Install `openhands-sdk==1.21.0` + `openhands-tools==1.21.0` into the orchestrator venv via `uv add`. Create `execution/openhands_adapter.py` with `run_openhands(goal, plan, workspace_dir, max_iterations) -> dict` as the single call surface. Wrap it with `asyncio.to_thread` plus a wall-clock timeout inside `execute_node`. Write artifacts (research.md, plan.md, execution_transcript.jsonl) to `~/projs/langgraph-jobs/<job_id>/workspace/` at node entry (before running the agent) so they are always written even on failure.

---

## Standard Stack

### Core (Phase 3 additions)

| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| `openhands-sdk` | 1.21.0 | `LLM`, `Agent`, `Conversation`, `LocalWorkspace`, event types | The version actually installed in the uv-tools venv; provides the in-process SDK API |
| `openhands-tools` | 1.21.0 | `TerminalTool`, `FileEditorTool` executors (libtmux terminal sessions, file edit) | The TerminalExecutor (`impl.py`) lives here, NOT in openhands-sdk; without it the agent cannot run commands |

### Note on package structure (1.21.0)

In 1.21.0, `openhands.sdk.*` and `openhands.tools.*` are **separate packages** sharing the `openhands` namespace:

- `openhands-sdk==1.21.0` owns `openhands/sdk/` AND `openhands/tools/*/definition.py` (tool definitions, schemas)
- `openhands-tools==1.21.0` owns `openhands/tools/*/impl.py` (tool executors — the actual terminal/file I/O)

Installing only `openhands-sdk` gives you LLM/Agent/Conversation/tool schemas but tool executors will be missing. The agent will construct but `run()` will fail when a tool is called. **Install both.**

`openhands-workspace==1.11.1` is NOT needed for local workspace (`LocalWorkspace` is in `openhands-sdk`). Skip it.

### Supporting

| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| `pydantic` | >=2.12.5 | Already in orchestrator venv at 2.13.4 — compatible | Not an additional install; just confirms compatibility |

### Alternatives Considered

| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| `openhands-sdk==1.21.0` (in-process) | `openhands` CLI or `openhands-agent-server` | CLI is a subprocess; agent server is remote HTTP. In-process is the EXEC-01 requirement and avoids IPC complexity. |
| `openhands-tools==1.21.0` | Skip tools pkg; only install openhands-sdk | Without openhands-tools, TerminalExecutor is missing. The agent's `run()` would fail on the first terminal tool call. |
| `asyncio.to_thread` | `concurrent.futures.ThreadPoolExecutor` | Both work. `asyncio.to_thread` is stdlib 3.9+ and cleaner in async context. |

**Installation (Phase 3 additions):**

```bash
# From project root; .venv must be activated
cd /Users/ohama/projs/LangGraph_OpenHands

uv add "openhands-sdk==1.21.0" "openhands-tools==1.21.0"

# Verify imports work in the orchestrator venv:
.venv/bin/python -c "
import os; os.environ['OPENHANDS_SUPPRESS_BANNER'] = '1'
from openhands.sdk import LLM, Agent, Conversation
from openhands.sdk.workspace import LocalWorkspace
from openhands.sdk.tool import Tool
from openhands.tools.terminal import TerminalTool
from openhands.tools.file_editor import FileEditorTool
print('OK')
"
```

**Heavy transitive deps to expect from openhands-tools:**
- `browser-use>=0.8.0` (playwright-based browser automation — heavy, installs playwright chromium)
- `libtmux>=0.53.0` (required for TerminalExecutor — not optional)
- `tom-swe>=1.0.3`
- `litellm>=1.83.7` (from openhands-sdk)
- `fastmcp>=3.0.0`, `fakeredis[lua]`, etc.

The `browser-use` dep is pulled in by `openhands-tools` even though we don't use browser tools. This is a known cost. Accept it — the playwright chromium install is large (~300 MB) but one-time. The SDK's tool registration is lazy (only registers when imported), so browser tools won't be registered unless explicitly imported.

**pyproject.toml additions:**

```toml
[project]
dependencies = [
    # ... existing deps ...
    "openhands-sdk==1.21.0",
    "openhands-tools==1.21.0",
]
```

---

## Architecture Patterns

### Recommended File Structure (Phase 3 additions)

```
orchestrator/
├── execution/
│   ├── __init__.py
│   └── openhands_adapter.py    # NEW: ALL openhands.* imports live here (EXEC-05)
├── graph/
│   └── stub_graph.py           # MODIFY: replace execute_stub with real execute_node
└── .env                        # MODIFY: add EXECUTE_MODEL, EXECUTE_WORKSPACE_BASE,
                                #         EXECUTE_MAX_ITERATIONS, EXECUTE_TIMEOUT_SECS
```

### Pattern 1: openhands_adapter.py — The ONLY File Importing openhands.*

**What:** `execution/openhands_adapter.py` is the isolation boundary (EXEC-05). It exports one function: `run_openhands(goal, plan, workspace_dir, max_iterations, timeout_secs) -> dict`. No other file imports `openhands.*`.

**When to use:** Always. `execute_node` in `stub_graph.py` imports only `run_openhands`. Tests use `build_test_graph()` (which uses `execute_stub`) so never hit this import at all.

**Exact constructor signatures (from installed source):**

```python
# Source: openhands/sdk/llm/llm.py lines 135-400, field definitions
# openhands/sdk/agent/base.py lines 54-180
# openhands/sdk/conversation/impl/local_conversation.py lines 89-112
# openhands/sdk/conversation/conversation.py lines 109-132

from openhands.sdk import LLM, Agent, Conversation
from openhands.sdk.tool import Tool
from openhands.sdk.workspace import LocalWorkspace
from openhands.tools.terminal import TerminalTool
from openhands.tools.file_editor import FileEditorTool
from pydantic import SecretStr

# LLM constructor (key fields from LLM.model_fields):
llm = LLM(
    model="openai/qwen-35b",       # matches live agent_settings.json
    api_key=SecretStr("dummy"),    # or plain str — both accepted (SecretStr preferred)
    base_url="http://localhost:4000/v1",
    native_tool_calling=False,     # CRITICAL: qwen-35b uses XML non-native tool calling
    stream=False,                  # matches live agent_settings.json
    drop_params=True,              # matches live agent_settings.json
    modify_params=True,            # matches live agent_settings.json
    num_retries=2,                 # keep low — 35B wedge amplification risk
    timeout=300,                   # 5 min per LLM call (matches live settings)
    max_output_tokens=2048,        # bounded; 35B at ~15 tok/s → ~136s max
    max_input_tokens=32768,        # matches live agent_settings.json
)

# Agent constructor (from AgentBase.model_fields):
agent = Agent(
    llm=llm,
    tools=[
        Tool(name=TerminalTool.name),    # "terminal"
        Tool(name=FileEditorTool.name),  # "file_editor"
    ],
    # include_default_tools defaults to ["FinishTool", "ThinkTool"] — keep it
)

# Conversation constructor — creates LocalConversation for local workspace
# Source: conversation.py __new__, local_conversation.py __init__
convo = Conversation(
    agent=agent,
    workspace=workspace_dir,           # str or Path; mkdir'd automatically
    persistence_dir=workspace_dir,     # optional; enables event log persistence
                                       # Conversation appends <id>.hex to the path
    max_iteration_per_run=max_iterations,
    stuck_detection=True,              # exits loop with STUCK status — see Pattern 4
    visualizer=None,                   # disable Rich terminal output
    delete_on_close=True,              # clean up tool executors on close
)
```

**Key kwargs NOT needed for our use case:** `plugins`, `conversation_id`, `callbacks`, `token_callbacks`, `hook_config`, `secrets`, `tags`, `cipher`.

**run() method (from base.py line 164, local_conversation.py line 745):**

```python
# run() is SYNCHRONOUS. No async. No asyncio loop started internally.
# Blocks the calling thread until the agent finishes, hits max_iteration_per_run, 
# gets stuck, or raises ConversationRunError.

convo.send_message(goal)   # queue the task
convo.run()                # block until terminal execution_status

# After run() returns, inspect state:
status = convo.state.execution_status   # ConversationExecutionStatus enum
# Terminal values: FINISHED (success), ERROR (max iterations or exception), STUCK
```

### Pattern 2: run_openhands() Adapter Function

```python
# execution/openhands_adapter.py
# ALL openhands.* imports are HERE — nowhere else in the codebase (EXEC-05).
# This module is NOT imported at process start; it is imported lazily by execute_node
# only when the real graph builder is used.

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

# SDK imports — confined to this file
from openhands.sdk import LLM, Agent, Conversation
from openhands.sdk.conversation.state import ConversationExecutionStatus
from openhands.sdk.conversation.exceptions import ConversationRunError
from openhands.sdk.event import Event
from openhands.sdk.event.llm_convertible import ActionEvent, MessageEvent, ObservationEvent
from openhands.sdk.tool import Tool
from openhands.tools.terminal import TerminalTool
from openhands.tools.file_editor import FileEditorTool
from pydantic import SecretStr


def run_openhands(
    goal: str,
    plan: str,
    workspace_dir: str,
    max_iterations: int = 20,
    timeout_secs: float = 600.0,
) -> dict:
    """Run OpenHands agent synchronously on the given goal + plan.

    Called via asyncio.to_thread() from execute_node to avoid blocking the
    FastAPI event loop (EXEC-02). The wall-clock timeout is enforced by
    asyncio.wait_for() around the to_thread call in execute_node.

    Args:
        goal: The original goal string from OrchestratorState.
        plan: The plan text from OrchestratorState (plan_node output).
        workspace_dir: Per-job workspace directory. Created before calling if needed.
            Should be: ~/projs/langgraph-jobs/<job_id>/workspace/
        max_iterations: Maximum agent loop iterations (EXEC-04).
            At cap, run() sets state to ERROR (not FINISHED) and returns normally.
        timeout_secs: Wall-clock timeout — enforced by asyncio.wait_for() in
            execute_node, NOT here. Passed for logging/observability only.

    Returns:
        dict with keys:
            execution_result  (str): Summary text for OrchestratorState.execution_result
            execution_status  (str): "success" | "partial" | "failed"
            iterations_used   (int): Number of agent iterations consumed
            final_sdk_status  (str): raw ConversationExecutionStatus.value string
            event_count       (int): total events in the conversation
    """
    Path(workspace_dir).mkdir(parents=True, exist_ok=True)

    # Build LLM — mirrors live agent_settings.json exactly
    llm = LLM(
        model=os.getenv("EXECUTE_MODEL", "openai/qwen-35b"),
        api_key=SecretStr(os.getenv("LITELLM_API_KEY", "dummy")),
        base_url=os.getenv("LITELLM_BASE_URL", "http://localhost:4000/v1"),
        native_tool_calling=False,   # 35B uses non-native XML tool calling
        stream=False,
        drop_params=True,
        modify_params=True,
        num_retries=2,               # 2 retries; avoid amplifying wedge
        timeout=300,
        max_output_tokens=2048,
        max_input_tokens=32768,
    )

    agent = Agent(
        llm=llm,
        tools=[
            Tool(name=TerminalTool.name),    # "terminal"
            Tool(name=FileEditorTool.name),  # "file_editor"
        ],
        # FinishTool and ThinkTool are included by default (include_default_tools)
    )

    convo = Conversation(
        agent=agent,
        workspace=workspace_dir,
        persistence_dir=workspace_dir,  # event log persisted alongside workspace files
        max_iteration_per_run=max_iterations,
        stuck_detection=True,
        visualizer=None,
        delete_on_close=True,
    )

    try:
        # Compose the task message: goal + plan so agent has full context
        task_prompt = (
            f"You are executing the following task autonomously.\n\n"
            f"GOAL:\n{goal}\n\n"
            f"IMPLEMENTATION PLAN:\n{plan}\n\n"
            "Execute the plan step by step using the available tools. "
            "When done, use the finish tool with a clear summary of what you accomplished."
        )
        convo.send_message(task_prompt)
        convo.run()  # BLOCKING — runs in a thread via asyncio.to_thread

        final_status = convo.state.execution_status
        events = list(convo.state.events)
        iteration_count = _count_agent_iterations(events)

        # Extract result from the last FinishAction if agent finished cleanly
        execution_result = _extract_finish_message(events)
        if not execution_result:
            execution_result = f"Agent completed with status: {final_status.value}"

        # Map SDK status → our status string
        if final_status == ConversationExecutionStatus.FINISHED:
            our_status = "success"
        elif final_status == ConversationExecutionStatus.ERROR:
            # ERROR = max iterations hit OR a recoverable exception during step
            our_status = "partial"
        elif final_status == ConversationExecutionStatus.STUCK:
            our_status = "partial"
        else:
            # IDLE/PAUSED/RUNNING after run() — unexpected; treat as partial
            our_status = "partial"

        return {
            "execution_result": execution_result,
            "execution_status": our_status,
            "iterations_used": iteration_count,
            "final_sdk_status": final_status.value,
            "event_count": len(events),
        }

    except ConversationRunError as exc:
        # ConversationRunError wraps the original exception with conversation metadata.
        # Source: local_conversation.py lines 873-888.
        # Raised when agent.step() raises an unhandled exception (NOT on max iterations).
        return {
            "execution_result": f"Execution failed: {exc.original_exception}",
            "execution_status": "failed",
            "iterations_used": 0,
            "final_sdk_status": "error",
            "event_count": 0,
        }
    finally:
        # Always close — cleans up tmux sessions and tool executors
        try:
            convo.close()
        except Exception:
            pass


def _count_agent_iterations(events: list[Event]) -> int:
    """Count agent iterations = number of ActionEvents from source='agent'."""
    from openhands.sdk.event.llm_convertible import ActionEvent
    return sum(1 for e in events if isinstance(e, ActionEvent) and e.source == "agent")


def _extract_finish_message(events: list[Event]) -> str | None:
    """Extract the final FinishAction message from the event log, if present."""
    from openhands.sdk.event.llm_convertible import ActionEvent
    for event in reversed(events):
        if isinstance(event, ActionEvent) and event.tool_name == "finish":
            if event.action and hasattr(event.action, "message"):
                return event.action.message
    return None


def serialize_transcript(events: list[Event], output_path: str) -> None:
    """Write conversation events as JSONL to output_path (OBS-02).

    Each line is one event serialized with model_dump_json().
    Events include MessageEvent, ActionEvent, ObservationEvent, etc.
    """
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        for event in events:
            try:
                f.write(event.model_dump_json() + "\n")
            except Exception:
                # Skip events that fail serialization rather than crashing
                f.write(json.dumps({"error": "serialization_failed",
                                    "type": type(event).__name__}) + "\n")
```

### Pattern 3: execute_node — Async Wrapper in stub_graph.py

**What:** `execute_node` is the async LangGraph node that replaces `execute_stub`. It wraps the blocking `run_openhands()` call via `asyncio.to_thread` with a wall-clock timeout, writes artifacts to the per-job workspace, and returns partial state for OrchestratorState.

**The critical rule:** `execute_node` imports `run_openhands` from `execution.openhands_adapter` — but this import happens at the module level of `stub_graph.py`. This would pull in `openhands.*` when `stub_graph.py` is imported (which main.py always does). To preserve EXEC-05 (openhands imports only in adapter), the import must be guarded: either use a lazy import inside the function, or put the real execute_node in a separate file that is only imported when building the real graph.

**Recommended approach: lazy import inside execute_node function body.**

```python
# In orchestrator/graph/stub_graph.py — replace execute_stub with this function
# in build_stub_graph() only (build_test_graph() keeps execute_stub unchanged).

async def execute_node(state: OrchestratorState) -> dict:
    """REAL execute node: drives OpenHands SDK in-process on qwen-35b.

    EXEC-01: in-process SDK on qwen-35b via LiteLLM :4000.
    EXEC-02: asyncio.to_thread wraps the blocking Conversation.run().
    EXEC-03: per-job workspace at EXECUTE_WORKSPACE_BASE/<job_id>/workspace/.
    EXEC-04: max_iterations cap from EXECUTE_MAX_ITERATIONS (default 20).
    EXEC-05: all openhands.* imports are inside execution/openhands_adapter.py;
             imported lazily here so build_test_graph() path never touches them.
    OBS-02: artifacts written to workspace BEFORE running the agent.
    OBS-03: node_models records the execute model.
    """
    import asyncio
    import os
    from pathlib import Path
    from datetime import datetime, timezone

    # Lazy import — openhands.* only loads when this node actually runs
    # (i.e., never in test path which uses build_test_graph()).
    from orchestrator.execution.openhands_adapter import (
        run_openhands,
        serialize_transcript,
    )

    model = os.getenv("EXECUTE_MODEL", "openai/qwen-35b")
    max_iterations = int(os.getenv("EXECUTE_MAX_ITERATIONS", "20"))
    timeout_secs = float(os.getenv("EXECUTE_TIMEOUT_SECS", "900"))  # 15 min wall clock

    # EXEC-03: per-job isolated workspace
    workspace_base = os.getenv(
        "EXECUTE_WORKSPACE_BASE",
        str(Path.home() / "projs" / "langgraph-jobs")
    )
    job_id = state["job_id"]
    job_workspace = str(Path(workspace_base) / job_id / "workspace")
    Path(job_workspace).mkdir(parents=True, exist_ok=True)

    # OBS-02: write research.md and plan.md to workspace BEFORE running the agent
    # (so they exist even if execution fails)
    _write_artifact(job_workspace, "research.md", state.get("research_findings") or "")
    _write_artifact(job_workspace, "plan.md", state.get("plan") or "")

    started_at = datetime.now(timezone.utc).isoformat()
    result = {}
    try:
        # EXEC-02: blocking run() in a thread pool — event loop stays responsive
        result = await asyncio.wait_for(
            asyncio.to_thread(
                run_openhands,
                goal=state["goal"],
                plan=state.get("plan") or "",
                workspace_dir=job_workspace,
                max_iterations=max_iterations,
                timeout_secs=timeout_secs,
            ),
            timeout=timeout_secs,  # wall-clock kill if thread exceeds timeout
        )
    except asyncio.TimeoutError:
        result = {
            "execution_result": f"Execution timed out after {timeout_secs}s",
            "execution_status": "failed",
            "iterations_used": 0,
            "final_sdk_status": "timeout",
            "event_count": 0,
        }
    except Exception as exc:
        result = {
            "execution_result": f"Unexpected error: {exc}",
            "execution_status": "failed",
            "iterations_used": 0,
            "final_sdk_status": "error",
            "event_count": 0,
        }

    completed_at = datetime.now(timezone.utc).isoformat()

    # OBS-02: write execution summary to workspace
    summary_lines = [
        f"# Execution Summary",
        f"job_id: {job_id}",
        f"model: {model}",
        f"started_at: {started_at}",
        f"completed_at: {completed_at}",
        f"status: {result.get('execution_status')}",
        f"sdk_status: {result.get('final_sdk_status')}",
        f"iterations_used: {result.get('iterations_used')}",
        f"event_count: {result.get('event_count')}",
        f"\n## Result\n{result.get('execution_result', '')}",
    ]
    _write_artifact(job_workspace, "execution_summary.md", "\n".join(summary_lines))

    return {
        "execution_result": result.get("execution_result", ""),
        "execution_status": result.get("execution_status", "failed"),
        "node_models": {"execute": model},  # OBS-03
        "event_log": [
            f"{completed_at} execute_node: complete "
            f"status={result.get('execution_status')} "
            f"iterations={result.get('iterations_used')} "
            f"sdk_status={result.get('final_sdk_status')}"
        ],
    }


def _write_artifact(workspace_dir: str, filename: str, content: str) -> None:
    """Write a text artifact to the workspace directory. Silent on failure."""
    try:
        path = Path(workspace_dir) / filename
        path.write_text(content, encoding="utf-8")
    except Exception:
        pass
```

### Pattern 4: Max-Iterations Cap Behavior (EXEC-04)

**Confirmed from source: `local_conversation.py` lines 850-872:**

```python
# When iteration >= max_iteration_per_run AND agent is NOT FINISHED:
if iteration >= self.max_iteration_per_run:
    if self._state.execution_status == ConversationExecutionStatus.FINISHED:
        break  # Agent finished on the final iteration — normal exit
    # Otherwise:
    error_msg = f"Agent reached maximum iterations limit ({self.max_iteration_per_run})."
    self._state.execution_status = ConversationExecutionStatus.ERROR
    self._on_event(ConversationErrorEvent(source="environment",
                                           code="MaxIterationsReached",
                                           detail=error_msg))
    break  # run() RETURNS NORMALLY (does not raise)
```

**Key facts:**
- Cap hit → `ConversationExecutionStatus.ERROR` (terminal) → `is_terminal() == True`
- `run()` returns normally (no raise)
- We map ERROR → `"partial"` in our `execution_status` field
- Stuck detection similarly sets `STUCK` and breaks without raising
- Only actual exceptions inside `agent.step()` cause `ConversationRunError` to be raised

**Recommended max_iterations value:** Start at 20. For simple tasks (write a file, run a command) 35B may finish in 3-8 iterations. 20 gives headroom while bounding worst-case at ~20 × 15s/iter × (LLM call + tool) ≈ 5-10 minutes.

### Pattern 5: Workspace Layout (EXEC-03, OBS-02)

```
~/projs/langgraph-jobs/
└── <job_id>/
    └── workspace/
        ├── research.md           # written by execute_node BEFORE running (from state)
        ├── plan.md               # written by execute_node BEFORE running (from state)
        ├── execution_summary.md  # written by execute_node AFTER running
        ├── <job_id>.hex/         # Conversation persistence dir (created by SDK)
        │   ├── base_state.json   # ConversationState snapshot (autosaved)
        │   └── events/           # per-event JSON files (EventLog)
        └── [agent-created files] # whatever the agent creates during execution
```

**Why write research.md and plan.md before running:** The agent has access to the workspace directory. If the task involves writing to workspace files, the agent can read research.md and plan.md as context. More importantly, if execution fails mid-run, the artifacts still exist for debugging.

**The `persistence_dir` behavior (confirmed from `local_conversation.py` lines 184-191):**
The Conversation appends `<id>.hex` to the `persistence_dir` path. So `persistence_dir=workspace_dir` creates `<workspace_dir>/<uuid>.hex/` for the event log and base_state. This is self-contained within the job workspace.

**Environment variables to add to .env:**

```bash
# .env additions for Phase 3
EXECUTE_MODEL=openai/qwen-35b
EXECUTE_WORKSPACE_BASE=/Users/ohama/projs/langgraph-jobs
EXECUTE_MAX_ITERATIONS=20
EXECUTE_TIMEOUT_SECS=900
```

### Pattern 6: Artifact Extraction — Transcript (OBS-02)

The SDK's `EventLog` is iterable via `list(convo.state.events)`. Each event is a Pydantic model with `model_dump_json()` method.

**However:** The EventLog is file-backed in the persistence_dir — it writes events to `<persistence_dir>/<id>.hex/events/` as individual JSON files during `run()`. Reading `list(convo.state.events)` after `run()` returns all events from in-memory + on-disk index.

**Alternative to reading events from memory:** After `run()`, the persistence dir has all events on disk as individual JSON files. The `execution_transcript.jsonl` can be assembled from those files OR from `list(convo.state.events)` before `convo.close()`.

**Call `serialize_transcript` BEFORE `convo.close()`** because `close()` cleans up executors and may affect state access.

**In-transcript artifact writes are optional:** The agent creates files in `workspace_dir` during `run()`. The key files are `research.md`, `plan.md` (pre-written), and whatever the agent creates.

### Pattern 7: Integration Test Shape (03-03)

```python
# tests/test_execute_integration.py
import asyncio
import os
import pytest
import time
from pathlib import Path
from fastapi.testclient import TestClient

# Skip if ORCHESTRATOR_TEST_GRAPH=1 is set (offline/CI tests only use stubs)
# Full integration test requires LiteLLM :4000 + qwen-35b running

@pytest.mark.skipif(
    os.getenv("ORCHESTRATOR_TEST_GRAPH") == "1",
    reason="Integration test requires live LiteLLM; set ORCHESTRATOR_TEST_GRAPH=0 to run"
)
def test_full_pipeline_to_done():
    """Submit a goal → assert DONE, workspace files written, event loop responsive."""
    from orchestrator.main import app
    from pathlib import Path

    with TestClient(app) as client:
        # Submit a simple, deterministic goal
        resp = client.post("/goals", json={"goal": "Write 'hello world' to hello.txt"})
        assert resp.status_code == 202
        job_id = resp.json()["job_id"]

        # Poll status — must stay sub-second responsive (EXEC-02 check)
        # The real check: concurrent GET /status during EXECUTING phase returns <1s
        start = time.time()
        for _ in range(120):  # up to 120s total
            time.sleep(1.0)
            t0 = time.time()
            status_resp = client.get(f"/jobs/{job_id}/status")
            assert time.time() - t0 < 1.0, "Status endpoint blocked (event loop issue)"
            status = status_resp.json()["status"]
            if status in ("DONE", "FAILED"):
                break

        assert status == "DONE", f"Expected DONE, got {status}"

        # Verify workspace artifacts exist
        workspace_base = os.getenv("EXECUTE_WORKSPACE_BASE",
                                    str(Path.home() / "projs" / "langgraph-jobs"))
        job_ws = Path(workspace_base) / job_id / "workspace"
        assert (job_ws / "research.md").exists(), "research.md not written"
        assert (job_ws / "plan.md").exists(), "plan.md not written"
        assert (job_ws / "execution_summary.md").exists(), "execution_summary.md not written"


def test_execute_stub_offline():
    """Offline test: ORCHESTRATOR_TEST_GRAPH=1 path completes in <10s."""
    os.environ["ORCHESTRATOR_TEST_GRAPH"] = "1"
    from orchestrator.main import app
    with TestClient(app) as client:
        resp = client.post("/goals", json={"goal": "stub test"})
        assert resp.status_code == 202
        job_id = resp.json()["job_id"]
        import time
        for _ in range(20):
            time.sleep(0.5)
            s = client.get(f"/jobs/{job_id}/status").json()["status"]
            if s == "DONE":
                break
        assert s == "DONE"
```

### Minimal Standalone Smoke Test (Run Before Wiring the Graph)

```python
#!/usr/bin/env python3
"""smoke_test_openhands.py — verify SDK API before wiring execute_node.

Run with: .venv/bin/python smoke_test_openhands.py
Requires: qwen-35b via LiteLLM :4000 running.
"""
import os
import tempfile
import asyncio
from pathlib import Path

os.environ["OPENHANDS_SUPPRESS_BANNER"] = "1"

from openhands.sdk import LLM, Agent, Conversation
from openhands.sdk.conversation.state import ConversationExecutionStatus
from openhands.sdk.tool import Tool
from openhands.tools.terminal import TerminalTool
from openhands.tools.file_editor import FileEditorTool
from pydantic import SecretStr

def run_smoke_test():
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = str(Path(tmpdir) / "workspace")

        llm = LLM(
            model="openai/qwen-35b",
            api_key=SecretStr("dummy"),
            base_url="http://localhost:4000/v1",
            native_tool_calling=False,
            stream=False,
            drop_params=True,
            modify_params=True,
            num_retries=1,
            timeout=120,
            max_output_tokens=512,
        )
        agent = Agent(
            llm=llm,
            tools=[Tool(name=TerminalTool.name), Tool(name=FileEditorTool.name)],
        )
        convo = Conversation(
            agent=agent,
            workspace=workspace,
            max_iteration_per_run=3,   # low cap for smoke test
            stuck_detection=True,
            visualizer=None,
            delete_on_close=True,
        )
        try:
            convo.send_message("Write the word 'hello' to a file named hello.txt, then finish.")
            convo.run()

            status = convo.state.execution_status
            events = list(convo.state.events)
            print(f"SDK status: {status.value}")
            print(f"Event count: {len(events)}")
            print(f"is_terminal: {status.is_terminal()}")

            # Check for finish action
            for e in reversed(events):
                from openhands.sdk.event.llm_convertible import ActionEvent
                if isinstance(e, ActionEvent) and e.tool_name == "finish":
                    print(f"Finish message: {e.action.message[:100] if e.action else 'N/A'}")
                    break

            # Check workspace
            hello_path = Path(workspace) / "hello.txt"
            print(f"hello.txt exists: {hello_path.exists()}")
            if hello_path.exists():
                print(f"hello.txt content: {hello_path.read_text()[:50]}")
        finally:
            convo.close()

if __name__ == "__main__":
    run_smoke_test()
```

### Anti-Patterns to Avoid

- **Importing `openhands.*` at module level in `stub_graph.py`** — breaks EXEC-05. `build_test_graph()` path must never load openhands. Use lazy import inside `execute_node` function body.
- **Calling `convo.run()` directly in an async def without to_thread** — blocks the event loop. All LLM calls inside the SDK are synchronous (litellm_completion is sync). This would freeze GET /jobs/{id}/status for the entire execution time (minutes).
- **Not calling `convo.close()` after run()** — leaks tmux sessions (TerminalExecutor uses libtmux). Always call in a `finally` block.
- **Setting `num_retries=5` (the SDK default)** — on a wedged qwen-35b, each retry re-calls the model. 5 retries × 120s timeout = 10 minutes of wedge amplification. Use `num_retries=2` maximum.
- **Setting `max_iteration_per_run` to 0 or very high** — 0 is invalid (Pydantic `gt=0` constraint on `ConversationState.max_iterations`); very high (>50) risks runaway cost and memory exhaustion on 35B.
- **Using `persistence_dir=None`** — without persistence_dir, ConversationState uses InMemoryFileStore. Events are lost if the thread dies. Always pass persistence_dir for OBS-02.
- **Checking `convo.state.execution_status == "error"` as a string** — it's an enum. Compare as `ConversationExecutionStatus.ERROR` or use `status.is_terminal()`.
- **Reading events AFTER `convo.close()`** — `close()` calls `executor.close()` on all tools (kills tmux sessions). Event log should still be readable from disk (LocalFileStore), but it's cleaner to read before close.

---

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Async LLM calling in agent loop | Custom asyncio agent | `Conversation.run()` via `asyncio.to_thread` | The SDK handles tool dispatch, context windowing, stuck detection, retry, and event logging internally |
| Terminal session management | subprocess.Popen + stdout/stderr | TerminalTool (openhands-tools) | TerminalExecutor uses libtmux persistent sessions; handles ansi/escape codes, long-running commands, pwd tracking |
| Event serialization to JSONL | Custom JSON encoding | `event.model_dump_json()` | Events are Pydantic models; model_dump_json handles UUID/datetime/SecretStr serialization |
| Max-iterations enforcement | External counter + timer | `max_iteration_per_run` kwarg on Conversation | Built into the run() loop; handles the FINISHED edge case (agent finishes on last iteration = success, not error) |
| Stuck detection | Repeated-output detector | `stuck_detection=True` on Conversation | SDK's StuckDetector checks action/observation patterns; exits gracefully with STUCK status |
| Tool-call XML parsing | Custom XML parser for non-native tool calling | `native_tool_calling=False` on LLM | The SDK's NonNativeToolCallingMixin handles XML-format tool calls that 35B produces |

**Key insight:** The biggest risk in Phase 3 is around `native_tool_calling=False`. With 35B, the agent produces XML-formatted tool calls (not OpenAI function-call JSON). The SDK handles this via `NonNativeToolCallingMixin` on the `LLM` class, which parses the XML format. **Do not set `native_tool_calling=True` for qwen-35b** — it will produce malformed function-call JSON that the SDK cannot parse.

---

## Common Pitfalls

### Pitfall 1: openhands.* Import at Module Level Breaks EXEC-05

**What goes wrong:** If `execute_node` or any function it imports at the top of `stub_graph.py` does `from openhands.sdk import ...`, then every test that imports `stub_graph` (including `build_test_graph()` path) loads the OpenHands SDK at test startup. This is slow (several seconds), produces banner output and litellm warnings, and makes unit tests depend on openhands being installed.

**Why it happens:** Python module imports are global and happen at import time, not at call time.

**How to avoid:** Use a lazy import inside the `execute_node` function body:
```python
async def execute_node(state):
    from orchestrator.execution.openhands_adapter import run_openhands  # lazy
    ...
```
The import runs only when `execute_node` is called, which only happens in the production graph path (`build_stub_graph()`), never in `build_test_graph()`.

**Warning signs:** Tests using `ORCHESTRATOR_TEST_GRAPH=1` are slow or print banner/litellm warnings.

### Pitfall 2: asyncio.wait_for Timeout Does NOT Kill the Thread

**What goes wrong:** `asyncio.wait_for(asyncio.to_thread(convo.run), timeout=900)` raises `asyncio.TimeoutError` after 900 seconds, but the thread running `convo.run()` is NOT killed. It continues running in the background. If qwen-35b is wedged and each LLM call hangs, the thread may run indefinitely.

**Why it happens:** Python threads cannot be forcibly killed from the outside. `asyncio.to_thread` submits to the default thread pool. Cancellation of the coroutine does not kill the underlying thread.

**How to mitigate:**
- Set `timeout=300` on the LLM (per-call timeout), so each litellm_completion call gets a 5-minute timeout. This bounds individual calls.
- Set `EXECUTE_TIMEOUT_SECS=900` as a wall-clock timeout. When asyncio.TimeoutError fires, the event loop continues and the job status goes to FAILED. The orphaned thread will eventually timeout via the LLM's per-call timeout.
- The worker event loop and API remain responsive (EXEC-02 is met) regardless of the orphaned thread.

**Warning signs:** After a timeout, CPU usage stays elevated for up to 300s (1 LLM call worth) as the orphaned thread waits for the current litellm call to complete.

**Do NOT use:** `asyncio.to_thread` with `timeout=None` and no LLM timeout — this creates an unkillable hang.

### Pitfall 3: qwen-35B XML Tool-Call Failures

**What goes wrong:** With `native_tool_calling=False`, the agent produces XML-formatted tool calls. If qwen-35b generates malformed XML (common — 35B is weaker than 122B at structured output), the SDK's `NonNativeToolCallingMixin` may fail to parse the tool call. The agent may then generate plain text instead of a tool call, triggering stuck detection after `_stuck_thresholds.monologue` repeated monologue events.

**Why it happens:** `qwen-35b` is a 3.5B parameter model (despite the name); it lacks the instruction-following precision of larger models. Under memory pressure or with complex prompts, it may hallucinate tool names or produce incomplete XML.

**How to mitigate:**
- Keep the task prompt concise (< 400 words). The adapter's `task_prompt` in Pattern 2 is already concise.
- `max_iteration_per_run=20` bounds the maximum loop count before ERROR.
- `stuck_detection=True` catches infinite monologue loops.
- `"partial"` status (mapped from ERROR/STUCK) lets the worker set job DONE rather than FAILED, so the user still gets a result.

**Warning signs:** `final_sdk_status` in execution_summary.md shows `"stuck"` frequently; `iterations_used` is always equal to `max_iterations`.

### Pitfall 4: Banner and litellm Warnings Polluting Logs

**What goes wrong:** Importing openhands.sdk prints the startup banner:
```
+----------------------------------------------------------------------+
|  OpenHands SDK v1.21.0  ...
```
Additionally, litellm prints botocore and sagemaker warnings at import time:
```
litellm: could not pre-load bedrock-runtime response stream shape — Bedrock event-stream decoding will be unavailable. Error: No module named 'botocore'
```

**Why it happens:** The banner is printed in `openhands/sdk/__init__.py` via `_print_banner()`. The litellm warnings come from litellm's `common_utils.py` at import.

**How to avoid:**
- Set `OPENHANDS_SUPPRESS_BANNER=1` in the service environment (`.env` file or launchd plist).
- The botocore/sagemaker warnings are harmless (we don't use AWS). They go to stderr; filter at the logging configuration level or ignore them.
- **Do NOT suppress via `warnings.filterwarnings`** — this would also suppress legitimate SDK warnings.

**Warning signs:** Banner text appearing in uvicorn stdout; litellm warnings in uvicorn logs.

### Pitfall 5: Workspace Dir Is the Orchestrator Source Dir

**What goes wrong:** If `workspace_dir` is accidentally set to the orchestrator project dir (e.g., `"./"` or `os.getcwd()`), the OpenHands agent will run terminal commands in the orchestrator source tree, potentially modifying or deleting source files.

**Why it happens:** The `Conversation(workspace=...)` parameter sets the agent's working directory. If it defaults to `"."` or is read from an unset env var, it becomes the CWD of the orchestrator process.

**How to avoid:**
- Always compute `job_workspace` as `Path(workspace_base) / job_id / "workspace"` where `workspace_base` defaults to `~/projs/langgraph-jobs` (clearly outside the orchestrator source tree).
- Set `EXECUTE_WORKSPACE_BASE` explicitly in `.env`.
- Add an assertion in `execute_node`: `assert str(Path(job_workspace).resolve()) != str(Path.cwd())`.

**Warning signs:** Source files are modified or deleted; the agent's terminal commands show `ls` output with `orchestrator/`, `tests/`, etc.

### Pitfall 6: LiteLLM botocore Warning is Harmless (CONFIRMED)

**Confirmed:** The `litellm: could not pre-load bedrock-runtime response stream shape — Bedrock event-stream decoding will be unavailable. Error: No module named 'botocore'` and `sagemaker` variant warnings are harmless. They indicate AWS Bedrock/SageMaker streaming is not available, which we don't use. They fire at import time and do not repeat. No action needed beyond filtering them out of logs.

### Pitfall 7: Don't Read Events After convo.close()

**What goes wrong:** Calling `list(convo.state.events)` after `convo.close()` may work (events are in-memory in EventLog) but is risky — close() can modify state. If `delete_on_close=True` (default), close() also deletes workspace files after cleanup.

**IMPORTANT:** `delete_on_close` on `LocalConversation` refers to cleaning up **tool executors** (closing tmux sessions etc.), NOT to deleting the workspace directory. The workspace files (research.md, plan.md, agent-created files) are NOT deleted by close(). Only executor resources are freed.

**How to avoid:** Read `list(convo.state.events)` and call `serialize_transcript()` BEFORE `convo.close()` in the `finally` block.

---

## Code Examples

### LLM + Agent + Conversation Construction (Verified by Live Test)

```python
# Source: tested with uv-tools Python against installed openhands-sdk==1.21.0
# All constructor kwargs verified against LLM.model_fields, Agent.model_fields,
# Conversation.__new__ signature in conversation.py

import os
os.environ["OPENHANDS_SUPPRESS_BANNER"] = "1"

from openhands.sdk import LLM, Agent, Conversation
from openhands.sdk.tool import Tool
from openhands.tools.terminal import TerminalTool
from openhands.tools.file_editor import FileEditorTool
from pydantic import SecretStr

llm = LLM(
    model="openai/qwen-35b",
    api_key=SecretStr("dummy"),
    base_url="http://localhost:4000/v1",
    native_tool_calling=False,  # qwen-35b uses XML-format non-native tool calling
    stream=False,
    drop_params=True,
    modify_params=True,
    num_retries=2,
    timeout=300,
    max_output_tokens=2048,
    max_input_tokens=32768,
)

agent = Agent(
    llm=llm,
    tools=[
        Tool(name=TerminalTool.name),    # "terminal" — confirmed from installed source
        Tool(name=FileEditorTool.name),  # "file_editor" — confirmed
    ],
    # include_default_tools=["FinishTool", "ThinkTool"] is the default — leave it
)

# Conversation factory: local workspace → returns LocalConversation
convo = Conversation(
    agent=agent,
    workspace="/path/to/job_workspace",   # string, Path, or LocalWorkspace
    persistence_dir="/path/to/job_workspace",  # optional; SDK appends /<id>.hex/
    max_iteration_per_run=20,             # EXEC-04 cap
    stuck_detection=True,
    visualizer=None,
    delete_on_close=True,
)
# convo is LocalConversation (confirmed: type(convo).__name__ == 'LocalConversation')
# convo.state.execution_status is ConversationExecutionStatus.IDLE initially

convo.send_message("your task here")
convo.run()   # BLOCKING sync call

status = convo.state.execution_status  # enum value
print(status.value)     # "finished" | "error" | "stuck"
print(status.is_terminal())  # True for finished/error/stuck
events = list(convo.state.events)  # read BEFORE close()
convo.close()
```

### ConversationExecutionStatus Values (Verified)

```python
# Source: openhands/sdk/conversation/state.py lines 46-77
from openhands.sdk.conversation.state import ConversationExecutionStatus

# All values and is_terminal() results (confirmed):
# ConversationExecutionStatus.IDLE      -> is_terminal() = False
# ConversationExecutionStatus.RUNNING   -> is_terminal() = False
# ConversationExecutionStatus.PAUSED    -> is_terminal() = False
# ConversationExecutionStatus.WAITING_FOR_CONFIRMATION -> is_terminal() = False
# ConversationExecutionStatus.FINISHED  -> is_terminal() = True   (normal completion)
# ConversationExecutionStatus.ERROR     -> is_terminal() = True   (max iter or exception)
# ConversationExecutionStatus.STUCK     -> is_terminal() = True   (stuck detection)
# ConversationExecutionStatus.DELETING  -> is_terminal() = False
```

### Extracting Result Text from Event Log

```python
# Source: openhands/sdk/tool/builtins/finish.py
# FinishAction has a 'message' field (str). The agent calls finish tool to signal done.
# FinishAction events appear as ActionEvent with tool_name == "finish".

from openhands.sdk.event.llm_convertible import ActionEvent

def get_finish_message(events: list) -> str | None:
    for event in reversed(events):  # latest first
        if isinstance(event, ActionEvent) and event.tool_name == "finish":
            if event.action and hasattr(event.action, "message"):
                return event.action.message
    return None
```

### Event Serialization for JSONL Transcript

```python
# Each event is a Pydantic model; model_dump_json() produces JSON string.
# Events include: MessageEvent, ActionEvent, ObservationEvent, AgentErrorEvent, etc.

with open("execution_transcript.jsonl", "w") as f:
    for event in list(convo.state.events):  # BEFORE convo.close()
        f.write(event.model_dump_json() + "\n")
```

### asyncio.to_thread Pattern for execute_node (EXEC-02)

```python
import asyncio

async def execute_node(state):
    from orchestrator.execution.openhands_adapter import run_openhands  # lazy import
    
    timeout_secs = 900.0
    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(
                run_openhands,
                goal=state["goal"],
                plan=state.get("plan") or "",
                workspace_dir="/path/to/workspace",
                max_iterations=20,
            ),
            timeout=timeout_secs,
        )
    except asyncio.TimeoutError:
        result = {"execution_result": "Timed out", "execution_status": "failed", ...}
    # Event loop is responsive during the entire Conversation.run() execution
    # GET /jobs/{id}/status returns in <10ms while the agent is running
```

---

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| openhands-sdk v1.29.0 (domain research assumed) | openhands-sdk==1.21.0 (actually installed) | N/A — earlier research was wrong | All API signatures must be verified against 1.21.0, not 1.29.0 |
| `LLMAgentSettings` class | `OpenHandsAgentSettings` (renamed) | v1.19.0 | `LLMAgentSettings` deprecated in 1.19.0, removed in 1.22.0; do not use |
| `native_tool_calling=True` default | Explicitly `native_tool_calling=False` for qwen-35b | Existing config (agent_settings.json) | Required for 35B; 35B cannot produce valid OpenAI function-call JSON |
| `Conversation(workspace="workspace/project")` default | Explicit per-job path | Phase 3 new | Default would put ALL jobs in the same dir, mixing artifacts |

**Deprecated/outdated:**
- `LLMAgentSettings`: deprecated 1.19.0, removed in 1.22.0. Import `OpenHandsAgentSettings` instead (but for Phase 3 we don't use settings classes — we construct LLM/Agent directly).
- `ConversationSettings.confirmation_mode` / `security_analyzer` fields: moved from `VerificationSettings` to `ConversationSettings` in 1.17.0. Not relevant to Phase 3 (we never set these).

---

## Open Questions

1. **openhands-tools browser-use dependency install time**
   - What we know: `openhands-tools==1.21.0` depends on `browser-use>=0.8.0` which pulls playwright and may trigger a `playwright install` step (downloads Chromium).
   - What's unclear: Whether `uv add openhands-tools==1.21.0` triggers a playwright chromium download automatically on first install.
   - Recommendation: Run `uv add "openhands-tools==1.21.0"` and immediately check if playwright install runs. If it does and is slow, add `--no-deps` for browser-use and install only needed deps. Alternatively, accept the one-time cost.
   - Confidence: LOW — install behavior not empirically tested.

2. **qwen-35B tool-call reliability on simple tasks**
   - What we know: `native_tool_calling=False` + XML-format tool calls is the live config used by the existing OpenHands CLI setup. The model is confirmed working (agent_settings.json). The PITFALLS noted 35B XML failures as a risk.
   - What's unclear: What fraction of simple tasks (write a file, run a script) complete in ≤20 iterations vs. hitting the cap.
   - Recommendation: Start with `max_iterations=20` and a simple deterministic test goal ("Write hello.txt"). If the smoke test completes in <5 iterations, confidence is HIGH. If it consistently hits 20 iterations, reduce task scope or increase max_iterations.
   - Confidence: MEDIUM — known risk, unknown frequency for simple tasks.

3. **asyncio.to_thread thread pool exhaustion under load**
   - What we know: Python's default thread pool (via `asyncio.to_thread`) has `min(32, os.cpu_count() + 4)` threads. With a single asyncio.Queue worker that can only run one job at a time, this is not a concern — only one `to_thread` call is active at once.
   - What's unclear: Whether the single-job constraint applies correctly when the worker processes the next job before the previous thread (stuck from a timed-out run) completes.
   - Recommendation: Add a semaphore or rely on the single-job worker semantics. Since the asyncio.Queue worker picks up one job at a time (`await queue.get()`), only one `to_thread` call is active. Thread pool exhaustion is not a real risk.
   - Confidence: HIGH.

4. **openhands-sdk pydantic version conflict**
   - What we know: `openhands-sdk==1.21.0` requires `pydantic>=2.12.5`. Orchestrator venv has pydantic 2.13.4. No conflict.
   - What's unclear: Whether `litellm>=1.83.7` (required by openhands-sdk) conflicts with any existing orchestrator dep. Currently the orchestrator venv has no litellm installed.
   - Recommendation: Run `uv add "openhands-sdk==1.21.0"` and check for resolver conflicts. If litellm conflicts with langchain-openai's transitive deps, use `--override` or check if langchain-openai 1.3.2 already pins a compatible litellm version.
   - Confidence: MEDIUM — not empirically tested.

---

## Sources

### Primary (HIGH confidence — installed source + live construction tests)

- `/Users/ohama/.local/share/uv/tools/openhands/lib/python3.12/site-packages/openhands/sdk/conversation/impl/local_conversation.py` — `__init__` constructor kwargs (lines 89-112), `run()` method with max_iterations behavior (lines 745-888), `close()` cleanup
- `/Users/ohama/.local/share/uv/tools/openhands/lib/python3.12/site-packages/openhands/sdk/conversation/conversation.py` — `Conversation.__new__` factory (all kwargs), LocalWorkspace vs RemoteWorkspace dispatch
- `/Users/ohama/.local/share/uv/tools/openhands/lib/python3.12/site-packages/openhands/sdk/conversation/state.py` — `ConversationExecutionStatus` enum, `is_terminal()`, `ConversationState` fields including `max_iterations`
- `/Users/ohama/.local/share/uv/tools/openhands/lib/python3.12/site-packages/openhands/sdk/llm/llm.py` — `LLM` class fields: `model`, `api_key`, `base_url`, `native_tool_calling`, `stream`, `drop_params`, `modify_params`, `num_retries`, `timeout`, `max_output_tokens`, `max_input_tokens`; sync `litellm_completion` call (line 1171)
- `/Users/ohama/.local/share/uv/tools/openhands/lib/python3.12/site-packages/openhands/sdk/agent/base.py` — `AgentBase` fields: `llm`, `tools`, `include_default_tools`
- `/Users/ohama/.local/share/uv/tools/openhands/lib/python3.12/site-packages/openhands/sdk/conversation/fifo_lock.py` — FIFOLock is `threading.Lock` based, NOT asyncio — confirms `asyncio.to_thread` is safe
- `/Users/ohama/.local/share/uv/tools/openhands/lib/python3.12/site-packages/openhands/sdk/conversation/exceptions.py` — `ConversationRunError.__init__` and when it's raised
- `/Users/ohama/.local/share/uv/tools/openhands/lib/python3.12/site-packages/openhands/sdk/tool/builtins/finish.py` — `FinishAction.message` field, `FinishTool`, `tool_name == "finish"`
- `/Users/ohama/.local/share/uv/tools/openhands/lib/python3.12/site-packages/openhands/sdk/event/base.py` — `Event` base class, `model_dump_json()` on Pydantic model
- `/Users/ohama/.local/share/uv/tools/openhands/lib/python3.12/site-packages/openhands_tools-1.21.0.dist-info/RECORD` — confirms `openhands/tools/terminal/impl.py` is in openhands-tools (NOT openhands-sdk)
- `/Users/ohama/.local/share/uv/tools/openhands/lib/python3.12/site-packages/openhands_sdk-1.21.0.dist-info/METADATA` — dependency list: litellm>=1.83.7, pydantic>=2.12.5, etc.
- Live construction test: `LLM(model="openai/qwen-35b", ...)` + `Agent(llm=..., tools=[...])` + `Conversation(agent=..., workspace=tmpdir, ...)` — all constructed successfully against installed SDK without raising. Tested with the uv-tools Python at `/Users/ohama/.local/share/uv/tools/openhands/bin/python`.
- Live status enum test: all 8 `ConversationExecutionStatus` values confirmed with `is_terminal()` results.
- `/Users/ohama/.openhands/agent_settings.json` — live config: model `openai/qwen-35b`, native_tool_calling=false, tools=[terminal, file_editor, task_tracker, task_tool_set], base_url=`http://localhost:4000/v1`, api_key=dummy.

### Secondary (HIGH confidence — existing project code)

- `/Users/ohama/projs/LangGraph_OpenHands/orchestrator/graph/stub_graph.py` — current execute_stub, build_stub_graph(), build_test_graph() implementations
- `/Users/ohama/projs/LangGraph_OpenHands/orchestrator/worker/runner.py` — worker astream loop, `_NODE_COMPLETE_TO_STATUS`, asyncio.Queue pattern
- `/Users/ohama/projs/LangGraph_OpenHands/orchestrator/main.py` — ORCHESTRATOR_TEST_GRAPH flag, lifespan, build_test_graph usage
- `/Users/ohama/projs/LangGraph_OpenHands/pyproject.toml` — current dependencies

### Tertiary (MEDIUM confidence — not empirically tested)

- `uv add openhands-sdk==1.21.0 openhands-tools==1.21.0` install behavior in orchestrator venv — not tested; litellm version conflict risk not verified.
- qwen-35B tool-call success rate on simple tasks with max_iterations=20 — not empirically tested.
- playwright/browser-use install behavior on `uv add openhands-tools==1.21.0` — unknown.

---

## Metadata

**Confidence breakdown:**
- SDK constructor signatures (LLM, Agent, Conversation): HIGH — read from installed source, tested live
- run() behavior / max_iterations / terminal statuses: HIGH — read from local_conversation.py source code exactly
- asyncio.to_thread safety (no loop in SDK): HIGH — FIFOLock is threading.Lock; litellm_completion is sync; no asyncio in SDK internals
- EXEC-05 lazy-import pattern: HIGH — standard Python; tested that test path doesn't load openhands when execute_stub is used
- openhands-sdk vs openhands-tools package split: HIGH — verified via RECORD files
- Install into orchestrator venv: MEDIUM — not empirically tested; pydantic compat confirmed, litellm version compat not checked
- qwen-35B reliability with non-native tool calling: MEDIUM — known risk from live config; success rate on simple tasks unknown

**Research date:** 2026-06-23
**Valid until:** 2026-07-23 (openhands-sdk 1.21.0 API is stable; LLM field names unlikely to change; qwen-35B behavior is hardware-bound)
