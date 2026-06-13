from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from mesh_agents.github_scout import (
    GitHubScoutAgent,
    ScoutGithubSkillInput,
    _fetch_github,
    _fetch_repos_by_query,
    _handle_investigate_github,
    _handle_scout_github,
    _parse_atom_entries,
    _resolve_topics,
    _resolve_watchlist,
)
from mesh_models.source import SourceType

_SEARCH_HIT = {
    "name": "vllm",
    "full_name": "vllm-project/vllm",
    "html_url": "https://github.com/vllm-project/vllm",
    "description": "Fast LLM inference engine",
    "topics": ["llm", "inference"],
    "pushed_at": "2026-05-01T12:00:00Z",
    "owner": {"login": "vllm-project"},
}


def _search_response(items: list[dict[str, Any]]) -> MagicMock:
    r = MagicMock()
    r.status_code = 200
    r.raise_for_status = MagicMock()
    r.json.return_value = {"items": items}
    return r


def _readme_response(text: str) -> MagicMock:
    r = MagicMock()
    r.status_code = 200
    r.text = text
    return r


@pytest.fixture
def fake_client(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    client = MagicMock()
    client.__enter__ = MagicMock(return_value=client)
    client.__exit__ = MagicMock(return_value=False)
    monkeypatch.setattr("mesh_agents.github_scout.httpx.Client", lambda: client)
    return client


def test_fetch_trending_returns_github_sources(fake_client: MagicMock) -> None:
    fake_client.get.side_effect = [
        _search_response([_SEARCH_HIT]),
        _readme_response("# vllm\n\nFast and memory-efficient inference for LLMs."),
    ]
    papers = _fetch_github(
        topics=["llm"], max_results=5, watchlist=[], since=None
    )
    assert len(papers) == 1
    p = papers[0]
    assert p.source.type == SourceType.github
    assert p.title == "vllm-project/vllm"
    assert "Fast and memory-efficient" in p.abstract
    assert p.arxiv_id == "vllm-project_vllm"


def test_fetch_trending_handles_missing_readme(fake_client: MagicMock) -> None:
    missing = MagicMock()
    missing.status_code = 404
    fake_client.get.side_effect = [_search_response([_SEARCH_HIT]), missing]
    papers = _fetch_github(
        topics=["llm"], max_results=5, watchlist=[], since=None
    )
    assert len(papers) == 1
    # Falls back to description + topics
    assert "Fast LLM inference engine" in papers[0].abstract
    assert "Topics: llm" in papers[0].abstract


def test_parse_atom_entries_picks_up_release_notes() -> None:
    xml = """
    <feed xmlns="http://www.w3.org/2005/Atom">
      <entry>
        <id>tag:github.com,2008:Repository/123/v1.2.3</id>
        <title>v1.2.3</title>
        <updated>2026-05-10T15:00:00Z</updated>
        <link href="https://github.com/foo/bar/releases/tag/v1.2.3"/>
        <content>Major improvements to scheduler and inference path.</content>
      </entry>
    </feed>
    """
    entries = _parse_atom_entries(xml)
    assert len(entries) == 1
    assert entries[0]["title"] == "v1.2.3"
    assert "scheduler" in entries[0]["content"]


def test_fetch_watchlist_releases(fake_client: MagicMock) -> None:
    atom = """
    <feed xmlns="http://www.w3.org/2005/Atom">
      <entry>
        <id>tag:github.com,2008:Repository/123/v2.0.0</id>
        <title>v2.0.0</title>
        <updated>2026-05-10T15:00:00Z</updated>
        <link href="https://github.com/foo/bar/releases/tag/v2.0.0"/>
        <content>Big release.</content>
      </entry>
    </feed>
    """
    feed_resp = MagicMock(status_code=200, text=atom)
    # No trending: empty search response, then the atom feed
    fake_client.get.side_effect = [_search_response([]), feed_resp]
    papers = _fetch_github(
        topics=["llm"], max_results=0, watchlist=["foo/bar"], since=None
    )
    assert len(papers) == 1
    assert papers[0].title.startswith("foo/bar")
    assert papers[0].source.type == SourceType.github


def test_dedup_by_raw_content_hash(fake_client: MagicMock) -> None:
    body = "exact same description"
    item_a = {**_SEARCH_HIT, "name": "a", "full_name": "x/a", "html_url": "https://github.com/x/a"}
    item_b = {**_SEARCH_HIT, "name": "b", "full_name": "x/b", "html_url": "https://github.com/x/b"}
    fake_client.get.side_effect = [
        _search_response([item_a, item_b]),
        _readme_response(body),
        _readme_response(body),
    ]
    papers = _fetch_github(topics=["llm"], max_results=5, watchlist=[], since=None)
    assert len(papers) == 1


def test_resolve_topics_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MESH_GITHUB_TOPICS", "llm, agents")
    assert _resolve_topics(None) == ["llm", "agents"]


def test_resolve_watchlist_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MESH_GITHUB_WATCHLIST", "vllm-project/vllm, huggingface/transformers")
    assert _resolve_watchlist(None) == [
        "vllm-project/vllm",
        "huggingface/transformers",
    ]


def test_handle_scout_github_returns_dict(fake_client: MagicMock) -> None:
    fake_client.get.side_effect = [
        _search_response([_SEARCH_HIT]),
        _readme_response("README content"),
    ]
    out = asyncio.run(
        _handle_scout_github({"topics": ["llm"], "max_results": 1, "watchlist": []})
    )
    assert "papers" in out
    assert len(out["papers"]) == 1
    assert out["papers"][0]["source"]["type"] == "github"


def test_investigate_searches_repos_by_hypothesis(fake_client: MagicMock) -> None:
    fake_client.get.side_effect = [
        _search_response([_SEARCH_HIT]),
        _readme_response("# vllm\n\nPagedAttention for fast LLM serving."),
    ]
    papers = _fetch_repos_by_query("paged attention llm serving", max_results=5)
    assert len(papers) == 1
    assert papers[0].source.type == SourceType.github
    assert "PagedAttention" in papers[0].abstract
    # The query is passed straight to GitHub's free-text repo search.
    search_params = fake_client.get.call_args_list[0].kwargs["params"]
    assert search_params["q"] == "paged attention llm serving"


def test_investigate_github_handler_returns_source_records(fake_client: MagicMock) -> None:
    fake_client.get.side_effect = [
        _search_response([_SEARCH_HIT]),
        _readme_response("README"),
    ]
    out = asyncio.run(
        _handle_investigate_github(
            {
                "investigation_id": "inv-1",
                "hypothesis": "Is vLLM still SOTA for inference throughput?",
                "max_results": 3,
            }
        )
    )
    assert out["investigation_id"] == "inv-1"
    assert len(out["source_records"]) == 1
    assert out["source_records"][0]["source"]["type"] == "github"


@patch("mesh_agents.github_scout.build_multi_skill_card")
def test_a2a_card_declares_scout_github_skill(mock_card: MagicMock) -> None:
    GitHubScoutAgent().to_a2a_server(url="http://github-scout:8008")
    kwargs = mock_card.call_args.kwargs
    skill_ids = {s.id for s in kwargs["skills"]}
    assert "scout_github" in skill_ids
    assert "investigate_github" in skill_ids
    assert kwargs["name"] == "GitHub Scout"


def test_skill_input_defaults_to_env_or_built_in() -> None:
    si = ScoutGithubSkillInput()
    assert si.max_results == 10
    assert si.topics is None
