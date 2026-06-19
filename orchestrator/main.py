# orchestrator/main.py
# FastAPI application entry point.
# Lifespan context manager owns the AsyncSqliteSaver checkpointer, compiled graph,
# asyncio.Queue, and single worker task for the full lifetime of the service.
#
# CRITICAL — RESEARCH Pitfall 1 (lifespan control-transfer placement):
#   The control-transfer statement MUST be inside the AsyncSqliteSaver.from_conn_string
#   context manager block.  Placing it outside closes the aiosqlite connection before
#   any request is served, causing every graph.astream() call to fail with:
#   "aiosqlite.ProgrammingError: Cannot operate on a closed database"
#
# Do NOT use FastAPI BackgroundTasks for graph execution. The asyncio.Queue worker
# is the ONLY execution path (see RESEARCH.md Pattern 4, Alternatives Considered).
import asyncio
import os
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from orchestrator.graph.stub_graph import build_stub_graph
from orchestrator.persistence.job_store import init_jobs_db
from orchestrator.worker.runner import worker_loop

load_dotenv()

DATA_DIR: str = os.environ.get("DATA_DIR", "data")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI lifespan: open checkpointer → compile graph → start worker → serve.

    app.state keys available while serving:
      - app.state.graph     : compiled LangGraph graph (checkpointer baked in)
      - app.state.job_queue : asyncio.Queue of (job_id, goal) tuples

    Control transfer to FastAPI is placed INSIDE the `async with` block (RESEARCH
    Pitfall 1 — see module docstring). Exiting the `async with` block on shutdown
    closes the aiosqlite connection cleanly.
    """
    # 1. Ensure jobs.db schema exists before anything reads or writes it.
    os.makedirs(DATA_DIR, exist_ok=True)
    await init_jobs_db(f"{DATA_DIR}/jobs.db")

    # 2. Open AsyncSqliteSaver.  from_conn_string() is an @asynccontextmanager —
    #    the aiosqlite connection lives for the duration of this `async with` block.
    #    Control MUST be transferred (via the keyword below) inside this block.
    async with AsyncSqliteSaver.from_conn_string(
        f"{DATA_DIR}/checkpoints.db"
    ) as saver:
        # Explicit setup call — setup() is auto-called on first op via is_setup guard,
        # but calling it here ensures the checkpoint schema exists before any graph run.
        await saver.setup()

        # 3. Compile graph once; checkpointer is baked in and reused across all jobs.
        graph = build_stub_graph().compile(checkpointer=saver)
        app.state.graph = graph

        # 4. Create a single shared asyncio.Queue and start one worker coroutine.
        #    The worker is the only code that calls graph.astream(). HTTP handlers only
        #    enqueue and read — they never invoke the graph directly (RESEARCH Pattern 4).
        job_queue: asyncio.Queue = asyncio.Queue()
        app.state.job_queue = job_queue

        worker_task = asyncio.create_task(
            worker_loop(job_queue, graph, f"{DATA_DIR}/jobs.db")
        )

        # ---- SERVE: control transferred to FastAPI (INSIDE async with — Pitfall 1) ----
        yield  # noqa: E501  # MUST be inside `async with AsyncSqliteSaver` block
        # ---- SHUTDOWN PATH (reached when service stops) ----

        # 5. Cancel the worker coroutine and wait for clean exit.
        worker_task.cancel()
        try:
            await worker_task
        except asyncio.CancelledError:
            pass
    # The `async with AsyncSqliteSaver.from_conn_string(...)` block exits here,
    # closing the aiosqlite connection cleanly after the worker has stopped.


# FastAPI application instance.
# Routes are NOT registered here — 01-03 wires them via:
#   from orchestrator.api.routes import router
#   app.include_router(router)    # <-- TODO(01-03): add routes here
app = FastAPI(lifespan=lifespan)
