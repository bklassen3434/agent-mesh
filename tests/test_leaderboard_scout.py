from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
from mesh_agents.leaderboard_scout import (
    LeaderboardScoutAgent,
    _fetch_chatbot_arena,
    _fetch_hf_open_llm,
    _fetch_leaderboards,
    _fetch_paperswithcode,
    _handle_scout_leaderboards,
)
from mesh_models.source import SourceType


def _ok(json_body: dict[str, Any] | None = None, text: str | None = None) -> MagicMock:
    r = MagicMock(status_code=200)
    r.raise_for_status = MagicMock()
    if json_body is not None:
        r.json.return_value = json_body
    if text is not None:
        r.text = text
    return r


def _client(get_side_effect: Any) -> MagicMock:
    c = MagicMock()
    c.get.side_effect = get_side_effect
    return c


def test_hf_open_llm_parses_rows() -> None:
    client = _client(
        lambda *a, **kw: _ok(
            json_body={
                "rows": [
                    {"row": {"model": "ModelA", "average": 78.4}},
                    {"row": {"model": "ModelB", "average": 75.1}},
                ]
            }
        )
    )
    papers = _fetch_hf_open_llm(client, top_n=5)
    assert len(papers) == 1
    assert papers[0].source.type == SourceType.leaderboard
    assert "ModelA" in papers[0].abstract
    assert "78.4" in papers[0].abstract


def test_hf_open_llm_no_rows_returns_empty() -> None:
    client = _client(lambda *a, **kw: _ok(json_body={"rows": []}))
    assert _fetch_hf_open_llm(client, top_n=5) == []


def test_paperswithcode_aggregates_multiple_benchmarks() -> None:
    pwc_responses = iter(
        [
            _ok(json_body={"results": [{"model": "ModelX", "metric": {"value": 82.3}}]}),
            _ok(json_body={"results": [{"model": "ModelY", "metric": {"value": 95.1}}]}),
            _ok(json_body={"results": []}),  # empty third benchmark
            _ok(json_body={"results": [{"model": "ModelZ", "metric": {"value": 91.0}}]}),
            _ok(json_body={"results": [{"model": "ModelW", "metric": {"value": 84.0}}]}),
        ]
    )
    client = _client(lambda *a, **kw: next(pwc_responses))
    papers = _fetch_paperswithcode(client, top_n=3)
    assert len(papers) == 1
    text = papers[0].abstract
    assert "ModelX" in text
    assert "ModelY" in text


def test_chatbot_arena_csv_parsing() -> None:
    csv = (
        "Model,Rating\n"
        "ModelA,1280\n"
        "ModelB,1265\n"
        "ModelC,1240\n"
    )
    client = _client(lambda *a, **kw: _ok(text=csv))
    papers = _fetch_chatbot_arena(client, top_n=2)
    assert len(papers) == 1
    assert "ModelA — Arena rating 1280" in papers[0].abstract


def test_one_lane_failure_does_not_break_others() -> None:
    def fake_get(url: str, **kwargs: Any) -> MagicMock:
        if "datasets-server" in url:
            raise httpx.ConnectError("HF down")
        if "paperswithcode" in url:
            return _ok(
                json_body={"results": [{"model": "OkayModel", "metric": {"value": 70.0}}]}
            )
        if "chatbot-arena" in url:
            raise httpx.ConnectError("LMSys down")
        return _ok(json_body={})

    # _fetch_leaderboards opens its own client; patch httpx.Client to return our mock
    fake = MagicMock()
    fake.__enter__ = MagicMock(return_value=fake)
    fake.__exit__ = MagicMock(return_value=False)
    fake.get.side_effect = fake_get

    with patch("mesh_agents.leaderboard_scout.httpx.Client", lambda: fake):
        papers = _fetch_leaderboards(
            lanes=["hf_open_llm", "papers_with_code", "chatbot_arena"], top_n=5
        )

    # Only the working lane (papers_with_code) produces a paper; the broken ones
    # are logged and skipped without raising.
    assert len(papers) == 1
    assert "OkayModel" in papers[0].abstract


def test_unknown_lane_is_logged_not_raised() -> None:
    fake = MagicMock()
    fake.__enter__ = MagicMock(return_value=fake)
    fake.__exit__ = MagicMock(return_value=False)
    fake.get.side_effect = lambda *a, **kw: _ok(json_body={"rows": []})
    with patch("mesh_agents.leaderboard_scout.httpx.Client", lambda: fake):
        papers = _fetch_leaderboards(lanes=["not_a_real_lane"], top_n=5)
    assert papers == []


def test_handle_returns_dict() -> None:
    fake = MagicMock()
    fake.__enter__ = MagicMock(return_value=fake)
    fake.__exit__ = MagicMock(return_value=False)
    fake.get.side_effect = lambda *a, **kw: _ok(json_body={"rows": []})
    with patch("mesh_agents.leaderboard_scout.httpx.Client", lambda: fake):
        out = asyncio.run(_handle_scout_leaderboards({"lanes": ["hf_open_llm"]}))
    assert "papers" in out


@patch("mesh_agents.leaderboard_scout.build_multi_skill_card")
def test_a2a_card_declares_scout_leaderboards_skill(mock_card: MagicMock) -> None:
    LeaderboardScoutAgent().to_a2a_server(url="http://leaderboard-scout:8012")
    kwargs = mock_card.call_args.kwargs
    skill_ids = {s.id for s in kwargs["skills"]}
    assert "scout_leaderboards" in skill_ids
    assert "investigate_leaderboard" in skill_ids
