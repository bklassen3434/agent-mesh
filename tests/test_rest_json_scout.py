from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
from mesh_agents.rest_json_scout import (
    ScoutRestJsonSkillInput,
    _build_url,
    _dig,
    _fetch_rest,
    _handle_scout_rest_json,
    _parse_published,
)
from mesh_models.source import SourceType

_CLIENT = "mesh_agents.rest_json_scout.httpx.Client"


def test_dig_nested_and_index() -> None:
    obj = {"data": {"results": [{"title": "a"}, {"title": "b"}]}}
    assert _dig(obj, "data.results.1.title") == "b"
    assert _dig(obj, "data.missing") is None
    assert _dig(obj, "") is obj


def test_parse_published_epoch_and_iso() -> None:
    assert _parse_published(0) == datetime(1970, 1, 1, tzinfo=UTC)
    iso = _parse_published("2024-01-02T03:04:05Z")
    assert iso is not None and iso.tzinfo is not None
    assert _parse_published("not a date") is None


def test_build_url() -> None:
    assert _build_url("https://x/api", "") == "https://x/api"
    assert _build_url("https://x/api", "q=ai") == "https://x/api?q=ai"
    assert _build_url("https://x/api?p=1", "q=ai") == "https://x/api?p=1&q=ai"
    assert _build_url("https://x/api", "?q=ai") == "https://x/api?q=ai"


def _patch_client(payload: Any) -> Any:
    """Patch httpx.Client so a GET returns ``payload`` as JSON."""
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value=payload)
    client = MagicMock()
    client.get = MagicMock(return_value=resp)
    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=client)
    ctx.__exit__ = MagicMock(return_value=False)
    return patch(_CLIENT, return_value=ctx)


def _cfg(**kw: Any) -> ScoutRestJsonSkillInput:
    base: dict[str, Any] = {
        "endpoint": "https://api.example.com/items",
        "items_path": "results",
        "title_path": "name",
        "text_path": "body",
        "url_path": "link",
        "published_path": "ts",
    }
    base.update(kw)
    return ScoutRestJsonSkillInput.model_validate(base)


def _item(name: str, link: str, ts: str | None = None) -> dict[str, Any]:
    item: dict[str, Any] = {"name": name, "body": f"Body of {name}", "link": link}
    if ts is not None:
        item["ts"] = ts
    return item


def test_fetch_rest_maps_items() -> None:
    payload = {
        "results": [
            _item("Item A", "https://x/a", "2024-05-01T00:00:00Z"),
            _item("Item B", "https://x/b", "2024-05-02T00:00:00Z"),
        ]
    }
    with _patch_client(payload):
        papers = _fetch_rest(_cfg(), None)
    assert len(papers) == 2
    assert papers[0].source.type == SourceType.rest
    assert papers[0].title == "Item A"
    assert papers[0].source.url == "https://x/a"
    assert papers[0].arxiv_id.startswith("rest_")


def test_missing_url_synthesizes_stable_id() -> None:
    payload = {"results": [{"name": "T", "body": "Body text"}]}
    with _patch_client(payload):
        papers = _fetch_rest(_cfg(), None)
    assert len(papers) == 1
    assert papers[0].source.url.startswith("rest://https://api.example.com/items#")


def test_since_cutoff_filters() -> None:
    old = (datetime.now(UTC) - timedelta(days=10)).isoformat()
    payload = {"results": [_item("Old", "https://x/old", old)]}
    cutoff = datetime.now(UTC) - timedelta(days=1)
    with _patch_client(payload):
        papers = _fetch_rest(_cfg(), cutoff)
    assert papers == []


def test_items_not_list_returns_empty() -> None:
    payload = {"results": {"not": "a list"}}
    with _patch_client(payload):
        papers = _fetch_rest(_cfg(), None)
    assert papers == []


def test_http_error_returns_empty() -> None:
    with patch(_CLIENT, side_effect=httpx.ConnectError("down")):
        papers = _fetch_rest(_cfg(), None)
    assert papers == []


def test_top_level_list_with_empty_items_path() -> None:
    payload = [_item("X", "https://x/x")]
    with _patch_client(payload):
        papers = _fetch_rest(_cfg(items_path=""), None)
    assert len(papers) == 1


def test_handle_scout_rest_json_shape() -> None:
    payload = {"results": [_item("T", "https://x/t")]}
    with _patch_client(payload):
        out = asyncio.run(
            _handle_scout_rest_json(
                {
                    "endpoint": "https://api.example.com/items",
                    "items_path": "results",
                    "title_path": "name",
                    "text_path": "body",
                    "url_path": "link",
                }
            )
        )
    assert out["papers"][0]["source"]["type"] == "rest"
