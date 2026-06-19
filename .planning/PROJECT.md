# LangGraph + OpenHands Orchestrator

## What This Is

A local multi-agent orchestration system where **LangGraph** acts as the "brain" (Research → Plan) and **OpenHands** acts as the "hands" (Execute). A user submits a goal to an always-on REST service; the orchestrator researches the problem, produces a plan, and autonomously drives OpenHands to carry it out — all on locally-served Qwen models. Built for a single operator (the author) running everything on one Mac.

## Core Value

A goal submitted to the service is autonomously researched, planned, and executed end-to-end by local agents — and the service survives reboots so it's always available.

## Requirements

### Validated

<!-- Shipped and confirmed valuable. -->

(None yet — ship to validate)

> **Pre-existing substrate (already running on this machine, not built by this project):**
> - Qwen 35B served via `mlx_lm.server` on `:8000` (launchd `com.ohama.qwen36-35b`)
> - Qwen 122B served via `mlx_lm.server` on `:8001` (launchd `com.ohama.qwen122b`)
> - LiteLLM proxy on `:4000` routing `qwen-35b`, `qwen-122b`, `qwen-local` (launchd `com.ohama.litellm`)
> - OpenHands CLI/SDK installed via uv (`~/.local/share/uv/tools/openhands`, SDK v1.21), configured at `~/.openhands/agent_settings.json`

### Active

<!-- Current scope. Building toward these. Hypotheses until shipped. -->

- [ ] LangGraph orchestrator with a linear Research → Plan → Execute graph
- [ ] Research node runs on Qwen 122B (via LiteLLM `qwen-122b`)
- [ ] Plan node runs on Qwen 122B
- [ ] Execute node drives OpenHands via the Python SDK (in-process), running on Qwen 35B
- [ ] Fully autonomous flow — no human approval gate between Plan and Execute
- [ ] FastAPI service exposing job submit / status / result endpoints
- [ ] CLI client to submit jobs and read status/results
- [ ] launchd service so the orchestrator auto-starts and survives reboot
- [ ] Job/run state persisted (LangGraph checkpointer) so runs survive process restarts

### Out of Scope

<!-- Explicit boundaries. Includes reasoning to prevent re-adding. -->

- Human-in-the-loop plan approval gate — chose fully autonomous for v1; revisit if autonomous runs go off the rails
- Parallel / supervisor multi-agent execution — v1 is a single linear pipeline; parallelism deferred to keep MVP clear
- Web UI — REST API + CLI is sufficient for a single operator; UI deferred
- ACP / headless-CLI handoff to OpenHands — chose in-process Python SDK for tightest coupling and easiest debugging
- Multi-user / auth / remote access — single local operator on localhost only
- Building/serving the LLMs themselves — mlx + LiteLLM stack already exists and is owned separately

## Context

- **Platform:** macOS (Apple Silicon), single machine, single operator (`ohama`).
- **LLM access path:** Everything talks to LiteLLM on `http://localhost:4000/v1` (OpenAI-compatible). Model names: `qwen-122b` (smart, port 8001 backend, 16k max-tokens), `qwen-35b` (fast, port 8000 backend, 8k max-tokens). `enable_thinking: false` on both backends.
- **OpenHands wiring:** `~/.openhands/agent_settings.json` currently points the agent at `openai/qwen-35b` @ `localhost:4000/v1`, `native_tool_calling: false`, `reasoning_effort: high`, tools: terminal / file_editor / task_tracker / task_tool_set, condenser on 35B. The Execute node will reuse/derive from this configuration via the SDK.
- **Service pattern to mirror:** existing launchd plists in `~/Library/LaunchAgents/com.ohama.*.plist` (RunAtLoad + KeepAlive + ThrottleInterval, logs to a known dir). The orchestrator service should follow the same convention.
- **Version note:** OpenHands CLI tool is v1.16.0 but loads SDK v1.21.0 at runtime — pin/verify the SDK version the orchestrator imports.
- **Related local dirs:** `~/projs/openhands_litellm` (setup notes), `~/projs/gsd-openhands`, `~/agent-stack/` (litellm venv + config), `~/llm-system/` (models + mlx env).

## Constraints

- **Tech stack**: Python + LangGraph + OpenHands SDK + FastAPI — OpenHands SDK is Python-only, so the orchestrator process is Python to call it in-process.
- **LLM endpoint**: All model calls go through LiteLLM `:4000` — do not hit mlx ports (8000/8001) directly; keep a single routing layer.
- **Model roles**: Research/Plan must use `qwen-122b`; Execute uses `qwen-35b` — deliberate quality-vs-speed split, 122B is otherwise idle.
- **Resource**: Running 122B and 35B simultaneously is memory/throughput-heavy on one Mac — orchestrator should avoid hammering both at once where possible.
- **Reboot survival**: Service must be a launchd agent (RunAtLoad + KeepAlive), matching the existing `com.ohama.*` pattern.
- **Local-only**: Bind to localhost; no external exposure, no auth in v1.

## Key Decisions

<!-- Decisions that constrain future work. -->

| Decision | Rationale | Outcome |
|----------|-----------|---------|
| OpenHands handoff via in-process Python SDK | Tightest coupling, pass state/results as objects, easiest to debug in one process | — Pending |
| Research/Plan on 122B, Execute on 35B | Use the idle smart model for thinking; keep execution fast | — Pending |
| Fully autonomous (no approval gate) | Fire-and-forget operation for a single trusted operator | — Pending |
| REST API (FastAPI) + CLI client | Mirrors existing mlx/litellm service pattern; remote-callable, no frontend work | — Pending |
| Always-on launchd service | Reboot survival, consistent with existing stack | — Pending |
| Linear Research → Plan → Execute graph for v1 | Clearest MVP; parallel/supervisor deferred | — Pending |

---
*Last updated: 2026-06-19 after initialization*
