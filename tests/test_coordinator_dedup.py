"""Phase 11b: the coordinator dedup gate (_dedup_for_extraction)."""
from __future__ import annotations

from datetime import UTC, datetime

from mesh_agents.arxiv_scout import ScoutedPaper
from mesh_db.connection import MeshConnection
from mesh_db.processed_items import get_processed_item, record_processed_item
from mesh_models.source import Source, SourceType
from mesh_pipeline.coordinator import _dedup_for_extraction


def _paper(url: str, content_hash: str) -> ScoutedPaper:
    return ScoutedPaper(
        source=Source(
            type=SourceType.arxiv,
            url=url,
            published_at=datetime(2026, 1, 1, tzinfo=UTC),
            raw_content_hash=content_hash,
        ),
        title="t",
        abstract="a",
        arxiv_id=url.rsplit("/", 1)[-1],
    )


def test_unseen_items_all_extract(tmp_db: MeshConnection) -> None:
    papers = [_paper("https://x/1", "h1"), _paper("https://x/2", "h2")]
    to_extract, skipped = _dedup_for_extraction(tmp_db, papers, datetime.now(UTC))
    assert len(to_extract) == 2
    assert skipped == 0


def test_unchanged_items_skipped(tmp_db: MeshConnection) -> None:
    record_processed_item(tmp_db, "arxiv", "https://x/1", "h1")
    papers = [_paper("https://x/1", "h1"), _paper("https://x/2", "h2")]
    to_extract, skipped = _dedup_for_extraction(tmp_db, papers, datetime.now(UTC))
    assert [p.source.url for p in to_extract] == ["https://x/2"]
    assert skipped == 1


def test_changed_content_re_extracts(tmp_db: MeshConnection) -> None:
    record_processed_item(tmp_db, "arxiv", "https://x/1", "h1")
    papers = [_paper("https://x/1", "h1-revised")]
    to_extract, skipped = _dedup_for_extraction(tmp_db, papers, datetime.now(UTC))
    assert [p.source.url for p in to_extract] == ["https://x/1"]
    assert skipped == 0


def test_touch_updates_last_seen_for_skipped(tmp_db: MeshConnection) -> None:
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    record_processed_item(tmp_db, "arxiv", "https://x/1", "h1", now=t0)
    later = datetime(2026, 2, 1, tzinfo=UTC)
    _dedup_for_extraction(tmp_db, [_paper("https://x/1", "h1")], later)
    item = get_processed_item(tmp_db, "arxiv", "https://x/1")
    assert item is not None and item.last_seen_at == later


def test_intra_batch_duplicates_collapse(tmp_db: MeshConnection) -> None:
    papers = [_paper("https://x/1", "h1"), _paper("https://x/1", "h1")]
    to_extract, skipped = _dedup_for_extraction(tmp_db, papers, datetime.now(UTC))
    assert len(to_extract) == 1
    assert skipped == 0
