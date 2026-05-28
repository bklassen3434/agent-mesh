from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from mesh_agents.reddit_scout import (
    RedditScoutAgent,
    _fetch_reddit,
    _handle_scout_reddit,
    _resolve_subs,
)
from mesh_models.source import SourceType


def _listing_resp(posts: list[dict[str, Any]]) -> MagicMock:
    r = MagicMock()
    r.raise_for_status = MagicMock()
    r.json.return_value = {"data": {"children": [{"data": p} for p in posts]}}
    return r


def _token_resp(token: str = "TOKEN") -> MagicMock:
    r = MagicMock()
    r.raise_for_status = MagicMock()
    r.json.return_value = {"access_token": token}
    return r


def _post(
    id_: str = "abc",
    title: str = "Interesting paper on LLMs",
    selftext: str = "Long enough body about LLM training.",
    score: int = 100,
    author: str = "alice",
    created_utc: float = 1714000000.0,
) -> dict[str, Any]:
    return {
        "id": id_,
        "title": title,
        "selftext": selftext,
        "score": score,
        "author": author,
        "created_utc": created_utc,
        "permalink": f"/r/MachineLearning/comments/{id_}/post/",
    }


@pytest.fixture
def fake_client(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    client = MagicMock()
    client.__enter__ = MagicMock(return_value=client)
    client.__exit__ = MagicMock(return_value=False)
    monkeypatch.setattr("mesh_agents.reddit_scout.httpx.Client", lambda: client)
    monkeypatch.setenv("REDDIT_CLIENT_ID", "id")
    monkeypatch.setenv("REDDIT_CLIENT_SECRET", "sec")
    return client


def test_fetch_reddit_returns_sources(fake_client: MagicMock) -> None:
    fake_client.post.return_value = _token_resp()
    fake_client.get.return_value = _listing_resp([_post(id_="p1")])
    papers = _fetch_reddit(
        subreddits=["MachineLearning"], listing="day", max_results=5, min_score=20
    )
    assert len(papers) == 1
    assert papers[0].source.type == SourceType.reddit
    assert papers[0].source.url.endswith("/p1/post/")


def test_low_score_posts_filtered(fake_client: MagicMock) -> None:
    fake_client.post.return_value = _token_resp()
    fake_client.get.return_value = _listing_resp(
        [_post(id_="big", score=999), _post(id_="small", score=5)]
    )
    papers = _fetch_reddit(
        subreddits=["MachineLearning"], listing="day", max_results=5, min_score=20
    )
    assert len(papers) == 1
    assert papers[0].arxiv_id.endswith("big")


def test_dedup_by_hash(fake_client: MagicMock) -> None:
    fake_client.post.return_value = _token_resp()
    body = "exact same body text shared across subs"
    fake_client.get.side_effect = [
        _listing_resp([_post(id_="a", selftext=body)]),
        _listing_resp([_post(id_="b", selftext=body)]),
    ]
    papers = _fetch_reddit(
        subreddits=["MachineLearning", "LocalLLaMA"],
        listing="day",
        max_results=10,
        min_score=20,
    )
    assert len(papers) == 1


def test_missing_creds_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REDDIT_CLIENT_ID", raising=False)
    monkeypatch.delenv("REDDIT_CLIENT_SECRET", raising=False)
    papers = _fetch_reddit(
        subreddits=["MachineLearning"], listing="day", max_results=5, min_score=20
    )
    assert papers == []


def test_one_sub_failure_does_not_abort(fake_client: MagicMock) -> None:
    import httpx
    fake_client.post.return_value = _token_resp()
    fake_client.get.side_effect = [
        httpx.ConnectError("nope"),
        _listing_resp([_post(id_="ok")]),
    ]
    papers = _fetch_reddit(
        subreddits=["broken", "ok_sub"], listing="day", max_results=5, min_score=20
    )
    assert len(papers) == 1


def test_resolve_subs_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MESH_REDDIT_SUBS", "MachineLearning, LocalLLaMA")
    assert _resolve_subs(None) == ["MachineLearning", "LocalLLaMA"]


def test_handle_scout_reddit_returns_dict(fake_client: MagicMock) -> None:
    fake_client.post.return_value = _token_resp()
    fake_client.get.return_value = _listing_resp([_post(id_="z")])
    out = asyncio.run(
        _handle_scout_reddit({"subreddits": ["MachineLearning"], "max_results": 1})
    )
    assert out["papers"][0]["source"]["type"] == "reddit"


@patch("mesh_agents.reddit_scout.build_multi_skill_card")
def test_a2a_card_declares_scout_reddit_skill(mock_card: MagicMock) -> None:
    RedditScoutAgent().to_a2a_server(url="http://reddit-scout:8010")
    kwargs = mock_card.call_args.kwargs
    skill_ids = {s.id for s in kwargs["skills"]}
    assert "scout_reddit" in skill_ids
    assert "investigate_reddit" in skill_ids
