"""Phase 11b: processed_items dedup ledger — the three ledger cases."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from mesh_db.connection import MeshConnection
from mesh_db.processed_items import (
    ProcessedDecision,
    decide,
    get_processed_item,
    record_processed_item,
    touch_processed_item,
)


def test_unseen_item(tmp_db: MeshConnection) -> None:
    assert decide(tmp_db, "arxiv", "https://x/1", "hashA") is ProcessedDecision.unseen


def test_unchanged_item(tmp_db: MeshConnection) -> None:
    record_processed_item(tmp_db, "arxiv", "https://x/1", "hashA")
    assert decide(tmp_db, "arxiv", "https://x/1", "hashA") is ProcessedDecision.unchanged


def test_changed_item(tmp_db: MeshConnection) -> None:
    record_processed_item(tmp_db, "arxiv", "https://x/1", "hashA")
    assert decide(tmp_db, "arxiv", "https://x/1", "hashB") is ProcessedDecision.changed


def test_record_is_upsert_preserving_first_seen(tmp_db: MeshConnection) -> None:
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    t1 = t0 + timedelta(days=3)
    record_processed_item(tmp_db, "arxiv", "https://x/1", "hashA", now=t0)
    record_processed_item(tmp_db, "arxiv", "https://x/1", "hashB", now=t1)

    item = get_processed_item(tmp_db, "arxiv", "https://x/1")
    assert item is not None
    assert item.content_hash == "hashB"
    assert item.first_seen_at == t0  # preserved across re-record
    assert item.last_seen_at == t1


def test_touch_bumps_last_seen_only(tmp_db: MeshConnection) -> None:
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    t1 = t0 + timedelta(days=1)
    record_processed_item(tmp_db, "hn", "https://y/2", "hashA", now=t0)
    touch_processed_item(tmp_db, "hn", "https://y/2", now=t1)

    item = get_processed_item(tmp_db, "hn", "https://y/2")
    assert item is not None
    assert item.content_hash == "hashA"  # unchanged
    assert item.first_seen_at == t0
    assert item.last_seen_at == t1


def test_distinct_external_ids_are_independent(tmp_db: MeshConnection) -> None:
    record_processed_item(tmp_db, "arxiv", "https://x/1", "hashA")
    assert decide(tmp_db, "arxiv", "https://x/2", "hashA") is ProcessedDecision.unseen
    # same external_id under a different source_type is also independent
    assert decide(tmp_db, "blog", "https://x/1", "hashA") is ProcessedDecision.unseen
