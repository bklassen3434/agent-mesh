"""LangGraph checkpointer selection (Phase 8).

One place decides where orchestration run state is persisted:

* ``LANGGRAPH_POSTGRES_URL`` set  → ``AsyncPostgresSaver`` (the docker
  ``langgraph-db`` container). This is the deployed path; checkpoints
  survive restarts and back the ``/status`` page.
* unset → ``InMemorySaver``. Keeps ``uv run`` and the test suite free of
  any Postgres dependency, matching the local-first ethos.

The coordinator + skeptic-sweep open this around a single graph
invocation; the saver lifecycle is scoped to the run.
"""
from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver

POSTGRES_URL_ENV = "LANGGRAPH_POSTGRES_URL"


def postgres_url() -> str | None:
    """The configured checkpoint Postgres URL, or None when unset/blank."""
    url = os.environ.get(POSTGRES_URL_ENV, "").strip()
    return url or None


def thread_config(thread_id: str) -> dict[str, Any]:
    """RunnableConfig selecting the checkpoint thread for a run.

    One thread per pipeline/sweep run (thread_id == run_id), so the
    checkpoint history of a thread maps to the history of a run.
    """
    return {"configurable": {"thread_id": thread_id}}


@asynccontextmanager
async def open_checkpointer() -> AsyncIterator[BaseCheckpointSaver[Any]]:
    """Yield a checkpointer for one graph run.

    Postgres when ``LANGGRAPH_POSTGRES_URL`` is set (tables created via
    ``setup()`` — idempotent), in-memory otherwise.
    """
    url = postgres_url()
    if url is None:
        from langgraph.checkpoint.memory import InMemorySaver

        yield InMemorySaver()
        return

    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    async with AsyncPostgresSaver.from_conn_string(url) as saver:
        await saver.setup()
        yield saver
