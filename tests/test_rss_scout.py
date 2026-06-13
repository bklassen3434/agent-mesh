from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock, patch

from mesh_agents.rss_scout import (
    ScoutRssSkillInput,
    _entry_to_paper,
    _fetch_feed,
    _handle_scout_rss,
    _matches_terms,
)
from mesh_models.source import SourceType


def _entry(
    published_at: datetime,
    title: str = "New model release",
    summary: str = "Body text describing a release. " * 4,
    link: str = "https://example.com/post",
    author: str = "Sample Lab",
) -> dict[str, Any]:
    return {
        "title": title,
        "summary": summary,
        "link": link,
        "published_parsed": time.gmtime(published_at.timestamp()),
        "author": author,
    }


def test_entry_to_paper_basic() -> None:
    paper = _entry_to_paper(
        "https://example.com/feed", _entry(datetime.now(UTC)), None, [], []
    )
    assert paper is not None
    assert paper.source.type == SourceType.rss
    assert paper.source.url == "https://example.com/post"
    assert paper.arxiv_id.startswith("rss_")


def test_cutoff_skips_old_entries() -> None:
    now = datetime.now(UTC)
    cutoff = now - timedelta(hours=24)
    paper = _entry_to_paper(
        "https://example.com/feed", _entry(now - timedelta(hours=48)), cutoff, [], []
    )
    assert paper is None


def test_html_stripped_from_abstract() -> None:
    entry = _entry(datetime.now(UTC), summary="<p>Hello <b>world</b></p><script>x</script>")
    paper = _entry_to_paper("https://example.com/feed", entry, None, [], [])
    assert paper is not None
    assert "<" not in paper.abstract
    assert "Hello" in paper.abstract


def test_include_terms_filter() -> None:
    assert _matches_terms("a robotics paper", include=["robot"], exclude=[])
    assert not _matches_terms("a cooking blog", include=["robot"], exclude=[])


def test_exclude_terms_filter() -> None:
    assert not _matches_terms("sponsored post about ai", include=[], exclude=["sponsored"])
    assert _matches_terms("real ai research", include=[], exclude=["sponsored"])


def test_entry_dropped_by_include_filter() -> None:
    entry = _entry(datetime.now(UTC), title="Cooking tips", summary="recipes for dinner")
    paper = _entry_to_paper(
        "https://example.com/feed", entry, None, include=["robotics"], exclude=[]
    )
    assert paper is None


def test_fetch_feed_dedupes_and_caps() -> None:
    now = datetime.now(UTC)
    same = _entry(now, summary="identical body text " * 5, link="https://example.com/a")
    dup = _entry(now, summary="identical body text " * 5, link="https://example.com/b")
    other = _entry(now, summary="different body entirely " * 5, link="https://example.com/c")
    fake = MagicMock()
    fake.bozo = 0
    fake.entries = [same, dup, other]
    with patch("mesh_agents.rss_scout.feedparser.parse", return_value=fake):
        papers = _fetch_feed("https://example.com/feed", None, [], [], max_results=10)
    # same + dup collapse on content hash → 2 distinct papers
    assert len(papers) == 2


def test_fetch_feed_parse_failure_returns_empty() -> None:
    with patch("mesh_agents.rss_scout.feedparser.parse", side_effect=ValueError("boom")):
        papers = _fetch_feed("https://bad/feed", None, [], [], max_results=10)
    assert papers == []


def test_handle_scout_rss_shape() -> None:
    now = datetime.now(UTC)
    fake = MagicMock()
    fake.bozo = 0
    fake.entries = [_entry(now)]
    with patch("mesh_agents.rss_scout.feedparser.parse", return_value=fake):
        out = asyncio.run(
            _handle_scout_rss({"feed_url": "https://example.com/feed", "max_results": 5})
        )
    assert "papers" in out
    assert out["papers"][0]["source"]["type"] == "rss"


def test_skill_input_defaults() -> None:
    si = ScoutRssSkillInput.model_validate({"feed_url": "https://x/feed"})
    assert si.include_terms == []
    assert si.exclude_terms == []
    assert si.max_results == 20
