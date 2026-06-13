from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
from mesh_agents.web_search_scout import (
    _handle_investigate_web,
    _handle_scout_web_search,
    _result_abstract,
    _result_published,
    _result_to_paper,
    _search,
)
from mesh_models.source import SourceType

_CLIENT = "mesh_agents.web_search_scout.httpx.Client"


def _result(
    url: str = "https://example.com/a",
    title: str = "Result A",
    description: str = "A snippet about robotics.",
    extra: list[str] | None = None,
    page_age: str | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {"url": url, "title": title, "description": description}
    if extra is not None:
        out["extra_snippets"] = extra
    if page_age is not None:
        out["page_age"] = page_age
    return out


def test_result_abstract_joins_snippets() -> None:
    abstract = _result_abstract(_result(description="Main.", extra=["Extra one.", "Extra two."]))
    assert "Main." in abstract and "Extra one." in abstract and "Extra two." in abstract


def test_result_published_parses_page_age() -> None:
    dt = _result_published(_result(page_age="2024-03-04T05:06:07Z"))
    assert dt.year == 2024 and dt.tzinfo is not None


def test_result_published_defaults_now() -> None:
    before = datetime.now(UTC) - timedelta(seconds=1)
    dt = _result_published(_result())
    assert dt >= before


def test_result_to_paper_shape() -> None:
    paper = _result_to_paper(_result(), None)
    assert paper is not None
    assert paper.source.type == SourceType.web
    assert paper.source.url == "https://example.com/a"
    assert paper.arxiv_id.startswith("web_")


def test_result_without_url_skipped() -> None:
    assert _result_to_paper(_result(url=""), None) is None


def _patch_client(results: list[dict[str, Any]]) -> Any:
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value={"web": {"results": results}})
    client = MagicMock()
    client.get = MagicMock(return_value=resp)
    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=client)
    ctx.__exit__ = MagicMock(return_value=False)
    return patch(_CLIENT, return_value=ctx)


def test_search_requires_api_key() -> None:
    with patch.dict("os.environ", {}, clear=True):
        assert _search(["robots"], 10, None) == []


def test_search_dedupes_across_queries() -> None:
    results = [_result(url="https://x/a"), _result(url="https://x/a"), _result(url="https://x/b")]
    with patch.dict("os.environ", {"BRAVE_API_KEY": "k"}), _patch_client(results):
        papers = _search(["q1", "q2"], 10, None)
    urls = {p.source.url for p in papers}
    assert urls == {"https://x/a", "https://x/b"}


def test_search_respects_max_results() -> None:
    results = [_result(url=f"https://x/{i}") for i in range(10)]
    with patch.dict("os.environ", {"BRAVE_API_KEY": "k"}), _patch_client(results):
        papers = _search(["q"], 3, None)
    assert len(papers) == 3


def test_search_http_error_returns_empty() -> None:
    with patch.dict("os.environ", {"BRAVE_API_KEY": "k"}), patch(
        _CLIENT
    ) as client_cls:
        client = MagicMock()
        client.get = MagicMock(side_effect=httpx.ConnectError("down"))
        client_cls.return_value.__enter__ = MagicMock(return_value=client)
        client_cls.return_value.__exit__ = MagicMock(return_value=False)
        papers = _search(["q"], 10, None)
    assert papers == []


def test_handle_scout_web_search_shape() -> None:
    with patch.dict("os.environ", {"BRAVE_API_KEY": "k"}), _patch_client([_result()]):
        out = asyncio.run(
            _handle_scout_web_search({"web_seed_queries": ["robots"], "max_results": 5})
        )
    assert out["papers"][0]["source"]["type"] == "web"


def test_handle_investigate_web_shape() -> None:
    with patch.dict("os.environ", {"BRAVE_API_KEY": "k"}), _patch_client([_result()]):
        out = asyncio.run(
            _handle_investigate_web(
                {"investigation_id": "inv-1", "hypothesis": "are robots improving?"}
            )
        )
    assert out["investigation_id"] == "inv-1"
    assert out["source_records"][0]["source"]["type"] == "web"
