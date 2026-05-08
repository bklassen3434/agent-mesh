from __future__ import annotations

from datetime import datetime

import duckdb
from mesh_db.sources import create_source, get_source_by_id, list_sources, update_source
from mesh_models.source import Source, SourceType


def _make_source(**kwargs: object) -> Source:
    defaults: dict[str, object] = {
        "type": SourceType.arxiv,
        "url": "https://arxiv.org/abs/2301.00001",
        "published_at": datetime(2023, 1, 1),
        "raw_content_hash": "abc123",
    }
    defaults.update(kwargs)
    return Source(**defaults)  # type: ignore[arg-type]


def test_create_and_get(tmp_db: duckdb.DuckDBPyConnection) -> None:
    s = _make_source()
    create_source(tmp_db, s)
    fetched = get_source_by_id(tmp_db, s.id)
    assert fetched is not None
    assert fetched.url == s.url
    assert fetched.type == SourceType.arxiv


def test_get_missing_returns_none(tmp_db: duckdb.DuckDBPyConnection) -> None:
    assert get_source_by_id(tmp_db, "nope") is None


def test_list_all(tmp_db: duckdb.DuckDBPyConnection) -> None:
    create_source(tmp_db, _make_source(url="u1", raw_content_hash="h1"))
    create_source(tmp_db, _make_source(url="u2", raw_content_hash="h2", type=SourceType.blog))
    result = list_sources(tmp_db)
    assert len(result) == 2


def test_list_filter_by_type(tmp_db: duckdb.DuckDBPyConnection) -> None:
    create_source(tmp_db, _make_source(url="u3", raw_content_hash="h3", type=SourceType.github))
    create_source(tmp_db, _make_source(url="u4", raw_content_hash="h4", type=SourceType.arxiv))
    github = list_sources(tmp_db, type=SourceType.github)
    assert all(s.type == SourceType.github for s in github)


def test_author_none(tmp_db: duckdb.DuckDBPyConnection) -> None:
    s = _make_source()
    create_source(tmp_db, s)
    fetched = get_source_by_id(tmp_db, s.id)
    assert fetched is not None
    assert fetched.author is None


def test_update_reliability(tmp_db: duckdb.DuckDBPyConnection) -> None:
    s = _make_source()
    create_source(tmp_db, s)
    updated = update_source(tmp_db, s.id, reliability_prior=0.9)
    assert abs(updated.reliability_prior - 0.9) < 1e-6
