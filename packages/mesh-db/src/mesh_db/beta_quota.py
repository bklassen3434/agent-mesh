"""Beta chatbot quota ledger (``runtime.beta_query_log``).

Anonymous wiki visitors get ``MESH_BETA_DAILY_QUERY_LIMIT`` chatbot questions
per day, counted per browser-issued beta id. Authenticated admins bypass the
quota entirely (the wiki never sends a beta id for them). These helpers are the
server-side count the API enforces against, so the cap can't be cleared from the
browser — the count lives in Postgres, not in a cookie.
"""
from __future__ import annotations

import os
from datetime import date

from mesh_db.connection import MeshConnection

__all__ = ["consume_quota", "daily_limit", "quota_remaining", "quota_used"]

_DEFAULT_LIMIT = 3


def daily_limit() -> int:
    """Questions a beta visitor may ask per day (``MESH_BETA_DAILY_QUERY_LIMIT``)."""
    raw = os.environ.get("MESH_BETA_DAILY_QUERY_LIMIT", str(_DEFAULT_LIMIT))
    try:
        return max(0, int(raw))
    except ValueError:
        return _DEFAULT_LIMIT


def quota_used(conn: MeshConnection, beta_id: str, day: date | None = None) -> int:
    """Questions ``beta_id`` has already asked on ``day`` (default: today)."""
    day = day or date.today()
    row = conn.execute(
        "SELECT count FROM runtime.beta_query_log WHERE beta_id = %s AND day = %s",
        (beta_id, day),
    ).fetchone()
    return int(row[0]) if row else 0


def quota_remaining(conn: MeshConnection, beta_id: str, day: date | None = None) -> int:
    """Questions ``beta_id`` has left today (never negative)."""
    return max(0, daily_limit() - quota_used(conn, beta_id, day))


def consume_quota(conn: MeshConnection, beta_id: str, day: date | None = None) -> int:
    """Increment today's count for ``beta_id`` and return the new total.

    Callers check ``quota_remaining`` first; this only records a question that
    was actually answered. Writer role; commits.
    """
    day = day or date.today()
    row = conn.execute(
        """
        INSERT INTO runtime.beta_query_log (beta_id, day, count)
        VALUES (%s, %s, 1)
        ON CONFLICT (beta_id, day)
        DO UPDATE SET count = runtime.beta_query_log.count + 1
        RETURNING count
        """,
        (beta_id, day),
    ).fetchone()
    conn.commit()
    return int(row[0]) if row else 1
