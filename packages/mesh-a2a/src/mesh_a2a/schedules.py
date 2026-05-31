"""Schedule config persistence (Phase 9).

Postgres-backed store for pipeline schedule config, living in the same
``langgraph-db`` container that holds the LangGraph checkpoints (the only
Postgres in the stack). Postgres is the source of truth: the API
reads/writes here, and the scheduler reconciles its in-process jobs
against this table.

There is no general Postgres migration runner — the checkpoint store
manages its own schema via ``saver.setup()``. So the ``schedules`` table
is created the same way: an idempotent ``ensure_schedules_table()`` that
both the API and the scheduler call before touching the table. The seed
rows match the env-var defaults (pipeline 6h, sweep 24h).
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import psycopg
from mesh_models.schedule import Schedule

from mesh_a2a.checkpoint import postgres_url

# job_id → default interval (hours). Seeded into the table on first ensure;
# also the fallback the scheduler uses if the table is somehow empty.
DEFAULT_INTERVALS: dict[str, int] = {
    "pipeline": 6,
    "skeptic_sweep": 24,
    # Phase 16c: memory consolidation runs daily, offline (batch API).
    "consolidation": 24,
}


class SchedulesUnavailable(RuntimeError):
    """Raised when no Postgres checkpoint store is configured.

    Callers (the API) map this to a 503 — schedule config simply isn't
    available in the local/in-memory deployment.
    """


def _require_url() -> str:
    url = postgres_url()
    if url is None:
        raise SchedulesUnavailable("LANGGRAPH_POSTGRES_URL is not configured")
    return url


@contextmanager
def _connect() -> Iterator[psycopg.Connection[Any]]:
    with psycopg.connect(_require_url(), autocommit=True) as conn:
        yield conn


_DDL = """
CREATE TABLE IF NOT EXISTS schedules (
    job_id         TEXT PRIMARY KEY,
    interval_hours INTEGER NOT NULL,
    enabled        BOOLEAN NOT NULL DEFAULT true,
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


def ensure_schedules_table() -> None:
    """Create the schedules table and seed defaults. Idempotent.

    Safe to call on every API request and at scheduler startup — the
    CREATE is ``IF NOT EXISTS`` and the seed is ``ON CONFLICT DO NOTHING``,
    so a populated table is left untouched.
    """
    with _connect() as conn:
        conn.execute(_DDL)
        for job_id, hours in DEFAULT_INTERVALS.items():
            conn.execute(
                "INSERT INTO schedules (job_id, interval_hours, enabled) "
                "VALUES (%s, %s, true) ON CONFLICT (job_id) DO NOTHING",
                (job_id, hours),
            )


def _row_to_schedule(row: tuple[Any, ...]) -> Schedule:
    return Schedule(
        job_id=str(row[0]),
        interval_hours=int(row[1]),
        enabled=bool(row[2]),
        updated_at=row[3],
    )


def list_schedules() -> list[Schedule]:
    ensure_schedules_table()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT job_id, interval_hours, enabled, updated_at "
            "FROM schedules ORDER BY job_id"
        ).fetchall()
    return [_row_to_schedule(r) for r in rows]


def get_schedule(job_id: str) -> Schedule | None:
    ensure_schedules_table()
    with _connect() as conn:
        row = conn.execute(
            "SELECT job_id, interval_hours, enabled, updated_at "
            "FROM schedules WHERE job_id = %s",
            (job_id,),
        ).fetchone()
    return _row_to_schedule(row) if row else None


def update_schedule(
    job_id: str,
    *,
    interval_hours: int | None = None,
    enabled: bool | None = None,
) -> Schedule | None:
    """Patch a schedule row. Returns the updated row, or None if absent.

    COALESCE keeps unspecified fields untouched; ``updated_at`` always
    advances so the scheduler's reconcile loop can detect the change.
    """
    ensure_schedules_table()
    with _connect() as conn:
        row = conn.execute(
            """
            UPDATE schedules
               SET interval_hours = COALESCE(%s, interval_hours),
                   enabled        = COALESCE(%s, enabled),
                   updated_at     = now()
             WHERE job_id = %s
            RETURNING job_id, interval_hours, enabled, updated_at
            """,
            (interval_hours, enabled, job_id),
        ).fetchone()
    return _row_to_schedule(row) if row else None
