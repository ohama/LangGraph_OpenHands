# orchestrator/persistence/job_store.py
# aiosqlite CRUD for jobs.db (app-owned registry).
# PERSIST-02: WAL mode enabled on every connection.
# jobs.db is NEVER mixed with checkpoints.db (LangGraph-owned).
import aiosqlite
from datetime import datetime, timezone


async def init_jobs_db(db_path: str) -> None:
    """Create jobs.db schema on first run. Idempotent (IF NOT EXISTS).

    Sets PRAGMA journal_mode=WAL so concurrent HTTP readers and the single
    worker writer don't contend for the write lock.
    """
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                id          TEXT PRIMARY KEY,
                goal        TEXT NOT NULL,
                status      TEXT NOT NULL DEFAULT 'PENDING',
                result      TEXT,
                error       TEXT,
                created_at  TEXT NOT NULL,
                updated_at  TEXT NOT NULL
            )
        """)
        await conn.commit()


class JobStore:
    """aiosqlite CRUD for the jobs table.

    Opens a new connection per operation. Simpler than a connection pool and
    safe for a single-writer (worker) + multiple-readers (HTTP handlers) pattern.
    WAL mode is enabled on EVERY connection open.
    """

    def __init__(self, db_path: str):
        self.db_path = db_path

    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    async def _connect(self) -> aiosqlite.Connection:
        conn = await aiosqlite.connect(self.db_path)
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA synchronous=NORMAL")  # faster than FULL; safe with WAL
        return conn

    async def create_job(self, job_id: str, goal: str) -> None:
        now = self._now()
        async with await self._connect() as conn:
            await conn.execute(
                "INSERT INTO jobs (id, goal, status, created_at, updated_at) VALUES (?,?,?,?,?)",
                (job_id, goal, "PENDING", now, now),
            )
            await conn.commit()

    async def set_status(self, job_id: str, status: str) -> None:
        async with await self._connect() as conn:
            await conn.execute(
                "UPDATE jobs SET status=?, updated_at=? WHERE id=?",
                (status, self._now(), job_id),
            )
            await conn.commit()

    async def set_complete(self, job_id: str, result: str | None) -> None:
        async with await self._connect() as conn:
            await conn.execute(
                "UPDATE jobs SET status='DONE', result=?, updated_at=? WHERE id=?",
                (result, self._now(), job_id),
            )
            await conn.commit()

    async def set_failed(self, job_id: str, error: str) -> None:
        async with await self._connect() as conn:
            await conn.execute(
                "UPDATE jobs SET status='FAILED', error=?, updated_at=? WHERE id=?",
                (error, self._now(), job_id),
            )
            await conn.commit()

    async def get_job(self, job_id: str) -> dict | None:
        async with await self._connect() as conn:
            conn.row_factory = aiosqlite.Row
            async with conn.execute(
                "SELECT * FROM jobs WHERE id=?", (job_id,)
            ) as cur:
                row = await cur.fetchone()
                return dict(row) if row else None

    async def get_jobs_by_status(self, status: str) -> list[dict]:
        async with await self._connect() as conn:
            conn.row_factory = aiosqlite.Row
            async with conn.execute(
                "SELECT * FROM jobs WHERE status=?", (status,)
            ) as cur:
                return [dict(r) for r in await cur.fetchall()]
