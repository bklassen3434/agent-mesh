from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from mesh_agents.bluesky_scout import (
    BlueskyScoutAgent,
    _fetch_bluesky,
    _handle_scout_bluesky,
    _post_to_paper,
    _resolve_handles,
    _resolve_hashtags,
)
from mesh_models.source import SourceType


def _post(text: str = "Some long enough post about LLMs and tools" * 2,
          handle: str = "alice.bsky.social",
          rkey: str = "abc123",
          created_at: str = "2026-05-01T10:00:00Z") -> dict[str, Any]:
    return {
        "uri": f"at://did:plc:foo/app.bsky.feed.post/{rkey}",
        "record": {"text": text, "createdAt": created_at},
        "author": {"handle": handle},
    }


def _search_resp(posts: list[dict[str, Any]]) -> MagicMock:
    r = MagicMock()
    r.raise_for_status = MagicMock()
    r.json.return_value = {"posts": posts}
    return r


def _author_resp(posts: list[dict[str, Any]]) -> MagicMock:
    r = MagicMock()
    r.raise_for_status = MagicMock()
    r.json.return_value = {"feed": [{"post": p} for p in posts]}
    return r


@pytest.fixture
def fake_client(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    client = MagicMock()
    client.__enter__ = MagicMock(return_value=client)
    client.__exit__ = MagicMock(return_value=False)
    monkeypatch.setattr("mesh_agents.bluesky_scout.httpx.Client", lambda: client)
    return client


def test_post_to_paper_builds_permalink() -> None:
    paper = _post_to_paper(
        _post(handle="bob.bsky.social", rkey="xyz"), min_text_len=10
    )
    assert paper is not None
    assert paper.source.type == SourceType.bluesky
    assert paper.source.url == "https://bsky.app/profile/bob.bsky.social/post/xyz"


def test_short_posts_are_filtered() -> None:
    assert _post_to_paper(_post(text="too short"), min_text_len=40) is None


def test_fetch_hashtag_lane(fake_client: MagicMock) -> None:
    fake_client.get.return_value = _search_resp([_post(rkey="a"), _post(text="x" * 80, rkey="b")])
    papers = _fetch_bluesky(handles=[], hashtags=["ai"], max_results=5, min_text_len=40)
    assert len(papers) == 2
    assert all(p.source.type == SourceType.bluesky for p in papers)


def test_fetch_author_lane(fake_client: MagicMock) -> None:
    fake_client.get.return_value = _author_resp([_post(rkey="z")])
    papers = _fetch_bluesky(
        handles=["alice.bsky.social"], hashtags=[], max_results=5, min_text_len=40
    )
    assert len(papers) == 1
    assert papers[0].source.author == "alice.bsky.social"


def test_dedup_across_lanes(fake_client: MagicMock) -> None:
    same_text = "A duplicated post showing up via both hashtag and author lanes."
    fake_client.get.side_effect = [
        _search_resp([_post(text=same_text, rkey="r1")]),
        _author_resp([_post(text=same_text, rkey="r2")]),
    ]
    papers = _fetch_bluesky(
        handles=["alice.bsky.social"],
        hashtags=["ai"],
        max_results=10,
        min_text_len=40,
    )
    assert len(papers) == 1


def test_one_lane_failure_does_not_abort(fake_client: MagicMock) -> None:
    import httpx
    fake_client.get.side_effect = [
        httpx.ConnectError("nope"),
        _author_resp([_post(rkey="ok")]),
    ]
    papers = _fetch_bluesky(
        handles=["alice.bsky.social"], hashtags=["ai"], max_results=5, min_text_len=40
    )
    assert len(papers) == 1


def test_resolve_hashtags_strips_hashes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MESH_BLUESKY_HASHTAGS", "#ai, ml, #llm")
    assert _resolve_hashtags(None) == ["ai", "ml", "llm"]


def test_resolve_handles_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MESH_BLUESKY_HANDLES", "alice.bsky.social, bob.bsky.social")
    assert _resolve_handles(None) == ["alice.bsky.social", "bob.bsky.social"]


def test_handle_returns_dict(fake_client: MagicMock) -> None:
    fake_client.get.return_value = _search_resp([_post(rkey="d")])
    out = asyncio.run(
        _handle_scout_bluesky({"hashtags": ["ai"], "handles": [], "max_results": 5})
    )
    assert "papers" in out
    assert out["papers"][0]["source"]["type"] == "bluesky"


@patch("mesh_agents.bluesky_scout.build_multi_skill_card")
def test_a2a_card_declares_scout_bluesky_skill(mock_card: MagicMock) -> None:
    BlueskyScoutAgent().to_a2a_server(url="http://bluesky-scout:8009")
    kwargs = mock_card.call_args.kwargs
    skill_ids = {s.id for s in kwargs["skills"]}
    assert "scout_bluesky" in skill_ids
    assert "investigate_bluesky" in skill_ids
