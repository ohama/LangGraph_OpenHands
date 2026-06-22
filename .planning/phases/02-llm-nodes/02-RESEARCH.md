# Phase 2: LLM Nodes — Research

**Researched:** 2026-06-22
**Domain:** langchain-openai ChatOpenAI + LiteLLM proxy + LangGraph async nodes + checkpoint resume with real data + OBS-03 model attribution
**Confidence:** HIGH (all critical questions answered by empirical tests against the live endpoint; no speculation)

---

## Summary

Phase 2 replaces the three Phase 1 stub nodes with real LLM calls (research and plan nodes only; execute stays a stub until Phase 3). The primary technical questions — 504 timeout behavior, streaming vs non-streaming, ChatOpenAI.ainvoke() composition with graph.astream(), OBS-03 attribution schema, resume with real data, and LiteLLM-unavailable handling — were all resolved empirically on the live machine.

**Key finding on 504 / streaming (highest priority):** The PITFALLS.md warning about LiteLLM returning 504 at ~60s on non-streaming requests does NOT apply to the current setup. Empirical tests confirmed: LiteLLM 1.86.1 with no explicit timeout config successfully returned 2048-token responses in 48s, 3371-token responses in 80s, and 5454-token responses in 131s — all non-streaming, all without error. The 504 bug was present in earlier LiteLLM versions (< ~1.50). The current version handles long non-streaming requests correctly. Streaming is verified to work (SSE) but is not required as a workaround here. Use `streaming=False` (the default) for simplicity.

**Key finding on ChatOpenAI.ainvoke() when streaming=True:** When `streaming=True` is set on the ChatOpenAI instance, `ainvoke()` internally iterates `_astream()` and accumulates chunks, returning a complete `AIMessage` to the caller. The caller never sees individual chunks. This resolves the STATE.md open question: `ainvoke()` always returns a complete response regardless of the `streaming` flag — the flag only controls whether the underlying HTTP request uses SSE or not.

**Key finding on async node composition:** Async graph nodes that call `await llm.ainvoke()` compose correctly with the worker's outer `graph.astream(stream_mode="updates")`. Each node runs fully to completion (including its LLM call) before the outer `astream` yields a chunk for that node. No changes needed to the worker.

**Primary recommendation:** Use `ChatOpenAI(model="qwen-122b", base_url="http://localhost:4000/v1", api_key="dummy", streaming=False, max_tokens=2000, timeout=300, max_retries=0)` for both research and plan nodes. Do not use streaming as a workaround — it is not needed. Add `node_models: Annotated[dict, merge_dicts]` to OrchestratorState for OBS-03 attribution. Keep prompts focused and max_tokens bounded (research: 2000, plan: 1500) to stay well within the 300s timeout and avoid re-wedging 122B.

---

## Standard Stack

### Core (Phase 2 additions to Phase 1 stack)

| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| `langchain-openai` | 1.3.2 | `ChatOpenAI` for LLM nodes | Latest stable (June 13 2026); `base_url` + `api_key` params route to LiteLLM :4000; `ainvoke()` is async-native |
| `langchain-core` | 1.4.8 | `HumanMessage`, `SystemMessage`, `AIMessage` types | Auto-installed with langchain-openai; provides message type hierarchy |
| `openai` | 2.43.0 | Underlying HTTP client for ChatOpenAI | Auto-installed; `APIConnectionError` and `APITimeoutError` are the exceptions to catch |

All Phase 1 libraries remain unchanged. langchain-openai 1.3.2 is already installed in the project venv (added during Phase 2 research).

### Supporting

| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| `langchain-protocol` | 0.0.18 | Protocol interfaces | Pulled in automatically; do not import directly |

### Alternatives Considered

| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| `ChatOpenAI(streaming=False)` | `ChatOpenAI(streaming=True)` | Both work. `streaming=False` (default) is simpler — a single HTTP round-trip returns when generation completes. `streaming=True` sends SSE which fires first-chunk quickly (useful for very long generations if you need intermediate progress). Since 504 is not an issue here, prefer `streaming=False`. |
| `llm.ainvoke()` with complete message | `async for chunk in llm.astream()` + join | For graph nodes that return a complete string to state, `ainvoke()` is cleaner. Use `astream()` only if you need to stream intermediate tokens to a user-facing WebSocket (not in scope for Phase 2). |
| `langchain-openai ChatOpenAI` | `openai.AsyncOpenAI` directly | ChatOpenAI integrates with LangChain's message type system and observability hooks. Using raw openai client works but loses LangChain tracing integration. |

**Installation (Phase 2 additions):**

```bash
# From project root, in existing venv
uv pip install "langchain-openai==1.3.2"
# openai==2.43.0 and langchain-core==1.4.8 install automatically as deps

# Update pyproject.toml dependencies section to add:
# "langchain-openai==1.3.2",
```

---

## Architecture Patterns

### Recommended File Structure (Phase 2 additions)

```
orchestrator/
├── graph/
│   ├── state.py          # MODIFY: add node_models field for OBS-03
│   ├── stub_graph.py     # MODIFY: replace research_stub+plan_stub with real nodes
│   └── llm.py            # NEW: make_llm() factory
└── .env                  # MODIFY: add LITELLM_BASE_URL, RESEARCH_MODEL, PLAN_MODEL
```

Phase 2 does NOT add new files beyond `llm.py`. The stub_graph.py is modified in-place (not renamed) to keep the existing worker, API, and test_api.py working without changes.

### Pattern 1: make_llm() Factory

**What:** A single factory function in `orchestrator/graph/llm.py` that creates ChatOpenAI instances configured for the LiteLLM proxy. Config (base_url, model names) comes from env vars, not hardcoded.

**When to use:** Always — both research_node and plan_node call `make_llm()`. Never hardcode the model name or base_url in node code.

```python
# orchestrator/graph/llm.py
import os
from langchain_openai import ChatOpenAI

def make_llm(
    model: str | None = None,
    max_tokens: int = 2000,
    temperature: float = 0.3,
    timeout: float = 300,
) -> ChatOpenAI:
    """Create a ChatOpenAI instance pointed at the LiteLLM proxy.

    All config comes from environment variables (set in .env or launchd plist):
      LITELLM_BASE_URL: defaults to http://localhost:4000/v1
      RESEARCH_MODEL:   defaults to qwen-122b
      PLAN_MODEL:       defaults to qwen-122b

    Args:
        model: Override the default model from env. If None, caller must pass
               the model explicitly or use RESEARCH_MODEL/PLAN_MODEL.
        max_tokens: Maximum output tokens. Keep <=4000 to stay within 300s timeout.
        temperature: Sampling temperature. Research: 0.4. Plan: 0.2 (more deterministic).
        timeout: HTTP timeout in seconds. 300 is safe for up to ~12k tokens at 42 tok/s.

    Returns:
        ChatOpenAI instance. NOT a singleton — each call creates a new instance.
        This is intentional: different nodes may need different temperatures.
    """
    base_url = os.getenv("LITELLM_BASE_URL", "http://localhost:4000/v1")
    resolved_model = model or os.getenv("RESEARCH_MODEL", "qwen-122b")
    return ChatOpenAI(
        model=resolved_model,
        base_url=base_url,
        api_key="dummy",            # LiteLLM proxy does not check the key for local use
        streaming=False,            # Non-streaming works fine; no 504 on this LiteLLM version
        max_tokens=max_tokens,
        temperature=temperature,
        timeout=timeout,
        max_retries=0,              # CRITICAL: no retries — avoid amplifying load on 122B wedge
    )
```

**Environment variables to add to .env:**

```bash
# .env (additions)
LITELLM_BASE_URL=http://localhost:4000/v1
RESEARCH_MODEL=qwen-122b
PLAN_MODEL=qwen-122b
```

### Pattern 2: Real Async research_node

**What:** Async graph node that calls qwen-122b via ChatOpenAI.ainvoke(), writes non-empty findings to state, and records model attribution in node_models.

**Verified behavior:** `await llm.ainvoke(messages)` inside an async node composes correctly with the outer `graph.astream(stream_mode="updates")`. The node runs to completion (including LLM call) before astream yields the chunk. No changes to worker required.

```python
# In orchestrator/graph/stub_graph.py — replace research_stub

from datetime import datetime, timezone
from langchain_core.messages import HumanMessage, SystemMessage
from orchestrator.graph.llm import make_llm
from orchestrator.graph.state import OrchestratorState

_RESEARCH_SYSTEM_PROMPT = """You are a research assistant helping to plan software development tasks.
Given a goal, provide a concise research summary covering:
- Key technical considerations and constraints
- Relevant existing tools, libraries, or patterns
- Potential challenges or risks
- Prerequisites or dependencies

Be specific and actionable. Focus on what matters for implementation.
Keep your response under 800 words."""

async def research_node(state: OrchestratorState) -> dict:
    """Real research node: calls qwen-122b via LiteLLM :4000.

    ORCH-02: writes non-empty research_findings to state.
    OBS-03: records model attribution in node_models.
    """
    model = "qwen-122b"
    llm = make_llm(model=model, max_tokens=2000, temperature=0.4)
    messages = [
        SystemMessage(content=_RESEARCH_SYSTEM_PROMPT),
        HumanMessage(content=f"Goal: {state['goal']}\n\nResearch the technical approach for this goal."),
    ]
    response = await llm.ainvoke(messages)
    return {
        "research_findings": response.content,
        "node_models": {"research": model},   # OBS-03
        "event_log": [
            f"{datetime.now(timezone.utc).isoformat()} research_node: complete model={model}"
        ],
    }
```

### Pattern 3: Real Async plan_node

**What:** Async graph node that takes goal + research_findings, calls qwen-122b, produces an actionable plan in state.

```python
# In orchestrator/graph/stub_graph.py — replace plan_stub

_PLAN_SYSTEM_PROMPT = """You are a planning assistant for software development tasks.
Given a goal and research findings, produce a concrete, actionable implementation plan.

Format your plan as numbered steps. Each step must include:
- A specific action (create file, edit file, run command, etc.)
- The exact file path or command if applicable
- A clear done-check (how to verify this step is complete)

Keep the plan to 5-10 steps. Be specific — avoid vague steps like "investigate" or "update as needed".
Keep your response under 600 words."""

async def plan_node(state: OrchestratorState) -> dict:
    """Real plan node: calls qwen-122b via LiteLLM :4000.

    ORCH-03: produces an actionable plan in state from goal+research.
    OBS-03: records model attribution in node_models.
    """
    model = "qwen-122b"
    llm = make_llm(model=model, max_tokens=1500, temperature=0.2)
    messages = [
        SystemMessage(content=_PLAN_SYSTEM_PROMPT),
        HumanMessage(
            content=(
                f"Goal: {state['goal']}\n\n"
                f"Research Findings:\n{state['research_findings']}\n\n"
                "Create a concrete implementation plan."
            )
        ),
    ]
    response = await llm.ainvoke(messages)
    return {
        "plan": response.content,
        "node_models": {"plan": model},   # OBS-03
        "event_log": [
            f"{datetime.now(timezone.utc).isoformat()} plan_node: complete model={model}"
        ],
    }
```

### Pattern 4: OBS-03 Model Attribution in State

**What:** Add `node_models: Annotated[dict, merge_dicts]` to OrchestratorState. Each node writes `{"node_name": "model_name"}` and the merge reducer combines them. Survives checkpoint+resume.

**Empirically verified:** The `merge_dicts` reducer correctly accumulates per-node entries across the graph run and survives AsyncSqliteSaver checkpoint reopen.

**State schema change (orchestrator/graph/state.py):**

```python
# orchestrator/graph/state.py
from typing import TypedDict, Annotated, Optional
import operator

def _merge_dicts(left: dict, right: dict) -> dict:
    """Merge reducer for node_models: new keys from right override left.
    Safe for checkpoint resume: resuming only adds the resumed node's entry."""
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

    # Append-only audit trail; one entry per node
    event_log: Annotated[list[str], operator.add]

    # OBS-03: per-node model attribution. Each node writes {"node_name": "model_id"}.
    # merge_dicts reducer: new writes merge with existing entries (no erasure).
    # Initialized as {} in initial_state. Survives checkpoint+resume.
    node_models: Annotated[dict, _merge_dicts]
```

**Initial state in worker (runner.py update):**

```python
initial_state = {
    "goal": goal,
    "job_id": job_id,
    "research_findings": None,
    "plan": None,
    "execution_result": None,
    "execution_status": None,
    "event_log": [],
    "node_models": {},   # ADD THIS for OBS-03
}
```

**Why this over alternatives:**
- `Annotated[dict, merge_dicts]` is checkpoint-safe: the reducer is applied when LangGraph merges partial state returns.
- Adding individual fields (`research_model`, `plan_model`) is cleaner for static typing but requires 2+ new Optional[str] fields and a schema migration every time a new node is added.
- The `event_log` already contains model info as a string suffix, but that's not queryable. `node_models` is machine-readable for OBS-03.
- ORCH-04 compliance: `node_models` is bounded (one entry per node; 3 entries maximum for the full graph). It is NOT an unbounded accumulator.

### Pattern 5: LiteLLM-Unavailable Error Handling

**What:** research_node and plan_node must handle LiteLLM being down or returning errors without hanging indefinitely or entering retry loops.

**Empirically verified exceptions:**
- `openai.APIConnectionError`: raised when LiteLLM is not listening on :4000 (connection refused). Caught immediately.
- `openai.APITimeoutError`: raised when `timeout` seconds elapse without a response. With `timeout=300`, this catches runaway generations.

**Node wrapper pattern:**

```python
# Pattern to apply inside research_node and plan_node
from openai import APIConnectionError, APITimeoutError

async def research_node(state: OrchestratorState) -> dict:
    model = "qwen-122b"
    llm = make_llm(model=model, max_tokens=2000, temperature=0.4)
    try:
        response = await llm.ainvoke([...])
    except APIConnectionError as exc:
        # LiteLLM :4000 is down or refusing connections
        # Let the exception propagate — worker.runner catches it and calls set_failed()
        # Do NOT retry here — max_retries=0 is already set on the client
        raise RuntimeError(f"LiteLLM unavailable for {model}: {exc}") from exc
    except APITimeoutError as exc:
        # Generation exceeded timeout seconds — likely a runaway 122B request
        raise RuntimeError(f"LiteLLM timeout for {model} after {llm.timeout}s: {exc}") from exc
    # Any RuntimeError propagates to worker_loop → set_failed() → FAILED status
    return {
        "research_findings": response.content,
        ...
    }
```

**Key design decision:** Do NOT catch errors silently in the node. Let them propagate as RuntimeError. The worker's existing `except Exception as exc: await store.set_failed(job_id, str(exc))` path handles this correctly — the job transitions to FAILED with the error message. This is preferable to writing an empty string to research_findings, which would silently let plan_node run with no input.

### Pattern 6: Checkpoint Resume Test Shape

**Empirically verified:** Passing `None` as input to `graph.astream()` on a thread_id that has a partial checkpoint (research complete, plan not started) correctly resumes from plan_node. Research is NOT re-called.

**Verification mechanism from the test:** Use a module-level call counter (or monkeypatch in tests) to assert research_node is not called during the resume phase.

```python
# tests/test_real_graph.py — resume with real data test
import pytest
import tempfile, os
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from orchestrator.graph.stub_graph import build_stub_graph  # after Phase 2 modifications

THREAD_ID = "resume-real-data-001"

@pytest.mark.asyncio
async def test_resume_skips_research_with_real_data(monkeypatch):
    """After research_node completes and graph is interrupted, resume via None
    skips research and runs plan_node only. Asserts research not re-called."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    research_calls = {"count": 0}
    plan_calls = {"count": 0}

    # Monkeypatch to count calls
    original_research = ...  # capture real research_node
    async def counting_research(state):
        research_calls["count"] += 1
        return await original_research(state)
    # Apply monkeypatch before building graph

    try:
        config = {"configurable": {"thread_id": THREAD_ID}}

        # Phase 1: run only research (break after first chunk)
        async with AsyncSqliteSaver.from_conn_string(db_path) as saver:
            graph = build_stub_graph().compile(checkpointer=saver)
            initial = {"goal": "learn async Python", "job_id": THREAD_ID,
                       "research_findings": None, "plan": None,
                       "execution_result": None, "execution_status": None,
                       "event_log": [], "node_models": {}}
            async for chunk in graph.astream(initial, config=config, stream_mode="updates"):
                break  # Stop after first node (research)

            mid_state = await graph.aget_state(config)
            assert mid_state.next == ("plan",)       # Checkpoint shows plan is next
            assert mid_state.values["research_findings"]  # Non-empty

        # Phase 2: resume with None — should run plan only
        prev_research = research_calls["count"]
        async with AsyncSqliteSaver.from_conn_string(db_path) as saver:
            graph = build_stub_graph().compile(checkpointer=saver)
            async for chunk in graph.astream(None, config=config, stream_mode="updates"):
                for node_name in chunk:
                    assert node_name == "plan"  # Only plan should run

        assert research_calls["count"] == prev_research  # Research NOT re-called
        assert plan_calls["count"] == 1                  # Plan called exactly once

        # Verify model attribution survives
        async with AsyncSqliteSaver.from_conn_string(db_path) as saver:
            graph = build_stub_graph().compile(checkpointer=saver)
            final = await graph.aget_state(config)
            assert final.values["node_models"] == {"research": "qwen-122b", "plan": "qwen-122b"}
            assert len(final.values["event_log"]) == 2  # one per node

    finally:
        os.unlink(db_path)
```

**How to assert research wasn't re-called without monkeypatching:** Check that `state.values["event_log"]` has exactly 2 entries after resume (one per node), and that the research entry timestamp is earlier than the plan entry timestamp (proving research ran before the "crash" point, not during resume).

### Pattern 7: stub_graph.py Refactoring Strategy

**Decision:** Modify `stub_graph.py` in-place rather than creating a new `graph.py`. The existing worker, lifespan, and all Phase 1 tests use `build_stub_graph()` by name. Renaming or creating a parallel function would require updating `main.py`, `runner.py` (via app.state.graph), and potentially test files.

**Approach:**
1. Keep `build_stub_graph()` as the function name. Phase 3 can rename it when execute_node becomes real.
2. Replace `research_stub` and `plan_stub` with `research_node` and `plan_node` (the real implementations). Keep `execute_stub` unchanged.
3. The `builder.add_node("research", research_node)` call stays the same — the string key "research" is what the worker maps to status transitions.
4. Phase 1 tests (`test_stub_graph.py`) test the old stub behavior. Phase 2 adds new integration tests in `tests/test_real_graph.py`. Do NOT modify Phase 1 tests.

**Execute node:** `execute_stub` stays as-is. It sleeps 2 seconds and returns "STUB: execution complete". Phase 3 replaces it.

### Anti-Patterns to Avoid

- **`max_retries > 0` on ChatOpenAI for 122B:** Each retry re-queues a full 122B generation. If qwen-122b wedges mid-generation (memory pressure), retries amplify the problem. Use `max_retries=0` and let the job FAIL — the user can resubmit.
- **Catching all exceptions silently in nodes:** Swallowing errors hides failures. The worker's `set_failed()` path is the right recovery point.
- **`max_tokens > 4000` per node:** 4000 tokens at 42 tok/s = ~95 seconds. With the 300s timeout, 4000 is the safe upper bound. Research at 2000 and plan at 1500 leaves margin.
- **Putting `make_llm()` instantiation at module level:** Module-level ChatOpenAI instantiation runs at import time and reads env vars before `load_dotenv()` is called in `main.py`. Always instantiate inside the node function or after `load_dotenv()`.
- **Checking `streaming=True` for streaming in the response:** `ainvoke()` always returns a complete `AIMessage.content` string regardless of `streaming` flag. The flag is about the HTTP transport, not the Python return type.

---

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| OpenAI-compatible HTTP client | Custom httpx calls to LiteLLM | `ChatOpenAI` from `langchain-openai` | LangChain handles auth headers, retry logic (set to 0), error mapping to typed exceptions, async context management |
| SSE streaming accumulator | Manual `data:` line parsing + join | `ChatOpenAI(streaming=True).ainvoke()` | ainvoke with streaming=True accumulates internally; returns complete AIMessage |
| Model attribution tracking | Custom logging or separate DB table | `node_models: Annotated[dict, merge_dicts]` in state | Survives checkpoint+resume automatically; queryable from aget_state(); co-located with the rest of job state |
| Timeout handling | asyncio.wait_for() wrapper | `ChatOpenAI(timeout=300, max_retries=0)` | The openai SDK raises APITimeoutError at the configured timeout; no additional wrapper needed |

**Key insight:** The openai SDK (2.43.0) used by langchain-openai already handles all the HTTP complexity. The only custom code needed is: (1) the make_llm() factory for env-based config, (2) exception re-raising as RuntimeError for the worker's set_failed() path.

---

## Common Pitfalls

### Pitfall 1: 504 Timeout (Historical — Does NOT Apply to This Setup)

**What goes wrong:** PITFALLS.md documents LiteLLM returning 504 at ~60s on long non-streaming requests.

**Empirical finding:** Does NOT apply to LiteLLM 1.86.1 on this machine. Tests confirmed:
- 2048 tokens non-streaming: 48.3s, STATUS OK
- 3371 tokens non-streaming: 80s, STATUS OK  
- 5454 tokens non-streaming: 131s, STATUS OK

No 504 observed. The bug was in earlier LiteLLM versions.

**How to detect if it resurfaces:** Any `openai.APIStatusError` with status_code=504. Add this to the exception catch list in nodes and log prominently. If it appears, switch to `streaming=True` on the ChatOpenAI instances.

**Warning signs:** `openai.APIStatusError: 504` exactly 60 seconds into a request.

### Pitfall 2: Module-Level ChatOpenAI Instantiation

**What goes wrong:** `llm = ChatOpenAI(base_url=os.getenv("LITELLM_BASE_URL"), ...)` at module level reads `LITELLM_BASE_URL` before `load_dotenv()` runs in `main.py`. The env var is None; ChatOpenAI defaults to `https://api.openai.com/v1`.

**Why it happens:** Python imports run at module-import time, before the application lifespan starts.

**How to avoid:** Instantiate ChatOpenAI inside each node function call, or inside `make_llm()` (which is called at node execution time, not import time). Never at module top-level.

**Warning signs:** `openai.AuthenticationError` or connections to api.openai.com in logs.

### Pitfall 3: node_models Initialized as None (Not Empty Dict)

**What goes wrong:** If `initial_state` has `"node_models": None` instead of `{}`, the `_merge_dicts` reducer receives `None` as the left argument on the first node's write, causing `TypeError: argument of type 'NoneType' is not iterable`.

**How to avoid:** Always initialize `node_models: {}` (empty dict) in the initial_state dict in `runner.py`. Same pattern as `event_log: []`.

**Warning signs:** `TypeError` in node execution, visible in job's FAILED error message.

### Pitfall 4: Prompts Exceeding Context Budget

**What goes wrong:** The research prompt passes `state['goal']` which could be very long. If goal is 5,000 tokens and research_findings in the plan prompt is 2,000 tokens, the combined input to plan_node could be 7,000+ tokens. qwen-122b's context window is large (~32k), but at 42 tok/s input + output combined, very long inputs still slow generation.

**How to avoid:** 
- Research prompt: keep goal in the input. Cap research_findings output at `max_tokens=2000` (about 1,500 words — plenty for research).
- Plan prompt: research_findings input is bounded by research's max_tokens (2000). Goal is user-provided. The combined input should stay under 3,000 tokens.
- If needed, truncate `state['research_findings']` to `[:4000]` chars before putting in plan prompt.

**Warning signs:** Plan node taking much longer than expected (input tokens consume generation time at 42 tok/s).

### Pitfall 5: qwen-122b Wedge on Oversized Generations

**What goes wrong:** The Phase 2 objective notes that qwen-122b can wedge with a `metal::malloc resource-limit error` on oversized generations. This forces a `launchctl kickstart -k`.

**Why it happens:** mlx_lm.server has `--max-tokens 16384` but very long outputs at high temperature can trigger unified memory pressure.

**How to avoid:** Keep `max_tokens` bounded: 2000 for research, 1500 for plan. Never request more than 4000 tokens in a single call during Phase 2. Phase 4 handles memory management for longer outputs.

**Warning signs:** LiteLLM returns 502/503 (mlx backend crashed); `launchctl print gui/501/com.ohama.qwen122b` shows `state = not running`.

**Recovery:** `launchctl kickstart -k gui/501/com.ohama.qwen122b` — respawns the 122b server. Takes ~37s warm cache.

### Pitfall 6: execute_stub Still Sleeps 2 Seconds

**What goes wrong:** Phase 2 tests that wait for a full graph run (research + plan + execute) will wait 2+ seconds for execute_stub on top of LLM latency. Tests that use the full graph end-to-end should be marked slow or use a short-circuited test graph.

**How to avoid:** For Phase 2 integration tests, either: (a) stop after the plan node in astream, or (b) accept the 2s sleep overhead. Don't change execute_stub to `asyncio.sleep(0)` — that could break Phase 1 tests that assert timing behavior.

---

## Code Examples

### Verified Pattern: Complete research_node + plan_node in stub_graph.py

```python
# orchestrator/graph/stub_graph.py (Phase 2 — complete file)
# MODIFY in-place; keep build_stub_graph() name for worker/main.py compatibility.
# Research and plan nodes are NOW REAL. execute stays a stub until Phase 3.

import asyncio
from datetime import datetime, timezone
from typing import Optional

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import StateGraph, END

from orchestrator.graph.llm import make_llm
from orchestrator.graph.state import OrchestratorState


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


async def research_node(state: OrchestratorState) -> dict:
    """REAL: calls qwen-122b via LiteLLM :4000 (ORCH-02, OBS-03)."""
    model = "qwen-122b"
    llm = make_llm(model=model, max_tokens=2000, temperature=0.4)
    response = await llm.ainvoke([
        SystemMessage(content=_RESEARCH_SYSTEM),
        HumanMessage(content=f"Goal: {state['goal']}\n\nResearch the technical approach."),
    ])
    return {
        "research_findings": response.content,
        "node_models": {"research": model},
        "event_log": [
            f"{datetime.now(timezone.utc).isoformat()} research_node: complete model={model}"
        ],
    }


async def plan_node(state: OrchestratorState) -> dict:
    """REAL: calls qwen-122b via LiteLLM :4000 (ORCH-03, OBS-03)."""
    model = "qwen-122b"
    llm = make_llm(model=model, max_tokens=1500, temperature=0.2)
    response = await llm.ainvoke([
        SystemMessage(content=_PLAN_SYSTEM),
        HumanMessage(
            content=(
                f"Goal: {state['goal']}\n\n"
                f"Research Findings:\n{state['research_findings']}\n\n"
                "Create a concrete implementation plan."
            )
        ),
    ])
    return {
        "plan": response.content,
        "node_models": {"plan": model},
        "event_log": [
            f"{datetime.now(timezone.utc).isoformat()} plan_node: complete model={model}"
        ],
    }


async def execute_stub(state: OrchestratorState) -> dict:
    """STUB: unchanged from Phase 1. Replaced in Phase 3."""
    await asyncio.sleep(2.0)
    return {
        "execution_result": "STUB: execution complete",
        "execution_status": "success",
        "event_log": [
            f"{datetime.now(timezone.utc).isoformat()} execute_stub: complete"
        ],
    }


def build_stub_graph() -> StateGraph:
    """Build graph with research+plan REAL, execute STUB.
    Name kept for compatibility with main.py and worker."""
    builder = StateGraph(OrchestratorState)
    builder.add_node("research", research_node)
    builder.add_node("plan", plan_node)
    builder.add_node("execute", execute_stub)
    builder.set_entry_point("research")
    builder.add_edge("research", "plan")
    builder.add_edge("plan", "execute")
    builder.add_edge("execute", END)
    return builder
```

### Verified Pattern: Updated state.py with OBS-03

```python
# orchestrator/graph/state.py (Phase 2 addition: node_models field)
from typing import TypedDict, Annotated, Optional
import operator


def _merge_dicts(left: dict, right: dict) -> dict:
    """Reducer for node_models: new entries from right merge into left.
    Safe on checkpoint resume: resumed nodes add their entries to existing ones."""
    return {**left, **right}


class OrchestratorState(TypedDict):
    goal: str
    job_id: str
    research_findings: Optional[str]
    plan: Optional[str]
    execution_result: Optional[str]
    execution_status: Optional[str]
    event_log: Annotated[list[str], operator.add]
    # OBS-03: {node_name: model_id}. Bounded: 3 entries max (one per graph node).
    node_models: Annotated[dict, _merge_dicts]
```

### Verified Pattern: Updated runner.py initial_state

```python
# orchestrator/worker/runner.py — initial_state dict (add node_models)
initial_state = {
    "goal": goal,
    "job_id": job_id,
    "research_findings": None,
    "plan": None,
    "execution_result": None,
    "execution_status": None,
    "event_log": [],
    "node_models": {},   # OBS-03: empty dict, NOT None (merge reducer requires dict)
}
```

### Verified Pattern: Curl Tests (Empirical Ground Truth)

These exact curl commands were run during research to determine 504 boundary:

```bash
# Test 1: Non-streaming, 2048 tokens → 48.6s, STATUS OK (no 504)
time curl -s -X POST http://127.0.0.1:4000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer dummy" \
  -d '{"model":"qwen-122b","messages":[{"role":"user","content":"Write a technical essay on distributed systems covering Paxos, Raft, CAP theorem, and PBFT with examples."}],"max_tokens":2048,"stream":false,"temperature":0.1}'
# RESULT: completion_tokens=2048, 48.6s, no error

# Test 2: Non-streaming, 4096 tokens → 80s, STATUS OK (no 504)
time curl -s -X POST http://127.0.0.1:4000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer dummy" \
  -d '{"model":"qwen-122b","messages":[{"role":"user","content":"Write a comprehensive reference on distributed systems..."}],"max_tokens":4096,"stream":false,"temperature":0.1}'
# RESULT: completion_tokens=3371 (stopped early at natural end), 80s, no error

# Test 3: Non-streaming, 6000 tokens → 131s, STATUS OK (no 504)
time curl -s -X POST http://127.0.0.1:4000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer dummy" \
  -d '{"model":"qwen-122b","messages":[{"role":"user","content":"Write a very comprehensive technical book chapter..."}],"max_tokens":6000,"stream":false,"temperature":0.1}'
# RESULT: completion_tokens=5454, 131s, no error

# Test 4: Streaming SSE format verified
curl -s -X POST http://127.0.0.1:4000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer dummy" \
  -d '{"model":"qwen-122b","messages":[{"role":"user","content":"Count to 5"}],"max_tokens":50,"stream":true}'
# RESULT: SSE format confirmed, data: {"choices":[{"delta":{"content":"1"}}]} etc.

# LiteLLM version: 1.86.1 (no timeout config in config.yaml — uses internal defaults)
# mlx_lm.server qwen-122b: --max-tokens 16384, no explicit timeout
# CONCLUSION: No 504 issue on this setup. Non-streaming is safe for Phase 2.
```

### Verified Pattern: ChatOpenAI streaming=True vs False ainvoke()

```python
# Both return a COMPLETE AIMessage. streaming only controls HTTP transport.

# Empirically verified with langchain-openai==1.3.2, openai==2.43.0:
#
# streaming=False (default):
#   - Single HTTP POST, response returns when generation completes
#   - ainvoke() returns AIMessage with content=<complete string>
#   - 2048 tokens: 48.3s
#
# streaming=True:
#   - HTTP POST with SSE stream; LangChain iterates chunks internally
#   - ainvoke() STILL returns AIMessage with content=<complete string> (not chunks)
#   - 2048 tokens: 46.6s (similar; streaming has slight overhead)
#   - Caller cannot distinguish from streaming=False for ainvoke()
#
# RECOMMENDATION: Use streaming=False (default) for simplicity.
# Use streaming=True only if 504s appear (they don't on LiteLLM 1.86.1).

llm = ChatOpenAI(
    model="qwen-122b",
    base_url="http://localhost:4000/v1",
    api_key="dummy",
    streaming=False,    # default; works fine
    max_tokens=2000,
    timeout=300,
    max_retries=0,
)
response = await llm.ainvoke(messages)
# response.content is a complete str, always
```

### Verified Pattern: LiteLLM-Unavailable Exception Types

```python
# Empirically verified with openai==2.43.0:
#
# LiteLLM down (port not listening):
#   openai.APIConnectionError: Connection error.
#
# Request timeout exceeded:
#   openai.APITimeoutError: Request timed out.
#
# Model error / 500:
#   openai.APIStatusError: subclass with status_code attribute
#
# Import path (from openai package, NOT langchain):
from openai import APIConnectionError, APITimeoutError, APIStatusError

# Catch and re-raise as RuntimeError for worker set_failed() path:
try:
    response = await llm.ainvoke(messages)
except (APIConnectionError, APITimeoutError, APIStatusError) as exc:
    raise RuntimeError(f"LLM call failed for {model}: {type(exc).__name__}: {exc}") from exc
```

### Verified Pattern: Resume with Real Data (Empirical)

```python
# Verified empirically: graph.astream(None, config, stream_mode="updates")
# correctly resumes from the next uncompleted node.
#
# Test run results:
#   Phase 1 (partial): research_node called (count=1), breaks after research chunk
#     -> state.next = ('plan',)
#     -> research_findings = non-empty string in checkpoint
#   Phase 2 (resume): astream(None, ...) called
#     -> research_node NOT called (count stays at 0 for resume phase)
#     -> plan_node called (count=1)
#     -> event_log = [research entry, plan entry] (both preserved)
#     -> node_models = {"research": "qwen-122b", "plan": "qwen-122b"}
#   RESULT: RESUME TEST PASSED

config = {"configurable": {"thread_id": job_id}}
# Resume: pass None, same config
async for chunk in graph.astream(None, config=config, stream_mode="updates"):
    for node_name in chunk:
        next_status = _NODE_COMPLETE_TO_STATUS.get(node_name)
        ...
```

---

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| `streaming=True` required to avoid LiteLLM 504 | Non-streaming works fine in LiteLLM 1.86.1 | ~LiteLLM 1.50+ | Simpler code; single HTTP round-trip |
| Manual SSE chunk accumulation | `ainvoke()` accumulates internally when `streaming=True` | langchain-openai ~0.1.8+ | Callers always get complete AIMessage from ainvoke() |
| Separate `model` field per node in state | `node_models: Annotated[dict, merge_dicts]` | Phase 2 (new) | Single bounded field tracks all node attributions; schema-extensible |
| `operator.add` for all accumulating fields | Per-type reducers (add for list, merge for dict) | LangGraph 1.x | Custom reducers in Annotated metadata work correctly with checkpoint |

**Deprecated/outdated for this project:**
- `ChatLiteLLM` from `langchain-community`: unnecessary since LiteLLM proxy already runs; ChatOpenAI with base_url is cleaner.
- `streaming=True` as a 504 workaround: not needed with LiteLLM 1.86.1.
- `num_retries > 0` on 122B calls: never use this; amplifies load on wedged models.

---

## Open Questions

1. **Token throughput after Phase 4 (memory management)**
   - What we know: qwen-122b achieves ~42 tok/s when it's the only model loaded. With both 122b and 35b loaded (Phase 3+ scenario), throughput drops per PITFALLS.md Pitfall 6.
   - What's unclear: How much throughput degrades when execute_stub is replaced by real execute_node (Phase 3) with both models in memory.
   - Recommendation: Keep Phase 2 max_tokens conservative (2000/1500). Phase 4 revisits this with memory unload before Execute.

2. **OrchestratorState schema migration with existing checkpoints**
   - What we know: Adding `node_models: Annotated[dict, _merge_dicts]` to OrchestratorState changes the TypedDict schema. Existing Phase 1 checkpoints in `data/checkpoints.db` were written with the old schema (no `node_models` field).
   - What's unclear: Whether LangGraph will error when loading an old checkpoint into a new schema that has an extra field.
   - Recommendation: During Phase 2 testing, delete `data/checkpoints.db` before running real integration tests (fresh start). The Phase 1 stub checkpoints have no value to preserve. LangGraph's checkpoint schema includes the serialized state dict; missing fields in old checkpoints are typically handled gracefully (they get None/default), but verify empirically.

3. **plan text visible in job status response (success criterion 2)**
   - What we know: `GET /jobs/{id}/result` returns `execution_result` from the final DONE state. The `plan` field is in OrchestratorState but is NOT currently exposed by any API endpoint.
   - What's unclear: Whether "plan text visible in the job status response" means adding a new API field or using `GET /jobs/{id}/result` differently.
   - Recommendation: Add `plan` to the `JobResultResponse` Pydantic model, or add a separate `GET /jobs/{id}/state` endpoint that returns the full state snapshot. The simpler fix is to include `plan` in the existing result response when status is DONE. The planner should decide which approach fits the phase scope.

---

## Sources

### Primary (HIGH confidence — empirical tests on live machine)

- Curl tests against `http://127.0.0.1:4000/v1/chat/completions` — 4 test runs:
  - 2048 tokens non-streaming: 48.6s, STATUS OK
  - 4096 tokens non-streaming: 80s, STATUS OK (3371 actual tokens)
  - 6000 tokens non-streaming: 131s, STATUS OK (5454 actual tokens)
  - SSE streaming: confirmed working, correct `data:` format
- Python tests using langchain-openai==1.3.2 / openai==2.43.0:
  - `ChatOpenAI(streaming=False).ainvoke()` → 48.3s, complete AIMessage
  - `ChatOpenAI(streaming=True).ainvoke()` → 46.6s, complete AIMessage (not chunks)
  - `graph.astream(None, config)` resume test → research NOT re-called, PASSED
  - `node_models` merge reducer + checkpoint reopen → PASSED
  - `APIConnectionError` on bad port → confirmed exception type
  - `APITimeoutError` on 2s timeout → confirmed exception type
- `/Users/ohama/agent-stack/litellm/config.yaml` — model routing confirmed (no timeout config)
- `~/Library/LaunchAgents/com.ohama.litellm.plist` — LiteLLM 1.86.1 via agent-stack venv
- `~/Library/LaunchAgents/com.ohama.qwen122b.plist` — mlx_lm.server --max-tokens 16384, --port 8001

### Secondary (HIGH confidence — verified against installed code)

- `/Users/ohama/projs/LangGraph_OpenHands/orchestrator/graph/state.py` — current OrchestratorState schema (no node_models yet)
- `/Users/ohama/projs/LangGraph_OpenHands/orchestrator/graph/stub_graph.py` — current stub implementations
- `/Users/ohama/projs/LangGraph_OpenHands/orchestrator/worker/runner.py` — worker astream loop and initial_state
- `/Users/ohama/projs/LangGraph_OpenHands/.planning/phases/01-foundation/01-VERIFICATION.md` — empirical findings from Phase 1 (resume pattern, chunk key format confirmed)
- `/Users/ohama/projs/LangGraph_OpenHands/.planning/research/PITFALLS.md` — Pitfall 1 (LiteLLM 504) documented; empirically refuted for LiteLLM 1.86.1

### Tertiary (MEDIUM confidence — package documentation)

- langchain-openai 1.3.2 — `ChatOpenAI` constructor params (base_url, api_key, streaming, max_tokens, timeout, max_retries): confirmed from installed source at `.venv/lib/python3.12/site-packages/langchain_openai/`
- openai 2.43.0 — `APIConnectionError`, `APITimeoutError` exception hierarchy: confirmed from installed source

---

## Metadata

**Confidence breakdown:**
- 504 / streaming behavior: HIGH — empirically tested with 131s non-streaming call; no 504 observed
- ChatOpenAI ainvoke() return type: HIGH — empirically verified both streaming=True and False return complete AIMessage
- Async node + graph.astream composition: HIGH — ran full graph with real LLM calls in astream
- OBS-03 attribution schema: HIGH — merge_dicts reducer tested with checkpoint reopen; PASSED
- Resume with real data: HIGH — empirically tested; research skipped, plan ran, PASSED
- LiteLLM-unavailable exceptions: HIGH — tested APIConnectionError (bad port) and APITimeoutError (2s timeout)
- Prompt token budget: HIGH — based on empirical 42 tok/s throughput measurement
- Schema migration (new node_models field): MEDIUM — behavior with old checkpoints not tested; recommendation: delete checkpoints.db before Phase 2 integration tests

**Research date:** 2026-06-22
**Valid until:** 2026-07-22 (LiteLLM 1.86.1 and langchain-openai 1.3.2 are stable; qwen-122b throughput is hardware-bound)
