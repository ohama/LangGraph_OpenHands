# tests/test_job_store.py
# Tests for JobStore aiosqlite CRUD (PERSIST-02).
# Uses tempfile-backed DB paths so tests are fully isolated.
import os
import tempfile

import pytest

from orchestrator.persistence.job_store import init_jobs_db, JobStore


@pytest.fixture
async def job_store():
    """Provide a fresh temp-file-backed JobStore for each test."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    await init_jobs_db(db_path)
    store = JobStore(db_path)
    yield store
    os.unlink(db_path)


async def test_create_job_returns_pending(job_store):
    await job_store.create_job("job-001", "test goal")
    row = await job_store.get_job("job-001")
    assert row is not None
    assert row["id"] == "job-001"
    assert row["goal"] == "test goal"
    assert row["status"] == "PENDING"
    assert row["result"] is None
    assert row["error"] is None


async def test_set_status_reflects_new_status(job_store):
    await job_store.create_job("job-002", "another goal")
    await job_store.set_status("job-002", "RESEARCHING")
    row = await job_store.get_job("job-002")
    assert row["status"] == "RESEARCHING"


async def test_set_complete_writes_result_and_done(job_store):
    await job_store.create_job("job-003", "complete goal")
    await job_store.set_complete("job-003", "execution output here")
    row = await job_store.get_job("job-003")
    assert row["status"] == "DONE"
    assert row["result"] == "execution output here"


async def test_set_failed_writes_error_and_failed(job_store):
    await job_store.create_job("job-004", "failing goal")
    await job_store.set_failed("job-004", "something went wrong")
    row = await job_store.get_job("job-004")
    assert row["status"] == "FAILED"
    assert row["error"] == "something went wrong"


async def test_get_jobs_by_status_returns_matching_rows(job_store):
    await job_store.create_job("job-005", "goal A")
    await job_store.create_job("job-006", "goal B")
    await job_store.create_job("job-007", "goal C")
    await job_store.set_status("job-005", "RESEARCHING")
    await job_store.set_status("job-006", "RESEARCHING")
    # job-007 remains PENDING

    researching = await job_store.get_jobs_by_status("RESEARCHING")
    pending = await job_store.get_jobs_by_status("PENDING")

    assert len(researching) == 2
    assert {r["id"] for r in researching} == {"job-005", "job-006"}
    assert len(pending) == 1
    assert pending[0]["id"] == "job-007"


async def test_get_job_returns_none_for_missing(job_store):
    row = await job_store.get_job("nonexistent-id")
    assert row is None


async def test_wal_mode_is_active(job_store):
    """Confirm WAL journal mode is set — required by PERSIST-02."""
    import aiosqlite
    async with aiosqlite.connect(job_store.db_path) as conn:
        async with conn.execute("PRAGMA journal_mode") as cur:
            row = await cur.fetchone()
            assert row[0] == "wal", f"Expected WAL mode, got: {row[0]}"
