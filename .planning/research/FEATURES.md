# Feature Research

**Domain:** Local multi-agent orchestration service (Research → Plan → Execute pipeline)
**Researched:** 2026-06-19
**Confidence:** HIGH

---

## Feature Landscape

### Table Stakes (Users Expect These)

Features that a single operator expects to work correctly. Missing any of these makes the tool
unusable or untrustworthy.

| Feature | Why Expected | Complexity | Notes |
|---------|--------------|------------|-------|
| **Job submission** — POST /jobs with a goal string, returns a job_id | Entry point; without it nothing starts | LOW | FastAPI route; synchronous enqueue, async execution. Return 202 + job_id. |
| **Job status polling** — GET /jobs/{id}/status returns phase + state | Operator needs to know if it's running, stuck, or done | LOW | States: PENDING, RESEARCHING, PLANNING, EXECUTING, DONE, FAILED, CANCELLED. Phase matches LangGraph node. |
| **Result retrieval** — GET /jobs/{id}/result returns final artifact | The whole point of the service; without it runs are black boxes | LOW | Return the last successfully written artifact path or inline content. |
| **Cancel a run** — DELETE /jobs/{id} stops an in-flight job | Long runs (30–60 min LLM + OpenHands) must be stoppable | MEDIUM | Send cancellation signal to running LangGraph thread; mark state CANCELLED. LangGraph cancellation does not guarantee checkpoint persistence mid-node — document this. |
| **Persist run state across restarts** — LangGraph SQLite checkpointer | launchd will restart the process; runs must survive | MEDIUM | `langgraph-checkpoint-sqlite` per-thread keyed by job_id. Survive cold restart and resume from last completed node. |
| **Resume after restart** — re-attach to in-progress threads on startup | Without this, a mid-run restart silently orphans the job | MEDIUM | On startup: scan checkpointer for threads in non-terminal state; mark them RESUMING and re-invoke the graph from checkpoint. |
| **Health endpoint** — GET /health returns service liveness | launchd KeepAlive does not mean the app is functional | LOW | Check: FastAPI alive, SQLite reachable, LiteLLM reachable (lightweight ping). Return 200 / 503. |
| **launchd service plist** — RunAtLoad + KeepAlive, matching com.ohama.* pattern | Core constraint: service must survive reboots | LOW | Mirror existing plists. ExitTimeOut 30 for graceful SIGTERM. Log to known dir. |
| **Intermediate artifact storage** — research notes, plan, execution transcript written to disk | Without these, debugging a failed/bad run is impossible | LOW | Write to `~/jobs/{job_id}/research.md`, `plan.md`, `execution_transcript.json` after each node. |
| **CLI submit** — `orchestrator submit "goal"` prints job_id | Only way a single operator kicks off a job without curl | LOW | Wraps POST /jobs. Print job_id to stdout for piping. |
| **CLI status** — `orchestrator status {job_id}` | Operator needs to check without opening a browser | LOW | Wraps GET /jobs/{id}/status. Human-readable output. |
| **CLI logs** — `orchestrator logs {job_id}` | Operator needs to see what happened without digging into files manually | LOW | Tail or dump the per-run log file. |
| **CLI result** — `orchestrator result {job_id}` | Retrieve final artifact from terminal | LOW | Wraps GET /jobs/{id}/result. Print or open file. |
| **Per-run structured logging** — each job writes its own log file with timestamps | Without this, debugging failures requires grepping a shared log | LOW | Python logging with a per-job FileHandler added at job start. Include node transitions, LLM call metadata, error stack traces. |
| **LiteLLM unavailable handling** — fail gracefully when model endpoints are down | LLM servers can die; the service must not hang forever | MEDIUM | Timeout + retry policy on LangGraph nodes (LangGraph `RetryPolicy` on nodes). Mark job FAILED with clear error message after exhausted retries. |
| **OpenHands SDK error propagation** — catch SDK exceptions in Execute node | OpenHands runs can fail (model error, tool error, timeout) | MEDIUM | Wrap SDK call in try/except; capture ConversationState on failure; write partial transcript; set job state to FAILED with reason. |

---

### Differentiators (Competitive Advantage)

These are features that make this specific local setup valuable beyond a naive script — things a
simple `subprocess.run(["openhands", ...])` chain would not give you.

| Feature | Value Proposition | Complexity | Notes |
|---------|-------------------|------------|-------|
| **LangGraph stateful graph** — typed state object flows Research → Plan → Execute | Research output is input to Plan; Plan output is input to Execute. Not just piped strings. | MEDIUM | Use `TypedDict` state: `{goal, research_notes, plan, execution_transcript, error, model_used_per_node}`. Each node reads/writes structured fields. |
| **Node-aware status** — status reports which LangGraph node is currently executing | Operator knows whether the 122B is still researching or if OpenHands has started | LOW | Embed current_node in checkpointed state; expose in /status response. |
| **Per-node model attribution** — status/result records which model ran each node | Debugging "why did the plan look bad?" requires knowing which model was used | LOW | Write `{node: "research", model: "qwen-122b", tokens_in: X, tokens_out: Y}` into state at node completion. |
| **Graceful SIGTERM handling** — on launchd restart, finish current LLM call then checkpoint** | Without this, every process restart corrupts the mid-node state | MEDIUM | Install SIGTERM handler in FastAPI startup: set a shutdown flag; after current async LLM call completes, persist checkpoint, then exit. LangGraph checkpointer only guarantees between-node persistence — do not rely on mid-node checkpoint. |
| **Startup resume scan** — on process start, re-queue orphaned in-progress jobs | A mid-node crash leaves threads dangling; auto-recovery makes the service feel reliable | MEDIUM | On FastAPI startup event: query SQLite for threads with non-terminal status; re-invoke graph from checkpoint. Cap resume attempts (max 2) to avoid infinite retry loops on structurally broken runs. |
| **Structured artifact manifest** — `~/jobs/{job_id}/manifest.json` lists all artifacts with paths and sizes | Operator can immediately find what the run produced | LOW | Write after each node completes and on run completion. Consumable by future tooling. |
| **Execution transcript capture** — write OpenHands `ConversationState` events to disk | Full audit trail of every action/observation the Execute node took | MEDIUM | OpenHands SDK emits typed `Action`/`Observation` events. Serialize to `execution_transcript.jsonl` line-by-line as they arrive. |
| **Model role separation enforced in config** — Research/Plan always qwen-122b, Execute always qwen-35b, not runtime-configurable per job | Prevents accidental expensive/slow runs; keeps the quality/speed split deliberate | LOW | Hardcode in orchestrator config, not in per-job payload. Single operator; no need for per-job model override. |
| **OpenTelemetry tracing via OpenHands SDK** — OTEL spans for Execute node automatically captured | Zero-code observability of the Execute node's LLM calls, tool calls, and iteration count | LOW | OpenHands SDK emits OTEL spans automatically when `OTEL_*` env vars are set. Wire to a local Jaeger or just file export. Optional but nearly free to enable. |

---

### Anti-Features (Deliberately NOT Built in v1)

These features are tempting but wrong for a single-operator local tool at v1 scope. Documenting
the reason prevents them from sneaking back in.

| Anti-Feature | Why Requested | Why Avoid for v1 | What to Do Instead |
|---|---|---|---|
| **Plan approval gate (human-in-the-loop)** | Safety: review the plan before OpenHands executes | Defeats the fire-and-forget value; adds UI/interaction complexity; PROJECT.md explicitly excludes it | Trust the plan. If autonomous runs go off the rails, add the gate in v2 after seeing failure modes. |
| **Web UI / dashboard** | Visibility: see job list, status, logs in a browser | Single operator with a terminal; adds frontend work with zero functional gain over CLI | Use `orchestrator status`, `orchestrator logs`. Add a UI only if CLI becomes painful. |
| **Multi-user / auth / rate limiting** | Generalization: share the service with others | Localhost-only; no external exposure; auth adds complexity for zero benefit | Bind to 127.0.0.1. No auth header. Single operator = implicit trust. |
| **Parallel job execution / queue worker** | Throughput: run multiple goals simultaneously | 122B + 35B already saturate one Mac's memory simultaneously; parallel runs would cause OOM or GPU contention | Serialize: one job at a time. Enqueue subsequent submits; drain sequentially. |
| **Parallel / supervisor multi-agent within a run** | Quality: spawn sub-agents to parallelize research | Adds coordination complexity; 122B model has enough context for sequential research; MVP is a linear graph | Keep Research → Plan → Execute linear. Add parallelism only after linear pipeline is proven. |
| **Pluggable node registry / dynamic graph config** | Flexibility: add new nodes without code changes | Over-engineering for a personal tool; three fixed nodes is the entire MVP scope | Hardcode the three-node graph. Extend in code when needed. |
| **Real-time streaming endpoint (SSE / WebSocket)** | UX: see tokens as they stream during a long run | Adds async complexity to both server and CLI; polling /status every 5s is sufficient for a single operator | Implement `orchestrator status --watch` that polls on a timer. No server-side push needed. |
| **Celery / Redis / external task queue** | Scalability: proper worker separation | Massively over-engineered for one operator, one concurrent job, one machine. Adds two more services to manage. | Use FastAPI `BackgroundTasks` or a single `asyncio.Task` per job. SQLite is the queue. |
| **ACP / headless-CLI OpenHands handoff** | Alternative handoff pattern | In-process SDK gives tighter coupling, structured state, and easier debugging. PROJECT.md explicitly excludes CLI handoff. | Python SDK in-process only. |
| **Building or managing LLM servers** | Control: manage mlx/LiteLLM from orchestrator | Completely out of scope; existing launchd services own that substrate | Treat LiteLLM on :4000 as an external dependency. Health check it; don't manage it. |
| **Cost tracking / billing UI** | Visibility: see token spend | Single operator on local models; no dollar cost per token. Token count in logs is sufficient. | Log `tokens_in` / `tokens_out` per node in the structured log. No dashboard needed. |
| **Per-job model selection via API** | Flexibility: caller chooses which model does Research | Defeats the deliberate quality/speed split. Adds API surface complexity. | Hardcode: 122B for Research/Plan, 35B for Execute. Change in config file only. |

---

## Feature Dependencies

```
[Job submission API]
    └──enables──> [Job status polling]
    └──enables──> [Cancel a run]
    └──enables──> [Result retrieval]

[LangGraph SQLite checkpointer]
    └──required by──> [Persist run state across restarts]
    └──required by──> [Resume after restart on startup]
    └──required by──> [Node-aware status]

[Per-run log file setup]
    └──required by──> [CLI logs command]

[Intermediate artifact storage (per node)]
    └──enables──> [Result retrieval]
    └──enables──> [Structured artifact manifest]
    └──enables──> [Execution transcript capture]

[LangGraph stateful state TypedDict]
    └──required by──> [Per-node model attribution]
    └──required by──> [Node-aware status]
    └──required by──> [Structured artifact manifest]

[FastAPI startup event]
    └──required by──> [Startup resume scan]
    └──required by──> [Graceful SIGTERM handler registration]

[OpenHands SDK in-process]
    └──enables──> [Execution transcript capture]
    └──enables──> [OpenTelemetry tracing (Execute node)]
    └──required by──> [OpenHands SDK error propagation]

[Health endpoint]
    └──independent of all job features (pure liveness check)

[launchd plist]
    └──depends on──> [Health endpoint] (for monitoring)
    └──depends on──> [Graceful SIGTERM handler]
    └──depends on──> [Startup resume scan]
```

### Dependency Notes

- **Checkpointer must be established before any graph invocation:** SQLite checkpointer initialization is a startup concern, not per-job. Wire at `compile()` time.
- **Intermediate artifacts must be written before result retrieval:** The result endpoint returns a pointer to the artifact directory. Artifacts must exist before the job reaches DONE.
- **Startup resume scan depends on SIGTERM handler being correct:** If a prior run died mid-node without checkpointing, resume will re-run from the last between-node checkpoint. This is correct behavior; document it in operator notes.
- **Cancel and checkpointer interact with a known LangGraph limitation:** Cancellation mid-node does not guarantee the in-flight state is checkpointed. The status will be set to CANCELLED in the job store (SQLite), but the LangGraph thread checkpoint reflects the last completed node.

---

## MVP Definition

### Launch With (v1)

Minimum set to make the service genuinely useful and trustworthy for a single operator.

- [ ] Job submission (POST /jobs) — the entire flow starts here
- [ ] Job status polling (GET /jobs/{id}/status) with node-phase granularity — operator must know what is happening
- [ ] Result retrieval (GET /jobs/{id}/result) — the point of the service
- [ ] Cancel a run (DELETE /jobs/{id}) — long runs must be stoppable
- [ ] LangGraph SQLite checkpointer — run persistence across restarts (non-negotiable given launchd)
- [ ] Startup resume scan — orphaned jobs must auto-recover on process start
- [ ] Intermediate artifact storage (research.md, plan.md, execution_transcript.jsonl) — audit trail per run
- [ ] Per-run structured log file — debugging failed runs without log file is painful
- [ ] LiteLLM unavailable / timeout handling — nodes must fail gracefully, not hang forever
- [ ] OpenHands SDK error propagation — Execute node failures must be captured and surfaced
- [ ] Health endpoint (GET /health) — launchd and operator need liveness signal
- [ ] launchd plist (com.ohama.orchestrator) — core constraint; service is useless without auto-start
- [ ] Graceful SIGTERM handler — prevent mid-node state corruption on launchd restart
- [ ] CLI: submit / status / logs / result — terminal-first operator workflow
- [ ] LangGraph stateful TypedDict state (goal → research_notes → plan → execution_transcript) — typed handoff between nodes
- [ ] Per-node model attribution logged to state — minimum observability for debugging

### Add After Validation (v1.x)

Add once the linear pipeline is running reliably.

- [ ] Structured artifact manifest (manifest.json per job) — trigger: operator starts losing track of what a run produced
- [ ] OpenTelemetry tracing for Execute node — trigger: need to debug OpenHands iteration count or tool failure patterns
- [ ] `orchestrator status --watch` polling loop in CLI — trigger: operator finds manual re-running of status annoying during long runs
- [ ] Job list endpoint (GET /jobs) with pagination — trigger: operator wants to audit past runs

### Future Consideration (v2+)

Defer until the linear pipeline has proven itself and failure modes are understood.

- [ ] Plan approval gate (human-in-the-loop) — if autonomous runs consistently produce bad plans
- [ ] Parallel node execution within a run — if Research produces multiple sub-tasks worth executing in parallel
- [ ] Web UI — if CLI becomes genuinely painful for more than one operator
- [ ] Multiple concurrent jobs — if the Mac's memory/GPU situation improves or a second machine joins

---

## Feature Prioritization Matrix

| Feature | Operator Value | Implementation Cost | Priority |
|---------|----------------|---------------------|----------|
| Job submission API | HIGH | LOW | P1 |
| Job status polling (with phase) | HIGH | LOW | P1 |
| Result retrieval | HIGH | LOW | P1 |
| LangGraph SQLite checkpointer | HIGH | LOW | P1 |
| launchd plist | HIGH | LOW | P1 |
| Health endpoint | HIGH | LOW | P1 |
| CLI (submit/status/logs/result) | HIGH | LOW | P1 |
| Intermediate artifact storage | HIGH | LOW | P1 |
| LangGraph stateful TypedDict state | HIGH | MEDIUM | P1 |
| Graceful SIGTERM handler | HIGH | MEDIUM | P1 |
| Startup resume scan | HIGH | MEDIUM | P1 |
| Cancel a run | HIGH | MEDIUM | P1 |
| LiteLLM unavailable handling (retry + timeout) | HIGH | MEDIUM | P1 |
| OpenHands SDK error propagation | HIGH | MEDIUM | P1 |
| Per-run log file | HIGH | LOW | P1 |
| Per-node model attribution in state | MEDIUM | LOW | P1 |
| Node-aware status in /status response | MEDIUM | LOW | P1 |
| Structured artifact manifest | MEDIUM | LOW | P2 |
| OTEL tracing (Execute node) | MEDIUM | LOW | P2 |
| CLI --watch polling | MEDIUM | LOW | P2 |
| Job list endpoint | LOW | LOW | P2 |
| Plan approval gate | LOW | HIGH | P3 |
| Web UI | LOW | HIGH | P3 |
| Parallel job execution | LOW | HIGH | P3 |

**Priority key:**
- P1: Must have for launch — service is not functional or trustworthy without it
- P2: Should have — adds operator quality-of-life once core is working
- P3: Nice to have — deferred; revisit after v1 is proven

---

## Competitor Feature Analysis

This tool is not competing with a SaaS product. The reference comparisons are the patterns used
by analogous local autonomous agent tools.

| Feature | Naive script (subprocess chain) | OpenHands CLI standalone | This orchestrator |
|---------|--------------------------------|--------------------------|-------------------|
| Reboot survival | No | No | Yes (launchd + checkpointer) |
| State handoff between nodes | String files, manual | N/A | Typed LangGraph state object |
| Status visibility | None | Exit code only | Phase-aware REST + CLI |
| Intermediate artifacts | Manual | Conversation log | Structured per-node files |
| Cancel in flight | kill PID | Ctrl-C | DELETE /jobs/{id} |
| Resume after crash | None | Restart from beginning | Resume from last checkpoint node |
| Failure reason | Stderr dump | Exit message | Structured error in state + log |
| Model attribution | None | Single model | Per-node model + token counts |

---

## Sources

- LangGraph checkpointer persistence: [Persistence - Docs by LangChain](https://docs.langchain.com/oss/python/langgraph/persistence) — SQLite checkpointer, thread_id keying, resume semantics
- LangGraph error handling: [Fault Tolerance in LangGraph: Retries, Timeouts and Error Handlers](https://www.langchain.com/blog/fault-tolerance-in-langgraph) — RetryPolicy per node, error classification
- LangGraph cancellation limitation: [Run Cancellation Causes Loss of Streamed State Not Yet Persisted as a Checkpoint · Issue #5672](https://github.com/langchain-ai/langgraph/issues/5672) — mid-node cancel does not guarantee checkpoint persistence
- OpenHands SDK observability: [Observability & Tracing - OpenHands Docs](https://docs.openhands.dev/sdk/guides/observability) — OTEL automatic instrumentation, trace hierarchy
- OpenHands SDK introduction: [Introducing the OpenHands Software Agent SDK](https://openhands.dev/blog/introducing-the-openhands-software-agent-sdk) — ConversationState, typed events, serialization
- FastAPI long-running job patterns: [Serving Long-Running Jobs with FastAPI Using Webhooks and Task Polling](https://medium.com/@bhagyarana80/serving-long-running-jobs-with-fastapi-using-webhooks-and-task-polling-860bb0d3e0f9) — 202 Accepted + job_id + polling pattern
- launchd SIGTERM behavior: [A launchd Tutorial](https://www.launchd.info/) — ExitTimeOut, SIGTERM propagation to child processes
- Agent artifact management: [How to Manage AI Agent Artifacts - Complete Guide 2025](https://fast.io/resources/ai-agent-artifacts/) — naming conventions, intermediate vs final artifacts

---
*Feature research for: Local LangGraph + OpenHands multi-agent orchestration service*
*Researched: 2026-06-19*
