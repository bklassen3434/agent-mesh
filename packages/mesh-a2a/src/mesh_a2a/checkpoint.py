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
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver

POSTGRES_URL_ENV = "LANGGRAPH_POSTGRES_URL"


def postgres_url() -> str | None:
    """The configured checkpoint Postgres URL, or None when unset/blank."""
    url = os.environ.get(POSTGRES_URL_ENV, "").strip()
    return url or None


def thread_config(thread_id: str) -> RunnableConfig:
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


# ── status-page read side ────────────────────────────────────────────────────


@dataclass
class RunCheckpointState:
    """Latest checkpoint state for one orchestration run (one thread).

    Read by the /status page to surface in-flight runs, interrupted runs,
    and the errors each run accumulated — replacing the old agent_tasks
    table.
    """

    run_id: str
    run_type: str  # "pipeline" | "skeptic_sweep" | "unknown"
    finalized: bool
    updated_at: datetime | None
    errors: list[dict[str, Any]] = field(default_factory=list)

    def is_interrupted(self, *, threshold_seconds: int, now: datetime | None = None) -> bool:
        """A run is interrupted if it never finalized and its latest checkpoint
        is older than the threshold (the graph stopped making progress)."""
        if self.finalized or self.updated_at is None:
            return False
        ref = now or datetime.now(UTC)
        return (ref - self.updated_at).total_seconds() > threshold_seconds


def _classify_run(channel_values: dict[str, Any]) -> str:
    if "papers_scouted" in channel_values:
        return "pipeline"
    if "beliefs_considered" in channel_values:
        return "skeptic_sweep"
    return "unknown"


def _parse_ts(raw: Any) -> datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def read_run_states(*, scan_limit: int = 1000) -> list[RunCheckpointState]:
    """Latest checkpoint state per run thread, newest first.

    Returns an empty list when no Postgres checkpoint store is configured
    (local runs / tests), so callers degrade gracefully. ``scan_limit``
    bounds how many raw checkpoints are scanned before grouping by thread.
    """
    url = postgres_url()
    if url is None:
        return []

    from langgraph.checkpoint.postgres import PostgresSaver

    latest: dict[str, RunCheckpointState] = {}
    try:
        with PostgresSaver.from_conn_string(url) as saver:
            for tup in saver.list(None, limit=scan_limit):
                configurable = tup.config.get("configurable") or {}
                thread_id = str(configurable.get("thread_id") or "")
                if not thread_id:
                    continue
                ts = _parse_ts(tup.checkpoint.get("ts"))
                existing = latest.get(thread_id)
                if (
                    existing is not None
                    and existing.updated_at is not None
                    and ts is not None
                    and existing.updated_at >= ts
                ):
                    continue
                cv = tup.checkpoint.get("channel_values") or {}
                errors = cv.get("errors") or []
                latest[thread_id] = RunCheckpointState(
                    run_id=thread_id,
                    run_type=_classify_run(cv),
                    finalized=bool(cv.get("finalized", False)),
                    updated_at=ts,
                    errors=list(errors),
                )
    except Exception:
        # Best-effort: a status page must not 500 because the checkpoint
        # store is unreachable or mid-migration. Degrade to what we have.
        return sorted(
            latest.values(),
            key=lambda s: s.updated_at or datetime.min.replace(tzinfo=UTC),
            reverse=True,
        )

    return sorted(
        latest.values(),
        key=lambda s: s.updated_at or datetime.min.replace(tzinfo=UTC),
        reverse=True,
    )
