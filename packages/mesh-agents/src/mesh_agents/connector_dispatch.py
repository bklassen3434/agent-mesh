"""In-process connector dispatch — the market's source-acquisition path.

The coordinator scouts by calling each connector's A2A scout server over HTTP.
The market does not run the A2A fleet, so it calls the same scout *handlers*
(``_handle_scout_<slug>``) in-process. Every handler is the canonical connector
implementation (the ``SourceConnector`` protocol): given a per-field config dict
plus a ``max_results`` cap and optional ``since`` window, it returns
``{"papers": [ScoutedPaper, ...]}``. This module maps a connector slug to its
handler and adapts the result to ``list[ScoutedPaper]``.

Handlers are imported lazily (per call) so a missing optional scraping dependency
for one connector never breaks dispatch for the others.
"""
from __future__ import annotations

import importlib
from collections.abc import Awaitable, Callable
from typing import Any

from mesh_agents.arxiv_scout import ScoutedPaper

# connector slug → (module, handler attr). Mirrors each scout A2A server's
# ``skill_handlers`` registration, resolved in-process. Keyed by the connector
# slug (``mesh_models.connector`` / ``catalog.connectors``), which is what a
# field's enabled connectors are stored under.
_HANDLERS: dict[str, tuple[str, str]] = {
    "arxiv": ("mesh_agents.arxiv_scout", "_handle_scout_arxiv"),
    "hn": ("mesh_agents.hn_scout", "_handle_scout_hn"),
    "github": ("mesh_agents.github_scout", "_handle_scout_github"),
    "bluesky": ("mesh_agents.bluesky_scout", "_handle_scout_bluesky"),
    "reddit": ("mesh_agents.reddit_scout", "_handle_scout_reddit"),
    "blog": ("mesh_agents.blog_scout", "_handle_scout_blogs"),
    "leaderboard": ("mesh_agents.leaderboard_scout", "_handle_scout_leaderboards"),
    "web_search": ("mesh_agents.web_search_scout", "_handle_scout_web_search"),
    "rss": ("mesh_agents.rss_scout", "_handle_scout_rss"),
    "rest_json": ("mesh_agents.rest_json_scout", "_handle_scout_rest_json"),
}


def has_connector(connector_id: str) -> bool:
    """Whether an in-process scout handler is available for this connector."""
    return connector_id in _HANDLERS


def connector_ids() -> list[str]:
    return list(_HANDLERS)


async def scout_connector(
    connector_id: str,
    *,
    config: dict[str, Any],
    max_results: int,
    since: str | None = None,
) -> list[ScoutedPaper]:
    """Run one connector in-process and return its scouted papers.

    ``config`` is the connector's per-field config (search terms etc.); the
    handler validates it (extra keys are ignored). Returns ``[]`` for a connector
    with no in-process handler."""
    spec = _HANDLERS.get(connector_id)
    if spec is None:
        return []
    module = importlib.import_module(spec[0])
    handler: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]] = getattr(
        module, spec[1]
    )
    payload: dict[str, Any] = {**config, "max_results": max_results}
    if since is not None:
        payload["since"] = since
    result = await handler(payload)
    return [ScoutedPaper.model_validate(p) for p in result.get("papers", [])]
