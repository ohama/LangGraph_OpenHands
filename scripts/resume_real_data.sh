#!/usr/bin/env bash
# scripts/resume_real_data.sh
# Phase 2 criterion 4 proof: kill-after-research resume with node_models attribution.
#
# Flow:
#   1. Cleanup guard + EXIT trap (no orphan uvicorn left behind).
#   2. Precheck LiteLLM :4000 health.
#   3. Start service via .venv/bin/uvicorn (STATE.md decision 01-03).
#   4. Submit goal; capture job_id.
#   5. Poll until PLANNING (research complete; worker sets PLANNING after research chunk).
#   6. STOP service (pkill -9) + confirm :8000/process free (B4 single-writer rule:
#      release checkpoints.db lock before inline Python opens its own connection).
#   7. Resume inline via astream(None, config) under timeout 600 guard.
#   8. Assert: research NOT re-called, node_models=={research:qwen-122b,plan:qwen-122b},
#      plan non-empty.
#   9. EXIT trap confirms no orphan uvicorn.
#
# CRITICAL CONSTRAINTS (STATE.md + plan):
#   - Launch: .venv/bin/uvicorn (never uv run uvicorn — orphans the process).
#   - Kill:   pkill -9 -f "uvicorn orchestrator.main:app" (kills the real process).
#   - B4:     Service MUST be stopped and :8000 confirmed free BEFORE inline resume.
#   - Timeout: Resume runs under `timeout 600` hard guard (gtimeout if timeout absent).
#   - No DB restart: do NOT restart service before the resume (checkpoint is the authority).
#
# Expected runtime: ~3-5 min (research call ~60-120s + plan call ~30-90s).
# Do not interrupt — these are real qwen-122b generations.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
VENV_PYTHON="$PROJECT_DIR/.venv/bin/python"
VENV_UVICORN="$PROJECT_DIR/.venv/bin/uvicorn"
# NOTE: Port 8080 used here because :8000 is occupied by the mlx_lm.server qwen-35b
# model server in this dev environment. The orchestrator runs on :8080 for this proof.
# Production deployments (Phase 5 launchd) will resolve port conflict before using :8000.
PORT=8080
SERVER_LOG="/tmp/orch_resume.log"

echo "=== resume_real_data.sh — Phase 2 criterion 4 proof ==="
echo "Project: $PROJECT_DIR"
echo "Port:    $PORT"
echo ""

# ---------------------------------------------------------------------------
# Step 1: Cleanup guard — kill any existing uvicorn on :8000 before we start.
# EXIT trap ensures we clean up on any exit (success, failure, or error).
# ---------------------------------------------------------------------------
_cleanup() {
    echo ""
    echo "[cleanup] EXIT trap: killing any uvicorn orchestrator.main:app processes..."
    pkill -9 -f "uvicorn orchestrator.main:app" 2>/dev/null || true
    # Confirm :8000 is free
    local wait=0
    while lsof -ti tcp:${PORT} 2>/dev/null | grep -q .; do
        sleep 1
        wait=$((wait + 1))
        if [ $wait -ge 10 ]; then
            echo "[cleanup] WARNING: port :${PORT} still in use after 10s"
            break
        fi
    done
    if ! lsof -ti tcp:${PORT} 2>/dev/null | grep -q .; then
        echo "[cleanup] :${PORT} is free — no orphan uvicorn."
    fi
}
trap _cleanup EXIT

# Initial cleanup: kill any stale uvicorn from a previous run
echo "[step 1] Killing any stale uvicorn on :${PORT}..."
pkill -9 -f "uvicorn orchestrator.main:app" 2>/dev/null || true
sleep 1
if lsof -ti tcp:${PORT} 2>/dev/null | grep -q .; then
    echo "[step 1] WARNING: port :${PORT} still occupied after initial pkill; waiting..."
    sleep 3
fi
echo "[step 1] Port :${PORT} clear — proceeding."

# ---------------------------------------------------------------------------
# Step 2: Precheck LiteLLM :4000
# ---------------------------------------------------------------------------
echo ""
echo "[step 2] Checking LiteLLM :4000 health..."
if ! curl -sf http://127.0.0.1:4000/health > /dev/null 2>&1; then
    echo "ERROR: LiteLLM :4000 is down. Cannot run live resume proof."
    echo "       Start it: launchctl start gui/501/com.ohama.litellm"
    exit 2
fi
echo "[step 2] LiteLLM :4000 is UP"

# ---------------------------------------------------------------------------
# Step 3: Start the service via the approved binary (STATE.md decision 01-03).
# NEVER use `uv run uvicorn` — uv spawns uvicorn as a child; killing the
# wrapper orphans the real server on the port.
# ---------------------------------------------------------------------------
echo ""
echo "[step 3] Starting orchestrator service via .venv/bin/uvicorn..."
"$VENV_UVICORN" orchestrator.main:app --port ${PORT} > "$SERVER_LOG" 2>&1 &
SERVER_PID=$!
echo "[step 3] Server PID: $SERVER_PID"

# Poll /health until 200 (timeout 30s)
ELAPSED=0
until curl -sf http://127.0.0.1:${PORT}/health > /dev/null 2>&1; do
    sleep 1
    ELAPSED=$((ELAPSED + 1))
    if [ $ELAPSED -ge 30 ]; then
        echo "[step 3] FAIL: orchestrator /health did not return 200 within 30s"
        echo "[step 3] Server log:"
        cat "$SERVER_LOG" || true
        exit 3
    fi
done
echo "[step 3] Orchestrator :${PORT} is UP (${ELAPSED}s)"

# ---------------------------------------------------------------------------
# Step 4: Submit a goal and capture job_id.
# ---------------------------------------------------------------------------
echo ""
echo "[step 4] Submitting goal to :${PORT}/goals..."
SUBMIT_RESP=$(curl -s -X POST "http://127.0.0.1:${PORT}/goals" \
    -H "Content-Type: application/json" \
    -d '{"goal":"build a small CLI todo app in Python"}')
echo "[step 4] Response: $SUBMIT_RESP"

JOB_ID=$(echo "$SUBMIT_RESP" | "$VENV_PYTHON" -c "
import sys, json
d = json.load(sys.stdin)
print(d['job_id'])
" 2>/dev/null || echo "")

if [ -z "$JOB_ID" ]; then
    echo "[step 4] FAIL: Could not extract job_id from response: $SUBMIT_RESP"
    exit 4
fi
echo "[step 4] job_id: $JOB_ID"

# ---------------------------------------------------------------------------
# Step 5: Poll until PLANNING (research_node completed — worker sets PLANNING
# after research chunk). Allow up to 180s for research at 122B speed (60-120s).
# If we reach EXECUTING/DONE first, research still completed — accept it.
# ---------------------------------------------------------------------------
echo ""
echo "[step 5] Polling job status until PLANNING (research complete)..."
echo "         NOTE: research_node calls qwen-122b — may take 60-120s."
MAX_POLL=180
POLL_ELAPSED=0
LAST_STATUS=""
RESEARCH_DONE=false

while [ $POLL_ELAPSED -lt $MAX_POLL ]; do
    STATUS=$(curl -s "http://127.0.0.1:${PORT}/jobs/${JOB_ID}/status" \
        | "$VENV_PYTHON" -c "
import sys, json
d = json.load(sys.stdin)
print(d.get('status', ''))
" 2>/dev/null || echo "")

    if [ "$STATUS" != "$LAST_STATUS" ]; then
        echo "[step 5] Status transition: $STATUS (${POLL_ELAPSED}s)"
        LAST_STATUS="$STATUS"
    fi

    if [ "$STATUS" = "PLANNING" ]; then
        RESEARCH_DONE=true
        echo "[step 5] PLANNING reached — research_node completed!"
        break
    fi

    # If it advances past PLANNING (EXECUTING or DONE), research is complete too
    if [ "$STATUS" = "EXECUTING" ] || [ "$STATUS" = "DONE" ]; then
        RESEARCH_DONE=true
        echo "[step 5] Status is $STATUS — research completed before we caught PLANNING."
        echo "         This is acceptable: research_node ran and its checkpoint is saved."
        break
    fi

    if [ "$STATUS" = "FAILED" ] || [ "$STATUS" = "CANCELLED" ]; then
        echo "[step 5] FAIL: Job reached terminal status $STATUS before PLANNING."
        echo "[step 5] Server log tail:"
        tail -20 "$SERVER_LOG" || true
        exit 5
    fi

    sleep 2
    POLL_ELAPSED=$((POLL_ELAPSED + 2))
done

if [ "$RESEARCH_DONE" = "false" ]; then
    echo "[step 5] FAIL: Did not reach PLANNING within ${MAX_POLL}s. Last status: $LAST_STATUS"
    echo "[step 5] Server log tail:"
    tail -20 "$SERVER_LOG" || true
    exit 5
fi

# ---------------------------------------------------------------------------
# Step 6: STOP the service to release the checkpoints.db connection.
# B4 (single-writer rule): the inline resume MUST NOT open AsyncSqliteSaver
# while the service still holds a connection — SQLite will lock/hang.
# Confirm both the process is gone AND :8000 is free before continuing.
# ---------------------------------------------------------------------------
echo ""
echo "[step 6] Stopping service to release checkpoints.db lock (B4 single-writer rule)..."
pkill -9 -f "uvicorn orchestrator.main:app" 2>/dev/null || true

# Wait until BOTH conditions are met: process gone AND :8000 free
STOP_WAIT=0
STOP_MAX=15
while true; do
    PROC_GONE=true
    PORT_FREE=true

    if pgrep -f "uvicorn orchestrator.main:app" > /dev/null 2>&1; then
        PROC_GONE=false
    fi

    if lsof -ti tcp:${PORT} 2>/dev/null | grep -q .; then  # PORT=8080 (dev env; :8000 has qwen-35b)
        PORT_FREE=false
    fi

    if [ "$PROC_GONE" = "true" ] && [ "$PORT_FREE" = "true" ]; then
        echo "[step 6] Service stopped: process gone AND :${PORT} is free (${STOP_WAIT}s)"
        break
    fi

    sleep 1
    STOP_WAIT=$((STOP_WAIT + 1))
    if [ $STOP_WAIT -ge $STOP_MAX ]; then
        echo "[step 6] FAIL: Service did not stop within ${STOP_MAX}s."
        echo "         proc_gone=$PROC_GONE, port_free=$PORT_FREE"
        echo "         Orphaned process:"
        pgrep -af "uvicorn orchestrator.main:app" 2>/dev/null || true
        exit 4
    fi
done

echo "[step 6] checkpoints.db is now safe to open as sole writer."

# ---------------------------------------------------------------------------
# Step 7: Resume directly against the checkpoint (service DOWN, no db lock).
# Run under `timeout 600` hard guard so a wedged 122B cannot hang the script.
# timeout is available at /opt/homebrew/bin (verified in live_stack_state).
# The resume calls astream(None, config) — LangGraph skips research and runs
# plan_node (and execute_stub) only, reading research_findings from checkpoint.
# ---------------------------------------------------------------------------
echo ""
echo "[step 7] Resuming graph from checkpoint (service DOWN; sole writer)..."
echo "         NOTE: plan_node calls qwen-122b — may take 30-90s."
echo "         Hard timeout: 600s (prevents hang on wedged 122B)."
echo ""

RESUME_RESULT=$(timeout 600 "$VENV_PYTHON" -c "
import asyncio
import sys
import os
import json

# Add project root to path so orchestrator imports work
sys.path.insert(0, '$PROJECT_DIR')
# Load .env so LITELLM_BASE_URL / PLAN_MODEL env vars are available
from dotenv import load_dotenv
load_dotenv('$PROJECT_DIR/.env')

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from orchestrator.graph.stub_graph import build_stub_graph

JOB_ID = '$JOB_ID'
DB_PATH = '$PROJECT_DIR/data/checkpoints.db'

async def resume():
    config = {'configurable': {'thread_id': JOB_ID}}

    nodes_run = []
    async with AsyncSqliteSaver.from_conn_string(DB_PATH) as saver:
        graph = build_stub_graph().compile(checkpointer=saver)

        # Capture mid-state BEFORE resuming to count event_log entries
        # (to verify research was not re-called)
        pre_state = await graph.aget_state(config)
        pre_event_log = pre_state.values.get('event_log', [])
        pre_research_entries = [e for e in pre_event_log if 'research_node' in e]

        print(f'[resume] Pre-resume event_log ({len(pre_event_log)} entries): {pre_event_log}', flush=True)
        print(f'[resume] Pre-resume node_models: {pre_state.values.get(\"node_models\", {})}', flush=True)
        print(f'[resume] Pre-resume state.next: {pre_state.next}', flush=True)

        # Resume: astream(None, config) — skips completed research, runs plan+execute
        async for chunk in graph.astream(None, config=config, stream_mode='updates'):
            for node_name in chunk:
                nodes_run.append(node_name)
                print(f'[resume] Node completed: {node_name}', flush=True)

        # Fetch final state after resume
        final_state = await graph.aget_state(config)

    final_values = final_state.values
    node_models = final_values.get('node_models', {})
    event_log = final_values.get('event_log', [])
    plan = final_values.get('plan', '')

    # Count research entries after resume — must equal pre-resume count
    post_research_entries = [e for e in event_log if 'research_node' in e]

    print('', flush=True)
    print(f'[resume] nodes_run during resume: {nodes_run}', flush=True)
    print(f'[resume] Post-resume node_models: {node_models}', flush=True)
    print(f'[resume] Post-resume event_log ({len(event_log)} entries):', flush=True)
    for entry in event_log:
        print(f'  {entry}', flush=True)
    print(f'[resume] plan length: {len(plan)} chars', flush=True)
    print(f'[resume] plan (first 200 chars): {plan[:200]}', flush=True)

    # --- Write assertions to file (key=value, no quoting issues) ---
    # The shell reads this file in step 8 to parse results without JSON escaping.
    ASSERT_FILE = f'/tmp/resume_assertions_{JOB_ID}.txt'
    research_not_recalled = len(pre_research_entries) == len(post_research_entries)
    plan_ran = 'plan' in nodes_run
    research_not_in_resume = 'research' not in nodes_run
    with open(ASSERT_FILE, 'w') as af:
        af.write(f'research_not_recalled={research_not_recalled}\n')
        af.write(f'research_not_in_resume={research_not_in_resume}\n')
        af.write(f'plan_ran={plan_ran}\n')
        af.write(f'research_model={node_models.get(\"research\", \"\")}\n')
        af.write(f'plan_model={node_models.get(\"plan\", \"\")}\n')
        af.write(f'plan_len={len(plan)}\n')
    print(f'[resume] Assertions written to {ASSERT_FILE}', flush=True)

    return {
        'research_not_recalled': research_not_recalled,
        'plan_ran': plan_ran,
        'research_not_in_resume': research_not_in_resume,
    }

result = asyncio.run(resume())
print(f'[resume] Result: {result}')
" 2>&1) || {
    EXIT_CODE=$?
    if [ $EXIT_CODE -eq 124 ]; then
        echo "[step 7] FAIL: Resume timed out after 600s. qwen-122b may have wedged."
        echo "         Recovery: launchctl kickstart -k gui/501/com.ohama.qwen122b"
        exit 5
    else
        echo "[step 7] FAIL: Resume Python script exited with code $EXIT_CODE"
        echo "Output so far:"
        echo "$RESUME_RESULT"
        exit 5
    fi
}

echo "$RESUME_RESULT"

# ---------------------------------------------------------------------------
# Step 8: Assert results from inline Python output.
# Parse the ASSERTIONS file written by the Python script (key=value format,
# no special chars to escape — avoids shell quoting issues with JSON plan text).
# ---------------------------------------------------------------------------
echo ""
echo "[step 8] Asserting resume results..."

# The inline Python writes assertions to ASSERT_FILE (see step 7 above)
ASSERT_FILE="/tmp/resume_assertions_${JOB_ID}.txt"

if [ ! -f "$ASSERT_FILE" ]; then
    echo "FAIL: Assertions file not found at $ASSERT_FILE"
    echo "      The inline Python script may have failed before writing assertions."
    RESEARCH_NOT_RECALLED="False"
    RESEARCH_NOT_IN_RESUME="False"
    PLAN_RAN="False"
    RESEARCH_MODEL=""
    PLAN_MODEL=""
    PLAN_LEN="0"
else
    # Read key=value pairs (one per line, no quoting issues)
    RESEARCH_NOT_RECALLED=$(grep '^research_not_recalled=' "$ASSERT_FILE" | cut -d= -f2 || echo "False")
    RESEARCH_NOT_IN_RESUME=$(grep '^research_not_in_resume=' "$ASSERT_FILE" | cut -d= -f2 || echo "False")
    PLAN_RAN=$(grep '^plan_ran=' "$ASSERT_FILE" | cut -d= -f2 || echo "False")
    RESEARCH_MODEL=$(grep '^research_model=' "$ASSERT_FILE" | cut -d= -f2 || echo "")
    PLAN_MODEL=$(grep '^plan_model=' "$ASSERT_FILE" | cut -d= -f2 || echo "")
    PLAN_LEN=$(grep '^plan_len=' "$ASSERT_FILE" | cut -d= -f2 || echo "0")
    rm -f "$ASSERT_FILE"
fi

PASS=true

echo ""
echo "[assert] research_not_recalled : $RESEARCH_NOT_RECALLED"
echo "[assert] research_not_in_resume: $RESEARCH_NOT_IN_RESUME"
echo "[assert] plan_ran               : $PLAN_RAN"
echo "[assert] node_models.research   : $RESEARCH_MODEL"
echo "[assert] node_models.plan       : $PLAN_MODEL"
echo "[assert] plan length (chars)    : $PLAN_LEN"
echo ""

if [ "$RESEARCH_NOT_RECALLED" != "True" ]; then
    echo "FAIL: research_node was re-called during resume (event_log research count increased)"
    PASS=false
fi

if [ "$RESEARCH_NOT_IN_RESUME" != "True" ]; then
    echo "FAIL: 'research' node appeared in nodes_run during resume"
    PASS=false
fi

if [ "$PLAN_RAN" != "True" ]; then
    echo "FAIL: 'plan' node did NOT run during resume"
    PASS=false
fi

if [ "$RESEARCH_MODEL" != "qwen-122b" ]; then
    echo "FAIL: node_models['research'] is '$RESEARCH_MODEL', expected 'qwen-122b'"
    PASS=false
fi

if [ "$PLAN_MODEL" != "qwen-122b" ]; then
    echo "FAIL: node_models['plan'] is '$PLAN_MODEL', expected 'qwen-122b'"
    PASS=false
fi

if [ "$PLAN_LEN" -eq 0 ] 2>/dev/null; then
    echo "FAIL: plan is empty"
    PASS=false
fi

echo ""
echo "NOTE: GET /jobs/$JOB_ID/result is NOT fetched — service is intentionally down."
echo "      Job row in jobs.db may still show PLANNING (resume ran out-of-band)."
echo "      The checkpoint state is the source of truth for this proof."
echo ""

# ---------------------------------------------------------------------------
# Step 9: Final status — the EXIT trap will clean up uvicorn (should be none).
# ---------------------------------------------------------------------------
if [ "$PASS" = "true" ]; then
    echo "======================================================="
    echo "PASS: resume_real_data.sh — criterion 4 PROVEN"
    echo "======================================================="
    echo ""
    echo "Summary:"
    echo "  - research NOT re-called on resume (research_not_recalled=True)"
    echo "  - 'research' node did NOT appear in resume node list"
    echo "  - 'plan' node ran on resume"
    echo "  - node_models = {research: $RESEARCH_MODEL, plan: $PLAN_MODEL}"
    echo "  - plan is non-empty ($PLAN_LEN chars)"
    echo "  - Service was stopped and :${PORT} freed BEFORE inline resume (B4 rule)"
    echo "  - Resume ran under timeout 600 guard (no hang risk)"
    echo "  - No orphaned uvicorn (EXIT trap confirms)"
    exit 0
else
    echo "======================================================="
    echo "FAIL: resume_real_data.sh — one or more assertions failed"
    echo "======================================================="
    exit 1
fi
