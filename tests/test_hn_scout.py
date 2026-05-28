from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest
from mesh_agents.hn_scout import (
    HNScoutAgent,
    HNScoutInput,
    HNScoutOutput,
    _fetch_hn,
    _resolve_keywords,
)
from mesh_models.source import SourceType


def _fake_hit(
    object_id: str = "001",
    title: str = "Show HN: tiny LLM",
    url: str | None = "https://example.com/llm",
    story_text: str | None = None,
    author: str = "alice",
    points: int = 100,
) -> dict[str, object]:
    return {
        "objectID": object_id,
        "title": title,
        "url": url,
        "story_text": story_text,
        "author": author,
        "points": points,
        "created_at": "2026-05-01T12:00:00Z",
    }


def _mock_response(hits: list[dict[str, object]]) -> MagicMock:
    r = MagicMock()
    r.raise_for_status = MagicMock()
    r.json.return_value = {"hits": hits}
    return r


@pytest.fixture
def fake_client(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Patch httpx.Client to return canned Algolia hits, one batch per call."""
    client = MagicMock()
    client.__enter__ = MagicMock(return_value=client)
    client.__exit__ = MagicMock(return_value=False)
    monkeypatch.setattr("mesh_agents.hn_scout.httpx.Client", lambda: client)
    return client


def test_fetch_returns_scouted_papers_with_hn_source_type(fake_client: MagicMock) -> None:
    fake_client.get.return_value = _mock_response([_fake_hit("a"), _fake_hit("b", title="Llama 4")])

    papers = _fetch_hn(keywords=["LLM"], max_results=10, min_points=20)

    assert len(papers) == 2
    assert all(p.source.type == SourceType.hn_post for p in papers)
    assert papers[0].title == "Show HN: tiny LLM"


def test_dedup_by_raw_content_hash(fake_client: MagicMock) -> None:
    body = "The exact same body."
    dup = _fake_hit("a", story_text=body)
    fake_client.get.return_value = _mock_response([dup, _fake_hit("b", story_text=body)])

    papers = _fetch_hn(keywords=["AI"], max_results=10, min_points=20)

    assert len(papers) == 1


def test_link_post_falls_back_to_title_as_abstract(fake_client: MagicMock) -> None:
    hit = _fake_hit("a", story_text=None, title="Robotics news")
    fake_client.get.return_value = _mock_response([hit])

    papers = _fetch_hn(keywords=["robotics"], max_results=5, min_points=20)

    assert len(papers) == 1
    assert papers[0].abstract == "Robotics news"


def test_per_keyword_query_includes_points_filter(fake_client: MagicMock) -> None:
    fake_client.get.return_value = _mock_response([])
    _fetch_hn(keywords=["agent"], max_results=5, min_points=50)
    call = fake_client.get.call_args
    assert call.kwargs["params"]["query"] == "agent"
    assert call.kwargs["params"]["tags"] == "story"
    assert call.kwargs["params"]["numericFilters"] == "points>=50"


def test_failing_keyword_does_not_abort(fake_client: MagicMock) -> None:
    import httpx

    def maybe_fail(*args: object, **kwargs: object) -> MagicMock:
        params = kwargs["params"]
        if params["query"] == "boom":  # type: ignore[index]
            raise httpx.ConnectError("nope")
        return _mock_response([_fake_hit("ok")])

    fake_client.get.side_effect = maybe_fail

    papers = _fetch_hn(keywords=["boom", "AI"], max_results=10, min_points=20)

    assert len(papers) == 1
    assert papers[0].arxiv_id == "ok"


def test_resolve_keywords_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MESH_HN_KEYWORDS", "foo, bar, baz")
    assert _resolve_keywords(None) == ["foo", "bar", "baz"]


def test_resolve_keywords_explicit_arg_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MESH_HN_KEYWORDS", "foo")
    assert _resolve_keywords(["bar"]) == ["bar"]


def test_agent_run_returns_hnscoutoutput(fake_client: MagicMock) -> None:
    fake_client.get.return_value = _mock_response([_fake_hit("x")])
    agent = HNScoutAgent()
    result = asyncio.run(agent.run(HNScoutInput(keywords=["LLM"], max_results=1)))
    assert isinstance(result, HNScoutOutput)
    assert len(result.papers) == 1


@patch("mesh_agents.hn_scout.build_multi_skill_card")
def test_a2a_card_declares_scout_hn_skill(mock_card: MagicMock) -> None:
    HNScoutAgent().to_a2a_server(url="http://hn-scout:8005")
    kwargs = mock_card.call_args.kwargs
    skill_ids = {s.id for s in kwargs["skills"]}
    assert "scout_hn" in skill_ids
    assert "investigate_hn" in skill_ids
    assert kwargs["name"] == "HN Scout"
