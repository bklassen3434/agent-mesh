"""Freshness eval — is the field still ingesting current content?

Pure DB, no LLM. For each source type in the field, how old is the newest
published item? The headline is the age of the *freshest* source overall (the
field is "fresh" if anything recent came in); the per-type breakdown surfaces a
single dead feed that the headline would otherwise hide.
"""
from __future__ import annotations

from datetime import UTC, datetime

from mesh_db.connection import MeshConnection
from mesh_db.sources import source_freshness

from mesh_agents.eval.models import FreshnessReport, FreshnessRow

DEFAULT_STALE_AFTER_HOURS = 36.0


def _age_hours(ts: datetime | None, now: datetime) -> float | None:
    if ts is None:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return (now - ts).total_seconds() / 3600.0


def assess_freshness(
    conn: MeshConnection,
    field_id: str,
    *,
    stale_after_hours: float = DEFAULT_STALE_AFTER_HOURS,
    now: datetime | None = None,
) -> FreshnessReport:
    now = now or datetime.now(UTC)
    rows: list[FreshnessRow] = []
    for source_type, count, newest_pub, newest_fetch in source_freshness(conn, field_id):
        age = _age_hours(newest_pub, now)
        rows.append(
            FreshnessRow(
                source_type=source_type,
                count=count,
                newest_published=newest_pub,
                newest_fetched=newest_fetch,
                age_hours=age,
                stale=age is None or age > stale_after_hours,
            )
        )

    ages = [r.age_hours for r in rows if r.age_hours is not None]
    newest = min(ages) if ages else None

    return FreshnessReport(
        field_id=field_id,
        generated_at=now,
        stale_after_hours=stale_after_hours,
        newest_source_age_hours=newest,
        rows=rows,
    )
