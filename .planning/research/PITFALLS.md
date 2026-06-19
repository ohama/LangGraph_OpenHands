# Pitfalls Research

**Domain:** Local LangGraph + OpenHands orchestrator (Research → Plan → Execute) on macOS with local Qwen models via LiteLLM
**Researched:** 2026-06-19
**Confidence:** HIGH (most pitfalls are directly observable from the existing blueCode project history and verified sources; a few OpenHands SDK internals are MEDIUM)

---

## Critical Pitfalls

### Pitfall 1: LiteLLM proxy returns 504 on long non-streaming completions

**What goes wrong:**
With `stream: false` in `agent_settings.json`, requests to LiteLLM that take more than ~60s return `504 Gateway Timeout` even when a longer `timeout` is configured. The 122B model generating a long research or plan response can easily exceed 60 seconds. The OpenHands condenser call (which also uses 35B with `stream: false`) is equally at risk. This is a documented LiteLLM proxy bug: the proxy's internal httpx client timeout fires at 60s regardless of `request_timeout`/`router_settings.timeout` values in many versions.

**Why it happens:**
LiteLLM runs uvicorn with a default `timeout_keep_alive`; the proxy-level timeout is set in `litellm_settings` but in certain versions is not propagated to the underlying httpx client for non-streaming paths. Streaming responses avoid this because the first chunk arrives quickly, resetting the idle timer.

**How to avoid:**
1. Set `stream: true` everywhere in LiteLLM config, even though `agent_settings.json` has `stream: false`. The mlx-lm backend and LangGraph nodes both handle streamed responses fine.
2. If you must keep `stream: false`, set `uvicorn_params: {"timeout_keep_alive": 600}` and `timeout: 600` in LiteLLM config, and verify with a long generation curl test before wiring the orchestrator.
3. Configure `num_retries: 0` initially for diagnosis — retries on a 504 will re-queue 122B generation and amplify memory pressure.

**Warning signs:**
- LangGraph research or plan node raises `litellm.exceptions.Timeout` or `httpx.ReadTimeout` exactly at the 60s mark.
- LiteLLM logs show `504` but the mlx server logs show the generation completing successfully afterward.

**Phase to address:** Phase 1 (LiteLLM/service wiring) — validate with a `curl` test that generates >60s of output before wiring any graph node.

---

### Pitfall 2: OpenHands SDK blocks the FastAPI/LangGraph asyncio event loop

**What goes wrong:**
The OpenHands SDK `run()` or equivalent entry point is synchronous (blocking). Calling it directly from an `async def` LangGraph node or FastAPI endpoint handler blocks the event loop for the entire duration of the OpenHands agent run (minutes). This starves FastAPI health checks, job-status polling endpoints, and LangGraph's own async machinery. The symptom is that the FastAPI server appears hung and any status-check request times out during execution.

**Why it happens:**
FastAPI/uvicorn owns the asyncio event loop. LangGraph nodes are invoked inside that same event loop unless you explicitly run the graph in a thread. The OpenHands SDK's core loop (`run()`, `step()`) makes synchronous LiteLLM calls internally and uses its own threading for the sandbox — it cannot be awaited. Calling sync blocking code from `async def` without `run_in_executor` / `asyncio.to_thread` freezes the event loop.

**How to avoid:**
Wrap the OpenHands SDK call in `asyncio.to_thread()` (Python 3.9+) inside the Execute node:

```python
import asyncio

async def execute_node(state):
    result = await asyncio.to_thread(_run_openhands_sync, state["plan"])
    return {"result": result}

def _run_openhands_sync(plan):
    # blocking SDK call goes here
    ...
```

Never call the SDK directly in `async def` without this wrapper.

**Warning signs:**
- FastAPI `/status` endpoint returns no response during an active Execute node run.
- uvicorn worker shows 100% CPU or appears hung; no interleaved log output from other async paths.
- Any LangGraph `astream()` call blocks without yielding.

**Phase to address:** Phase 1 (FastAPI + LangGraph scaffold) — wire the thread wrapper before any OpenHands integration, test that `/status` responds during a `asyncio.sleep(30)` placeholder.

---

### Pitfall 3: OpenHands agent infinite loop on Qwen 35B with native_tool_calling: false

**What goes wrong:**
With `native_tool_calling: false`, OpenHands parses tool calls from XML tags in model output. Qwen 3.5 35B (and earlier Qwen 3.x family) produces malformed XML tool-call syntax at a measurable rate — unclosed tags, wrong tag names, mixed JSON-in-XML outputs. When XML parsing fails, OpenHands re-prompts the agent with an error observation. The model re-fails at the same point, producing an identical malformed tag. With `num_retries: 5` in the LLM config and no iteration budget ceiling, this creates a tight loop that exhausts the context window, triggers the condenser (which also calls 35B), and can run indefinitely. Documented OpenHands SyntaxError rate is ~21.7% of execution failures on models without native tool calling.

**Why it happens:**
`native_tool_calling: false` means every tool invocation is a text-format prompt asking the model to emit a specific XML fragment. Smaller/weaker models are inconsistent at this. The agent loop's error recovery is to retry with an observation describing what went wrong — but if the model is stuck in a bad mode, the recovery prompt does not help.

**How to avoid:**
1. Set a hard `max_iterations` limit in the OpenHands runtime config (e.g., 15–20 steps). The existing `agent_settings.json` does not set this — add it.
2. Detect the "same tool call, same malformed output, N times in a row" pattern at the LangGraph wrapper level and abort the Execute node with an error rather than letting it exhaust context.
3. Investigate whether mlx-lm's Qwen 3.5 35B supports structured output / JSON mode as a proxy for tool reliability. If it does, configure it in LiteLLM's `litellm_params` for the `qwen-35b` entry.

**Warning signs:**
- OpenHands logs showing repeated `AgentAction(tool='...', args=...)` followed by `AgentObservation(content='SyntaxError: ...')` with identical content.
- The condenser fires before step 20 (means 35B is burning tokens on loop overhead rather than task work).
- 35B mlx-lm server shows continuous token generation without pauses between tool calls.

**Phase to address:** Phase 2 (Execute node + OpenHands integration) — add iteration cap and loop-detect logic before any real task is attempted.

---

### Pitfall 4: LangGraph state accumulates full conversation history across graph nodes

**What goes wrong:**
If the orchestrator stores LangGraph `messages` (Research output, Plan output, Execute transcript) as an accumulating list in graph state, the checkpointer serializes the entire list on every step. A single run with a long research response (~8k tokens), a plan (~2k tokens), and an Execute transcript (~10k tokens) produces state objects of 50–80KB. This gets written to disk (SqliteSaver) on every node transition. On job resume, LangGraph merges the checkpoint state with the initial input — if you re-invoke a completed thread, the messages list doubles. Additionally, the full state is passed to LangGraph's internal state-display machinery and can overwhelm log output.

**Why it happens:**
LangGraph's default `add_messages` reducer appends, never replaces. The schema is designed for chatbots where the full message history is always relevant. For a Research → Plan → Execute pipeline, only the final artifact of each prior stage matters to the next stage.

**How to avoid:**
1. Use separate, non-accumulating state fields for cross-node handoffs: `research: str`, `plan: str`, `result: str`. Pass only these typed fields, not raw `messages` lists.
2. If messages are needed for debugging, store them in a separate field with a `replace` reducer (not `add_messages`), and cap the stored content with a character limit.
3. On resume, pass `None` as input (not the initial goal) to re-enter from the checkpoint without re-merging initial state.

**Warning signs:**
- Checkpoint writes taking >100ms (check SqliteSaver timing in LangGraph debug logs).
- State object size growing proportionally with task complexity rather than staying flat.
- Job resume producing duplicate research or plan artifacts.

**Phase to address:** Phase 1 (LangGraph graph schema design) — define the TypedDict state schema upfront with explicit non-accumulating fields before writing any node logic.

---

### Pitfall 5: launchd starts the orchestrator before LiteLLM is ready (dependency ordering)

**What goes wrong:**
With `RunAtLoad: true` and `KeepAlive: true`, all `com.ohama.*` agents start simultaneously at login. The orchestrator's FastAPI service starts, tries to validate connectivity to LiteLLM on `:4000`, gets a connection refused, and either crashes or enters a bad state. launchd sees the crash, respawns immediately (ThrottleInterval default is 10s), the service crashes again — creating a restart storm. If ThrottleInterval is too short (e.g., 1s), launchd may permanently stop respawning after repeated rapid failures.

**Why it happens:**
launchd explicitly does not support service dependency ordering. The existing `com.ohama.litellm`, `com.ohama.qwen122b`, and `com.ohama.qwen36-35b` plists all use RunAtLoad and have their own startup times (model loading for 35B ~37s warm cache, 122B longer). The orchestrator's startup validation runs before any of them are ready.

**How to avoid:**
1. Do not fail fast on LiteLLM unavailability at startup. Instead, implement a lazy connectivity check: FastAPI starts and accepts requests; the first job submission triggers a LiteLLM health check with retry (e.g., poll `:4000/health` every 5s for up to 120s before returning 503).
2. Set `ThrottleInterval: 30` in the orchestrator plist to bound restart storm rate.
3. Add `KeepAlive: {SuccessfulExit: false}` if the process should only restart on crash, not on clean exit.
4. Do NOT use `WaitForDebugger` or `SuccessfulExit: true` as a workaround — these have unpredictable interactions on macOS.

**Warning signs:**
- Orchestrator plist logs showing repeated fast-exit cycles (check `~/Library/Logs/` or the configured `StandardErrorPath`).
- `launchctl list | grep com.ohama.orchestrator` showing a pid of `-` (not running) and a non-zero LastExitStatus.
- Any startup validation that calls `requests.get("http://localhost:4000/health")` with no retry is the bug.

**Phase to address:** Phase 3 (launchd service packaging) — write the startup probe logic before creating the plist.

---

### Pitfall 6: Running 35B and 122B simultaneously causes memory pressure and throughput collapse

**What goes wrong:**
The Qwen 3.5 122B-A10B-4bit model uses ~62 GB of RAM at inference time. The 35B-A3B-4bit uses ~22 GB. Combined RSS is ~84 GB. On a Mac with 96 GB unified memory, this leaves ~12 GB for the OS, orchestrator process, and swap. When the Execute node (35B) runs while 122B is still loaded from the Research/Plan phase (mlx-lm does not unload automatically), macOS begins heavy swap compression. The result is generation throughput for 35B dropping from ~35 tok/s to ~5–8 tok/s as memory pressure triggers kernel paging. A 10-step Execute run that would take 3 minutes at normal throughput can take 15–20 minutes. This also makes the 122B condenser call slower if it fires during Execute.

**Why it happens:**
mlx-lm models are loaded into unified memory and stay loaded until the server process is killed or memory reclamation forces eviction. The Research → Plan phase keeps 122B loaded; the Execute phase then loads 35B on top. There is no automatic model-unload between stages.

**How to avoid:**
1. Structure the LangGraph graph to run Research → Plan (122B) as a complete unit, then issue a `launchctl kickstart -k gui/501/com.ohama.qwen122b` restart before Execute to free 122B memory. This matches the KV-cache flush pattern already proven in blueCode v2.3.
2. Alternatively, accept the memory pressure for now (the 96 GB machine handles it at degraded speed) and add a TODO to implement explicit model unload if throughput proves unacceptable.
3. Do NOT run Research and Execute concurrently even if the graph is later parallelized — the combined memory load would hit OOM.

**Warning signs:**
- `memory_pressure` CLI tool showing red during Execute node runs.
- 35B mlx-lm server logs showing lower tok/s than warm-cache baseline.
- macOS `vm_stat` showing high `Pages occupied by compressor` count.

**Phase to address:** Phase 2 (Execute node integration) — measure throughput with both models loaded; add kickstart step if needed.

---

### Pitfall 7: LangGraph MemorySaver loses all job state on process restart

**What goes wrong:**
Using `MemorySaver` (the default in-memory checkpointer) means every job run is lost if the orchestrator process restarts — including jobs that were in-flight. With `KeepAlive: true` in launchd, the orchestrator restarts automatically after a crash, but any resume attempt will find no checkpoints. A completed Research + Plan that was mid-Execute is gone. Users who submitted a job and come back to check status see no result.

**Why it happens:**
`MemorySaver` is the documentation default and works for demos. Nothing in the basic LangGraph API warns that it is restart-volatile. The "job state persisted" requirement in PROJECT.md cannot be met with `MemorySaver`.

**How to avoid:**
Use `SqliteSaver` from `langgraph-checkpoint-sqlite` for single-process single-user deployment. Configure it to write to a stable path (e.g., `~/.local/share/langgraph-orchestrator/checkpoints.db`). The DB persists across restarts and launchd-triggered respawns. SqliteSaver is appropriate for a single-operator local service — no concurrency risk.

```python
from langgraph.checkpoint.sqlite import SqliteSaver
checkpointer = SqliteSaver.from_conn_string("~/.local/share/langgraph-orchestrator/checkpoints.db")
```

Do not use `PostgresSaver` — unnecessary complexity for a single-user local service.

**Warning signs:**
- Any code path that does `MemorySaver()` in production (not tests).
- Job status returns 404 after an orchestrator restart.

**Phase to address:** Phase 1 (LangGraph scaffold) — wire SqliteSaver from the first commit.

---

### Pitfall 8: Qwen 122B plan quality degrades on ambiguous or underspecified goals

**What goes wrong:**
The Research and Plan nodes use 122B, which produces high-quality output on well-specified goals. However, with `enable_thinking: false` and without chain-of-thought, the 122B model in planning mode tends to produce plans that look plausible but contain vague steps ("investigate the codebase", "make the necessary changes") when the goal is underspecified. The Execute node (35B) receives this vague plan and either loops trying to interpret it, or takes a destructive best-guess action.

This compounds the 35B tool-calling reliability issue: a vague plan step gives 35B insufficient guidance to produce a correctly-formatted tool call.

**Why it happens:**
`enable_thinking: false` removes the extended reasoning chain that allows 122B to notice gaps in a goal. The system prompt for the Plan node must compensate with explicit output schema constraints (specific files, specific actions, verifiable done criteria per step — the same GSD-style plan discipline from blueCode v2.0+).

**How to avoid:**
1. The Plan node system prompt must enforce a strict schema: each step must name specific files, a specific action (create/edit/run), and a done-check. Mirror the blueCode `planSystemPromptSuffix` discipline.
2. Add a plan-validation step in the LangGraph graph between Plan and Execute nodes that checks for vague steps (no file named, no action type) and either re-prompts or rejects with an error.
3. Test with a deliberately ambiguous goal input to verify the validation catches it before the Execute node runs.

**Warning signs:**
- Plan output contains steps like "review the code" or "update as needed" without file paths.
- Execute node receives a plan step and asks a clarifying question (because the step is too vague), then enters a clarification loop.

**Phase to address:** Phase 2 (Plan node + Plan → Execute handoff).

---

### Pitfall 9: OpenHands workspace/cwd defaults conflict with the orchestrator's working directory

**What goes wrong:**
When OpenHands SDK is called in-process without explicit workspace configuration, it defaults its working directory to the process cwd (the orchestrator's directory) or a temp path. Shell commands run by the Execute agent (`ls`, `git status`, etc.) operate on the wrong directory. File writes created by the agent land in unexpected locations. Worse, if the orchestrator lives under `~/projs/LangGraph_OpenHands/`, the agent may accidentally modify the orchestrator's own source files.

**Why it happens:**
The OpenHands SDK `workspace_base` parameter (or its equivalent in the version in use) must be explicitly set per-job. The `agent_settings.json` file has no `workspace_base` key — the current config is designed for CLI use where the CWD is set by the terminal. When called in-process from a launchd service, the CWD is the service's WorkingDirectory (if set) or `/`.

**How to avoid:**
1. For each job, set the `workspace_base` to a job-specific directory: `~/projs/langgraph-jobs/<job-id>/workspace/`. This also provides natural job isolation.
2. Add the orchestrator's own directory to an explicit exclude/deny list if the SDK supports it, so the agent cannot write there even if CWD is wrong.
3. Verify by having a smoke-test job run `pwd` and `ls` and checking the output matches the expected job workspace.

**Warning signs:**
- File writes from an Execute job appearing in `~/projs/LangGraph_OpenHands/` instead of a job workspace directory.
- `git status` output in agent observations mentioning the orchestrator's own files.

**Phase to address:** Phase 2 (Execute node integration).

---

### Pitfall 10: Autonomous Execute with no approval gate can modify the wrong repository or run destructive shell commands

**What goes wrong:**
The fully autonomous design (no approval gate) means the Execute node runs shell commands, creates files, and modifies code immediately upon receiving the plan. Without a workspace scope constraint, the OpenHands agent can run `rm -rf`, `git reset --hard`, or `pip install` system-wide. On a single-operator Mac where the user's real projects are in `~/projs/`, an agent targeting the wrong `workspace_base` can delete actual work.

**Why it happens:**
OpenHands in CLI mode has a `security_policy.j2` system prompt injection that warns the agent about risky operations. The existing `agent_settings.json` references this file. However, the security policy is a prompt instruction — it can be ignored by a sufficiently confused 35B model that misinterprets the task. The `ConfirmRisky` policy (which pauses on destructive operations) is off in the current config (no `confirm_mode` key visible).

**How to avoid:**
1. Set `workspace_base` per-job to an isolated directory (as in Pitfall 9). The sandbox then prevents writes outside that directory at the OS level if using DockerWorkspace, or relies on convention with LocalWorkspace.
2. For v1 with LocalWorkspace, add a pre-Execute step that verifies the plan contains no shell commands with destructive patterns (`rm -rf`, `git reset`, system-level installs) and rejects/escalates if found.
3. Set an explicit `max_iterations` so a confused agent cannot run indefinitely.
4. Log all shell command observations to a persistent file so you can audit what ran.

**Warning signs:**
- Plan steps containing `rm`, `reset`, or references to directories outside the job workspace.
- Execute agent observations mentioning files outside the intended workspace.
- No `max_iterations` set in the SDK config.

**Phase to address:** Phase 2 (Execute node) and Phase 3 (service hardening).

---

### Pitfall 11: Condenser double-calls 35B, amplifying load during already-slow Execute runs

**What goes wrong:**
The condenser config in `agent_settings.json` uses `qwen-35b` as both the primary agent LLM and the condenser LLM. When context fills (max_size: 80 events), the condenser fires a separate summarization call to 35B. This happens in the middle of the Execute agent run. The 35B server is serving the main agent loop; the condenser call queues on top. Since mlx-lm has `tool_concurrency_limit: 1` for the model itself and processes requests sequentially, the condenser call either starves the main loop or vice versa. This can cause the main agent loop to timeout waiting for a condenser response, while the condenser waits for the main loop to free the model.

**Why it happens:**
The condenser and the main LLM share the same endpoint (`openai/qwen-35b` → `localhost:4000` → mlx port 8000). mlx-lm processes one request at a time. Two simultaneous requests queue, but the requesting code may have timeouts shorter than the combined wait.

**How to avoid:**
1. Point the condenser at 122B (`qwen-122b`) rather than 35B — 122B is idle during Execute. This removes the resource contention.
2. Alternatively, increase the condenser `max_size` to a value where condensation is unlikely to fire during normal Execute runs (e.g., 150 events for a 20-step job). The risk is context overflow if the task runs long.
3. In LiteLLM, set a longer timeout for the `qwen-35b` route to accommodate sequential condenser + agent calls.

**Warning signs:**
- Condenser firing (log message from OpenHands about condensation) causing agent step timeout immediately after.
- 35B mlx-lm logs showing two concurrent requests queued.
- Task completion times much slower than expected given step count.

**Phase to address:** Phase 2 (Execute node configuration).

---

### Pitfall 12: OpenHands CLI v1.16 vs SDK v1.21 version split causes config format mismatch

**What goes wrong:**
The `~/.openhands/agent_settings.json` was written for the OpenHands CLI (v1.16) which loads the SDK at v1.21 runtime. When the orchestrator imports the SDK directly (`from openhands import ...`), it uses v1.21 APIs. Config fields that exist in v1.21 SDK may not match the JSON structure the CLI expects, or vice versa. Specifically: v1.23 SDK (the next version available) changed skills defaulting and added new condenser token threshold settings. If the orchestrator upgrades the SDK to any version other than the exact runtime v1.21, the `agent_settings.json` structure may silently misparse (unknown fields are often ignored, meaning features are silently disabled rather than erroring).

**Why it happens:**
The CLI and SDK are separate packages with independent versioning. The PROJECT.md explicitly notes the CLI/SDK version split. The `agent_settings.json` is the CLI's config format; the SDK's Python config class may have different field names or structure at v1.21 vs the JSON on disk.

**How to avoid:**
1. Pin the SDK version in the orchestrator's `requirements.txt` or `pyproject.toml` to exactly `openhands-ai==1.21.0` (or whatever version the CLI loads at runtime).
2. Do not read `agent_settings.json` directly into the SDK config class without validating that all fields deserialize correctly. Write a test that loads the JSON and constructs the SDK config object, asserting no unexpected-field warnings.
3. When upgrading the SDK, treat `agent_settings.json` as a migration target and test explicitly.

**Warning signs:**
- SDK initialization silently ignoring `native_tool_calling: false` (tool calls switch to native format unexpectedly).
- Condenser config silently ignored (no condensation happens despite long Execute runs).
- Any `UserWarning: Unknown field` from pydantic during SDK initialization.

**Phase to address:** Phase 2 (Execute node integration).

---

## Technical Debt Patterns

| Shortcut | Immediate Benefit | Long-term Cost | When Acceptable |
|----------|-------------------|----------------|-----------------|
| MemorySaver for checkpoints | Zero setup, works in tests | All job state lost on restart | Tests only; never in production service |
| stream: false on LiteLLM | Simpler response handling | 504 timeouts on long 122B responses | Never for responses >30s expected duration |
| Hardcoding workspace to orchestrator CWD | Works for dev | Agent modifies orchestrator files | Never in any deployed config |
| Skipping max_iterations in OpenHands | Agent has more "room" | Infinite loops, OOM, no job completion | Never; always set a ceiling |
| Calling OpenHands SDK directly in async def | Less boilerplate | FastAPI event loop starvation | Never; always use asyncio.to_thread |
| Single plist without ThrottleInterval | Works at first launch | Restart storm on crash | Never; always set ThrottleInterval ≥ 30 |

---

## Integration Gotchas

| Integration | Common Mistake | Correct Approach |
|-------------|----------------|------------------|
| LiteLLM → mlx-lm | Relying on LiteLLM's default 60s timeout for 122B responses | Set `timeout: 600` in litellm_settings AND verify it propagates to httpx client |
| OpenHands SDK → LiteLLM | Using `stream: false` (current agent_settings default) with long generations | Switch condenser and agent to `stream: true`; handle streamed response aggregation |
| LangGraph → OpenHands | Calling SDK's blocking run() directly in async node | Wrap with `asyncio.to_thread()` unconditionally |
| launchd → orchestrator | Startup health check calling LiteLLM before it's ready | Lazy probe with retry loop; return 503 until LiteLLM responds |
| launchd → orchestrator | KeepAlive with ThrottleInterval missing | Always set `ThrottleInterval: 30` to prevent restart storms |
| mlx-lm → concurrent model load | Starting Execute node while 122B still loaded | Unload 122B via `launchctl kickstart -k` between Plan and Execute phases |
| OpenHands workspace | Not setting workspace_base per-job | Explicit per-job directory, never inherit process CWD |

---

## Performance Traps

| Trap | Symptoms | Prevention | When It Breaks |
|------|----------|------------|----------------|
| Both models loaded during Execute | 35B throughput drops from 35 tok/s to <8 tok/s | Unload 122B after Plan, reload before next Research | Any run where 122B+35B combined RSS > ~80 GB |
| LangGraph state storing full message history | Checkpoint writes >100ms; state grows linearly with conversation length | Use typed string fields for cross-node artifacts, not messages list | After ~5 jobs with long Research outputs |
| Condenser sharing the same model endpoint as agent | Condenser fires → agent request queues → timeout | Point condenser at 122B during Execute phase | When agent context exceeds max_size (80 events by default) |
| SqliteSaver on every node transition | Write latency spikes under rapid node transitions | Acceptable for linear Research→Plan→Execute (3 writes per job); do not add unnecessary intermediate nodes | Not a concern until >100 jobs/day |
| LiteLLM `num_retries: 5` on 122B timeout | A failed 122B generation retries 5 times, each taking up to 120s | Set retries to 1 or 0 for Research/Plan nodes; handle retry at orchestrator level with user notification | Any run where 122B produces a timeout |

---

## Security Mistakes

| Mistake | Risk | Prevention |
|---------|------|------------|
| No workspace isolation per job | Execute agent can write anywhere the process user can write (~/ is fair game) | Set workspace_base to per-job directory; use OS-level chroot or Docker for stronger isolation in v2 |
| Logging full plan/result to stdout | Plan content (possibly including sensitive project details) in launchd logs | Log task metadata only (job id, status, step count); never log plan text to a file that might be world-readable |
| `dummy` API key in agent_settings.json in a git repo | No active risk for localhost-only service | Keep agent_settings.json out of git; it contains model routing config that may drift from actual setup |
| Execute agent can run `git push` or `curl` to external services | Could exfiltrate code or trigger external actions | Add network-level restriction or explicit deny-list in security_policy.j2 for outbound calls outside localhost |

---

## "Looks Done But Isn't" Checklist

- [ ] **LiteLLM timeout:** `timeout: 600` in litellm config — verify a 90-second test generation completes without 504, not just that the config key is present.
- [ ] **Checkpointer persistence:** Job submitted → orchestrator restarted → `/status/<job_id>` still returns result — MemorySaver passes smoke test but fails this check.
- [ ] **Event loop non-blocking:** `/status` endpoint responds within 200ms while Execute node is actively running — the common failure mode looks fine in sequential tests.
- [ ] **Workspace isolation:** File written by Execute job appears in `~/projs/langgraph-jobs/<id>/workspace/` not in `~/projs/LangGraph_OpenHands/` — easy to miss if first test task happens to target an isolated directory anyway.
- [ ] **Restart survival:** launchd plist installed → `sudo reboot` → service appears in `launchctl list` with a pid within 120s — RunAtLoad alone does not guarantee this; the plist must be in `~/Library/LaunchAgents/` not just in the project directory.
- [ ] **max_iterations set:** OpenHands agent config has an explicit iteration ceiling — check the runtime config object, not just the JSON file (SDK may ignore the field if key name mismatches).
- [ ] **Model version split:** SDK import version matches expected v1.21 — `import openhands; print(openhands.__version__)` from the orchestrator's venv.

---

## Recovery Strategies

| Pitfall | Recovery Cost | Recovery Steps |
|---------|---------------|----------------|
| 504 timeout from LiteLLM | LOW | Enable streaming on LiteLLM config; restart LiteLLM service |
| Event loop blocked | LOW | Add `asyncio.to_thread()` wrapper; redeploy |
| MemorySaver in production | MEDIUM | Swap to SqliteSaver; all prior job history is lost (no migration path) |
| OpenHands loop without max_iterations | MEDIUM | Kill Execute process; set max_iterations; may need manual workspace cleanup |
| Memory pressure / throughput collapse | LOW | `launchctl kickstart -k gui/501/com.ohama.qwen122b` to free 62 GB; restoring normal throughput takes ~37s warm-cache |
| launchd restart storm | LOW | `launchctl unload` the plist; add `ThrottleInterval: 30`; reload |
| Wrong workspace_base | HIGH | Manual audit of what the agent wrote and where; git reset if code was modified; no automated recovery path |
| CLI/SDK version mismatch | MEDIUM | Pin SDK version; diff agent_settings.json fields against SDK config class; rebuild venv |

---

## Pitfall-to-Phase Mapping

| Pitfall | Prevention Phase | Verification |
|---------|------------------|--------------|
| LiteLLM 504 on long non-streaming responses | Phase 1 (LiteLLM/service wiring) | `curl` test generating >90s response completes successfully |
| Blocking the FastAPI event loop | Phase 1 (FastAPI + LangGraph scaffold) | `/status` responds in <200ms while graph node runs a `asyncio.to_thread(sleep, 30)` placeholder |
| OpenHands agent infinite loop on tool-call failures | Phase 2 (Execute node integration) | Run with a deliberately bad task; confirm exit after max_iterations |
| LangGraph state bloat | Phase 1 (graph state schema) | Checkpoint size stays flat across 3 sequential test jobs |
| launchd startup before LiteLLM ready | Phase 3 (launchd packaging) | Unload/reload LiteLLM after orchestrator; orchestrator recovers within retry window |
| Memory pressure from dual-model load | Phase 2 (Execute node) | Measure 35B tok/s with and without 122B loaded; decide kickstart strategy |
| MemorySaver loses job state on restart | Phase 1 (graph state schema) | Kill orchestrator mid-Execute; restart; verify job resumes from checkpoint |
| Vague 122B plan quality | Phase 2 (Plan node) | Submit underspecified goal; verify plan validator rejects or requests clarification |
| Wrong workspace_base / CWD | Phase 2 (Execute node) | Run smoke job; confirm all file writes land in job-specific directory |
| Autonomous destructive Execute | Phase 2 + Phase 3 | Smoke test with plan containing `rm` command; verify rejection before execution |
| Condenser + agent resource contention | Phase 2 (Execute configuration) | Run a 30-step Execute job; confirm no timeout caused by condenser firing |
| CLI/SDK version mismatch | Phase 2 (OpenHands integration) | `openhands.__version__` == expected; run SDK config construction in test |

---

## Sources

- LangGraph state management pitfalls: https://altersquare.io/langgraph-state-management-undocumented-issues-after-commit/
- LangGraph GRAPH_RECURSION_LIMIT docs: https://docs.langchain.com/oss/python/langgraph/errors/GRAPH_RECURSION_LIMIT
- LangGraph agent infinite looping issue (Jan 2026): https://github.com/langchain-ai/langgraph/issues/6731
- LiteLLM 504 on non-streaming: https://github.com/BerriAI/litellm/issues/9551
- LiteLLM drop_params docs: https://docs.litellm.ai/docs/completion/drop_params
- OpenHands local LLM limitations: https://docs.openhands.dev/openhands/usage/llms/local-llms
- OpenHands context condenser: https://docs.openhands.dev/sdk/guides/context-condenser
- OpenHands native_tool_calling semantics issue: https://github.com/OpenHands/OpenHands/issues/8424
- OpenHands SDK releases (v1.21+): https://github.com/OpenHands/software-agent-sdk/releases
- launchd KeepAlive restart storm patterns: https://github.com/tjluoma/launchd-keepalive
- launchd ThrottleInterval docs: https://www.manpagez.com/man/5/launchd.plist/
- FastAPI event loop blocking: https://github.com/fastapi/fastapi/discussions/8842
- mlx-lm dual-model memory: https://insiderllm.com/guides/qwen35-local-guide-which-model-fits-your-gpu/
- blueCode v2.3 KV-cache contamination / kickstart pattern: .planning/PROJECT.md (Key Decisions table, Phase 26 Diagnostic D)
- blueCode v2.1 HTTP-only constraint (no in-process mlx_lm.load()): .planning/PROJECT.md (v2.1 milestone decision)
- OpenHands XML SyntaxError rate (~21.7%): https://arxiv.org/html/2511.03690v1

---
*Pitfalls research for: LangGraph + OpenHands local orchestrator (Research → Plan → Execute) on macOS*
*Researched: 2026-06-19*
