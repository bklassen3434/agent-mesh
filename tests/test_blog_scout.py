from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from mesh_agents.blog_scout import (
    BlogScoutAgent,
    FeedEntry,
    _entry_to_paper,
    _fetch_blogs,
    _handle_scout_blogs,
    _load_feeds_from_file,
    _resolve_lookback_hours,
)
from mesh_models.source import SourceType


def _entry(
    published_at: datetime,
    title: str = "New release notes",
    summary: str = "Body text describing a release. " * 4,
    link: str = "https://example.com/post",
) -> dict[str, Any]:
    return {
        "title": title,
        "summary": summary,
        "link": link,
        "published_parsed": time.gmtime(published_at.timestamp()),
        "author": "Sample Lab",
    }


def test_entry_to_paper_within_window() -> None:
    now = datetime.now(UTC)
    cutoff = now - timedelta(hours=24)
    paper = _entry_to_paper("Sample Lab", _entry(now - timedelta(hours=2)), cutoff=cutoff)
    assert paper is not None
    assert paper.source.type == SourceType.blog
    assert paper.title.startswith("Sample Lab")


def test_entry_outside_window_skipped() -> None:
    now = datetime.now(UTC)
    cutoff = now - timedelta(hours=24)
    paper = _entry_to_paper("Sample Lab", _entry(now - timedelta(hours=48)), cutoff=cutoff)
    assert paper is None


def test_html_summary_stripped() -> None:
    now = datetime.now(UTC)
    entry = _entry(now, summary="<p>Hello <b>world</b></p><script>x</script>")
    paper = _entry_to_paper("Lab", entry, cutoff=now - timedelta(hours=24))
    assert paper is not None
    assert "<" not in paper.abstract
    assert "Hello" in paper.abstract


def test_fetch_blogs_dedups_across_feeds() -> None:
    now = datetime.now(UTC)
    shared = _entry(now, link="https://shared/post", summary="duplicated body text " * 5)

    def fake_parse(url: str) -> Any:
        out = MagicMock()
        out.bozo = False
        out.entries = [shared]
        return out

    with patch("mesh_agents.blog_scout.feedparser.parse", side_effect=fake_parse):
        papers = _fetch_blogs(
            feeds=[FeedEntry(name="A", url="a"), FeedEntry(name="B", url="b")],
            lookback_hours=24,
            max_results=10,
        )
    assert len(papers) == 1


def test_unparseable_feed_does_not_abort() -> None:
    def fake_parse(url: str) -> Any:
        out = MagicMock()
        out.bozo = True
        out.bozo_exception = Exception("bad XML")
        out.entries = []
        return out

    with patch("mesh_agents.blog_scout.feedparser.parse", side_effect=fake_parse):
        papers = _fetch_blogs(
            feeds=[FeedEntry(name="broken", url="b")],
            lookback_hours=24,
            max_results=10,
        )
    assert papers == []


def test_resolve_lookback_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MESH_BLOG_LOOKBACK_HOURS", "48")
    assert _resolve_lookback_hours(None) == 48


def test_load_feeds_from_yaml(tmp_path: Path) -> None:
    p = tmp_path / "feeds.yaml"
    p.write_text(
        "feeds:\n"
        "  - name: A\n    url: https://a.example/feed\n"
        "  - name: B\n    url: https://b.example/feed\n"
    )
    feeds = _load_feeds_from_file(p)
    assert [f.name for f in feeds] == ["A", "B"]


def test_handle_returns_dict() -> None:
    now = datetime.now(UTC)
    entry = _entry(now)

    def fake_parse(url: str) -> Any:
        out = MagicMock()
        out.bozo = False
        out.entries = [entry]
        return out

    with patch("mesh_agents.blog_scout.feedparser.parse", side_effect=fake_parse):
        out = asyncio.run(
            _handle_scout_blogs(
                {
                    "feeds": [{"name": "Sample", "url": "https://sample/feed"}],
                    "lookback_hours": 24,
                    "max_results": 5,
                }
            )
        )
    assert "papers" in out
    assert out["papers"][0]["source"]["type"] == "blog"


@patch("mesh_agents.blog_scout.build_agent_card")
def test_a2a_card_declares_scout_blogs_skill(mock_card: MagicMock) -> None:
    BlogScoutAgent().to_a2a_server(url="http://blog-scout:8011")
    kwargs = mock_card.call_args.kwargs
    assert kwargs["skill_id"] == "scout_blogs"


def test_default_feed_file_exists() -> None:
    # The repo ships a default config/blog_feeds.yaml; the loader should find it.
    from mesh_agents.blog_scout import _default_feed_file
    assert _default_feed_file().is_file()
