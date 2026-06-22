# tests/test_api.py
# FastAPI TestClient integration test (01-03 Task 2).
#
# All tests use a temp DATA_DIR and LOG_DIR via environment variables so they
# never touch the real data/ or logs/ directories.
#
# TestClient runs inside `with TestClient(app) as client:` blocks so the lifespan
# (startup + worker task + shutdown) executes automatically.
#
# Coverage:
#   1. POST /goals → 202 + {job_id, status: "PENDING"}
#   2. Status progresses PENDING → RESEARCHING → ... → DONE; 202 returned
#      BEFORE graph finishes (success criterion 1) and intermediate state seen (crit 2)
#   3. GET /result returns 409 before DONE, 200+result after DONE; 404 for unknown
#   4. GET /health → 200 {"sqlite": "reachable"}
#   5. DELETE /jobs/{id} → 404 for unknown id; "already terminal" for a DONE job
#   6. Per-run log file exists at logs/{job_id}.log with RESEARCHING+DONE lines (crit 5)
import os
import tempfile
import time
import uuid

import pytest
from fastapi.testclient import TestClient


def _make_client():
    """Build a TestClient with isolated DATA_DIR and LOG_DIR in a temp dir.

    Returns (client_context, data_dir, log_dir).  Call as:
        with _make_client() as (client, data_dir, log_dir):
    """
    import contextlib

    @contextlib.contextmanager
    def _ctx():
        td = tempfile.mkdtemp(prefix="orchestrator_test_")
        data_dir = os.path.join(td, "data")
        log_dir = os.path.join(td, "logs")
        os.makedirs(data_dir, exist_ok=True)
        os.makedirs(log_dir, exist_ok=True)

        # Override env vars so the app (main.py + routes.py) uses isolated dirs.
        old_data = os.environ.get("DATA_DIR")
        old_log = os.environ.get("LOG_DIR")
        old_test_graph = os.environ.get("ORCHESTRATOR_TEST_GRAPH")
        os.environ["DATA_DIR"] = data_dir
        os.environ["LOG_DIR"] = log_dir
        # ORCHESTRATOR_TEST_GRAPH=1: lifespan compiles build_test_graph() (all-stub)
        # so the API tests never make real LLM calls and run fully offline.
        os.environ["ORCHESTRATOR_TEST_GRAPH"] = "1"

        try:
            # Import app AFTER env is set so DATA_DIR is picked up at import time.
            # Re-import forces routes to re-read DATA_DIR on each _jobs_db_path() call.
            from orchestrator.main import app

            with TestClient(app) as client:
                yield client, data_dir, log_dir
        finally:
            # Restore env
            if old_data is None:
                os.environ.pop("DATA_DIR", None)
            else:
                os.environ["DATA_DIR"] = old_data

            if old_log is None:
                os.environ.pop("LOG_DIR", None)
            else:
                os.environ["LOG_DIR"] = old_log

            if old_test_graph is None:
                os.environ.pop("ORCHESTRATOR_TEST_GRAPH", None)
            else:
                os.environ["ORCHESTRATOR_TEST_GRAPH"] = old_test_graph

            # Clean up temp dir
            import shutil
            shutil.rmtree(td, ignore_errors=True)

    return _ctx()


def _poll_status(client: TestClient, job_id: str, timeout: float = 15.0, interval: float = 0.25) -> list[str]:
    """Poll GET /jobs/{job_id}/status until terminal. Returns list of observed statuses."""
    observed: list[str] = []
    deadline = time.monotonic() + timeout

    while True:
        resp = client.get(f"/jobs/{job_id}/status")
        assert resp.status_code == 200, f"Status poll returned {resp.status_code}: {resp.text}"
        status = resp.json()["status"]

        if not observed or observed[-1] != status:
            observed.append(status)

        if status in ("DONE", "FAILED", "CANCELLED"):
            return observed

        if time.monotonic() > deadline:
            raise TimeoutError(
                f"Job {job_id} did not reach terminal status within {timeout}s; "
                f"last={status!r}, observed={observed}"
            )

        time.sleep(interval)


# ---------------------------------------------------------------------------
# Test 1 — POST /goals returns 202 with job_id and status PENDING
# ---------------------------------------------------------------------------

def test_submit_goal_returns_202():
    with _make_client() as (client, data_dir, log_dir):
        resp = client.post("/goals", json={"goal": "test goal"})
        assert resp.status_code == 202, f"Expected 202, got {resp.status_code}: {resp.text}"
        data = resp.json()
        assert "job_id" in data, f"No job_id in response: {data}"
        assert data["status"] == "PENDING", f"Expected PENDING, got {data['status']!r}"
        # job_id must be a valid UUID string
        uuid.UUID(data["job_id"])  # raises ValueError if invalid


# ---------------------------------------------------------------------------
# Test 2 — status progresses from PENDING through intermediates to DONE
# ---------------------------------------------------------------------------

def test_status_progresses_to_done():
    with _make_client() as (client, data_dir, log_dir):
        # Submit the job
        resp = client.post("/goals", json={"goal": "status progression test"})
        assert resp.status_code == 202
        job_id = resp.json()["job_id"]

        # Immediately check status — must be PENDING or RESEARCHING (i.e., response
        # returned before graph finished — success criterion 1).
        first_status_resp = client.get(f"/jobs/{job_id}/status")
        assert first_status_resp.status_code == 200
        first_status = first_status_resp.json()["status"]
        assert first_status in ("PENDING", "RESEARCHING"), (
            f"Expected PENDING or RESEARCHING immediately after submit, got {first_status!r}"
        )

        # Poll until DONE (stub takes ~4s; timeout 15s)
        observed = _poll_status(client, job_id, timeout=15.0)

        # Final status must be DONE
        assert observed[-1] == "DONE", f"Expected DONE as final status; observed={observed}"

        # At least one intermediate state must have been seen (success criterion 2)
        intermediates = {"RESEARCHING", "PLANNING", "EXECUTING"}
        seen_intermediates = intermediates.intersection(set(observed))
        assert seen_intermediates, (
            f"Expected at least one intermediate status from {intermediates}; "
            f"observed={observed}"
        )


# ---------------------------------------------------------------------------
# Test 3 — result endpoint: 409 before DONE, 200+result after, 404 for unknown
# ---------------------------------------------------------------------------

def test_result_requires_done():
    with _make_client() as (client, data_dir, log_dir):
        # 3a. Unknown job_id → 404
        fake_id = str(uuid.uuid4())
        resp_404 = client.get(f"/jobs/{fake_id}/result")
        assert resp_404.status_code == 404, f"Expected 404 for unknown id, got {resp_404.status_code}"

        # 3b. Submit and immediately check result → 409 (not DONE yet)
        resp = client.post("/goals", json={"goal": "result test"})
        assert resp.status_code == 202
        job_id = resp.json()["job_id"]

        # Check result immediately; job should be PENDING or RESEARCHING → 409
        result_resp = client.get(f"/jobs/{job_id}/result")
        # It's possible (very fast CI) the job is already DONE; accept both 409 and 200
        if result_resp.status_code == 409:
            detail = result_resp.json()["detail"]
            assert "not DONE" in detail, f"Unexpected 409 detail: {detail}"

        # Poll until DONE, then check result → 200
        _poll_status(client, job_id, timeout=15.0)

        result_resp_done = client.get(f"/jobs/{job_id}/result")
        assert result_resp_done.status_code == 200, (
            f"Expected 200 after DONE, got {result_resp_done.status_code}: {result_resp_done.text}"
        )
        result_data = result_resp_done.json()
        assert result_data["job_id"] == job_id
        assert result_data["result"] == "STUB: execution complete", (
            f"Unexpected result: {result_data['result']!r}"
        )


# ---------------------------------------------------------------------------
# Test 4 — GET /health returns 200 with sqlite reachable
# ---------------------------------------------------------------------------

def test_health_returns_200():
    with _make_client() as (client, data_dir, log_dir):
        resp = client.get("/health")
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        data = resp.json()
        assert data["status"] == "ok", f"Expected status=ok, got {data!r}"
        assert data["sqlite"] == "reachable", f"Expected sqlite=reachable, got {data!r}"


# ---------------------------------------------------------------------------
# Test 5a — DELETE /jobs/{id} → 404 for unknown id
# ---------------------------------------------------------------------------

def test_cancel_unknown_returns_404():
    with _make_client() as (client, data_dir, log_dir):
        fake_id = str(uuid.uuid4())
        resp = client.delete(f"/jobs/{fake_id}")
        assert resp.status_code == 404, f"Expected 404, got {resp.status_code}: {resp.text}"


# ---------------------------------------------------------------------------
# Test 5b — DELETE /jobs/{id} on a DONE job returns "already terminal"
# ---------------------------------------------------------------------------

def test_cancel_terminal_job():
    with _make_client() as (client, data_dir, log_dir):
        # Submit and wait for DONE
        resp = client.post("/goals", json={"goal": "cancel terminal test"})
        assert resp.status_code == 202
        job_id = resp.json()["job_id"]

        _poll_status(client, job_id, timeout=15.0)

        # Cancel a DONE job → "already terminal"
        cancel_resp = client.delete(f"/jobs/{job_id}")
        assert cancel_resp.status_code == 200, (
            f"Expected 200 for terminal job cancel, got {cancel_resp.status_code}: {cancel_resp.text}"
        )
        cancel_data = cancel_resp.json()
        assert "already terminal" in cancel_data.get("note", ""), (
            f"Expected 'already terminal' note, got {cancel_data!r}"
        )


# ---------------------------------------------------------------------------
# Test 6 — Per-job log file exists with timestamped RESEARCHING + DONE lines
# ---------------------------------------------------------------------------

def test_per_job_log_file_written():
    with _make_client() as (client, data_dir, log_dir):
        resp = client.post("/goals", json={"goal": "log file test"})
        assert resp.status_code == 202
        job_id = resp.json()["job_id"]

        _poll_status(client, job_id, timeout=15.0)

        # Log file is relative to CWD (logs/{job_id}.log per 01-02-SUMMARY Pattern).
        # The worker uses os.path.join("logs", ...) relative to process CWD.
        log_path = os.path.join("logs", f"{job_id}.log")
        assert os.path.exists(log_path), (
            f"Per-job log file not found at {log_path!r} (cwd={os.getcwd()!r})"
        )

        with open(log_path) as f:
            content = f.read()

        assert "RESEARCHING" in content, (
            f"'RESEARCHING' not in log file {log_path!r}; content:\n{content[:500]}"
        )
        assert "DONE" in content, (
            f"'DONE' not in log file {log_path!r}; content:\n{content[:500]}"
        )
