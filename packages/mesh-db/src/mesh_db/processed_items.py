"""Processed-items ledger (Phase 11b) — dedup before extraction.

One row per logical scouted item, keyed on ``(source_type, external_id)``.
The coordinator consults this before spending tokens on ``extract_claims``:

* unseen ``external_id``            → extract, then record the row
* seen, ``content_hash`` unchanged  → skip extraction, bump ``last_seen_at``
* seen, ``content_hash`` changed    → re-extract, update the row

Written exclusively by the coordinator (single-writer; coordinator-owned).
"""
from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel

from mesh_db.connection import MeshConnection


class ProcessedItem(BaseModel):
    source_type: str
    external_id: str
    content_hash: str
    first_seen_at: datetime
    last_seen_at: datetime


class ProcessedDecision(StrEnum):
    """Outcome of checking an item against the ledger."""

    unseen = "unseen"  # never recorded → extract
    unchanged = "unchanged"  # recorded, same content_hash → skip
    changed = "changed"  # recorded, content_hash differs → re-extract


def get_processed_item(
    conn: MeshConnection, source_type: str, external_id: str
) -> ProcessedItem | None:
    row = conn.execute(
        """
        SELECT source_type, external_id, content_hash, first_seen_at, last_seen_at
        FROM processed_items
        WHERE source_type = %s AND external_id = %s
        """,
        [source_type, external_id],
    ).fetchone()
    if row is None:
        return None
    return ProcessedItem(
        source_type=str(row[0]),
        external_id=str(row[1]),
        content_hash=str(row[2]),
        first_seen_at=_dt(row[3]),
        last_seen_at=_dt(row[4]),
    )


def decide(
    conn: MeshConnection,
    source_type: str,
    external_id: str,
    content_hash: str,
) -> ProcessedDecision:
    existing = get_processed_item(conn, source_type, external_id)
    if existing is None:
        return ProcessedDecision.unseen
    if existing.content_hash == content_hash:
        return ProcessedDecision.unchanged
    return ProcessedDecision.changed


def record_processed_item(
    conn: MeshConnection,
    source_type: str,
    external_id: str,
    content_hash: str,
    now: datetime | None = None,
) -> None:
    """Insert a new ledger row or update an existing one's content_hash +
    last_seen_at (preserving first_seen_at). Call after a successful extraction."""
    ts = now or datetime.now(UTC)
    conn.execute(
        """
        INSERT INTO processed_items
            (source_type, external_id, content_hash, first_seen_at, last_seen_at)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (source_type, external_id) DO UPDATE SET
            content_hash = excluded.content_hash,
            last_seen_at = excluded.last_seen_at
        """,
        [source_type, external_id, content_hash, ts, ts],
    )


def touch_processed_item(
    conn: MeshConnection,
    source_type: str,
    external_id: str,
    now: datetime | None = None,
) -> None:
    """Bump last_seen_at for an item skipped as unchanged (we saw it again but
    spent no tokens on it)."""
    ts = now or datetime.now(UTC)
    conn.execute(
        """
        UPDATE processed_items SET last_seen_at = %s
        WHERE source_type = %s AND external_id = %s
        """,
        [ts, source_type, external_id],
    )


def _dt(val: object) -> datetime:
    return val if isinstance(val, datetime) else datetime.fromisoformat(str(val))
