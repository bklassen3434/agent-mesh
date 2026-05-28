"""Bluesky scout — surfaces AI/ML posts from the public AppView API.

Bluesky's AppView is unauthenticated for read endpoints, so no creds are
needed. The scout fetches in two lanes:

* ``searchPosts`` by hashtag (``#ai``, ``#ml``, ``#llm`` by default)
* ``getAuthorFeed`` per handle in an optional curated list

Both produce ``ScoutedPaper``-style records with ``source.type=bluesky``.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from datetime import UTC, datetime
from typing import Any

import httpx
from mesh_a2a.card_builder import SkillSpec, build_multi_skill_card
from mesh_a2a.task_server import build_task_app
from mesh_models.source import Source, SourceType
from pydantic import BaseModel
from starlette.applications import Starlette

from mesh_agents.arxiv_scout import ScoutedPaper
from mesh_agents.base import BaseAgent
from mesh_agents.investigation import (
    investigate_skill_spec,
    make_empty_investigate_handler,
)

logger = logging.getLogger(__name__)

_DEFAULT_HASHTAGS = ["ai", "ml", "llm"]
_PUBLIC_API = "https://public.api.bsky.app"
_HTTP_TIMEOUT = 15.0
_MIN_TEXT_LEN = 40  # filter out trivial posts


class ScoutBlueskySkillInput(BaseModel):
    handles: list[str] | None = None
    hashtags: list[str] | None = None
    max_results: int = 20
    min_text_len: int = _MIN_TEXT_LEN


class ScoutBlueskySkillOutput(BaseModel):
    papers: list[dict[str, Any]]


def _make_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _resolve_handles(explicit: list[str] | None) -> list[str]:
    if explicit:
        return explicit
    env = os.environ.get("MESH_BLUESKY_HANDLES")
    if env:
        return [h.strip() for h in env.split(",") if h.strip()]
    return []


def _resolve_hashtags(explicit: list[str] | None) -> list[str]:
    if explicit:
        return explicit
    env = os.environ.get("MESH_BLUESKY_HASHTAGS")
    if env:
        return [t.strip().lstrip("#") for t in env.split(",") if t.strip()]
    return list(_DEFAULT_HASHTAGS)


def _post_to_paper(post: dict[str, Any], min_text_len: int) -> ScoutedPaper | None:
    """Convert one feed/search post into a ScoutedPaper, or None if too short."""
    record = post.get("record") or {}
    text = (record.get("text") or "").strip()
    if len(text) < min_text_len:
        return None

    author = post.get("author") or {}
    handle = author.get("handle") or "unknown"
    uri = post.get("uri", "")
    # at:// → https permalink: at://did:.../app.bsky.feed.post/<rkey>
    rkey = uri.rsplit("/", 1)[-1] if uri else ""
    permalink = f"https://bsky.app/profile/{handle}/post/{rkey}" if rkey else uri

    created_raw = record.get("createdAt") or post.get("indexedAt")
    try:
        published_at = (
            datetime.fromisoformat(created_raw.replace("Z", "+00:00"))
            if created_raw
            else datetime.now(UTC)
        )
    except (ValueError, AttributeError):
        published_at = datetime.now(UTC)

    source = Source(
        type=SourceType.bluesky,
        url=permalink,
        author=handle,
        published_at=published_at,
        raw_content_hash=_make_hash(text),
    )
    return ScoutedPaper(
        source=source,
        title=text[:80],
        abstract=text,
        arxiv_id=f"bsky_{rkey or _make_hash(text)[:12]}",
    )


def _fetch_hashtag(
    client: httpx.Client, hashtag: str, per_tag: int, min_text_len: int
) -> list[ScoutedPaper]:
    try:
        resp = client.get(
            f"{_PUBLIC_API}/xrpc/app.bsky.feed.searchPosts",
            params={"q": f"#{hashtag}", "limit": per_tag},
            timeout=_HTTP_TIMEOUT,
        )
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        logger.warning(
            "bluesky_hashtag_fetch_failed",
            extra={"hashtag": hashtag, "error": str(exc)},
        )
        return []
    posts = resp.json().get("posts", [])
    out: list[ScoutedPaper] = []
    for post in posts:
        paper = _post_to_paper(post, min_text_len)
        if paper is not None:
            out.append(paper)
    return out


def _fetch_author(
    client: httpx.Client, handle: str, per_author: int, min_text_len: int
) -> list[ScoutedPaper]:
    try:
        resp = client.get(
            f"{_PUBLIC_API}/xrpc/app.bsky.feed.getAuthorFeed",
            params={"actor": handle, "limit": per_author},
            timeout=_HTTP_TIMEOUT,
        )
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        logger.warning(
            "bluesky_author_fetch_failed",
            extra={"handle": handle, "error": str(exc)},
        )
        return []
    feed = resp.json().get("feed", [])
    out: list[ScoutedPaper] = []
    for item in feed:
        post = item.get("post") or {}
        paper = _post_to_paper(post, min_text_len)
        if paper is not None:
            out.append(paper)
    return out


def _fetch_bluesky(
    handles: list[str],
    hashtags: list[str],
    max_results: int,
    min_text_len: int,
) -> list[ScoutedPaper]:
    sources_n = max(len(handles) + len(hashtags), 1)
    per_source = max(max_results // sources_n, 1)

    seen: set[str] = set()
    out: list[ScoutedPaper] = []
    with httpx.Client() as client:
        for tag in hashtags:
            for paper in _fetch_hashtag(client, tag, per_source, min_text_len):
                if paper.source.raw_content_hash in seen:
                    continue
                seen.add(paper.source.raw_content_hash)
                out.append(paper)
                if len(out) >= max_results:
                    return out
        for handle in handles:
            for paper in _fetch_author(client, handle, per_source, min_text_len):
                if paper.source.raw_content_hash in seen:
                    continue
                seen.add(paper.source.raw_content_hash)
                out.append(paper)
                if len(out) >= max_results:
                    return out
    return out


async def _handle_scout_bluesky(payload: dict[str, Any]) -> dict[str, Any]:
    skill_input = ScoutBlueskySkillInput.model_validate(payload)
    handles = _resolve_handles(skill_input.handles)
    hashtags = _resolve_hashtags(skill_input.hashtags)
    papers = await asyncio.to_thread(
        _fetch_bluesky,
        handles,
        hashtags,
        skill_input.max_results,
        skill_input.min_text_len,
    )
    return ScoutBlueskySkillOutput(
        papers=[p.model_dump(mode="json") for p in papers]
    ).model_dump(mode="json")


class BlueskyScoutAgent(BaseAgent):
    name = "bluesky_scout"

    def __init__(self, llm: Any | None = None, db_conn: Any | None = None) -> None:
        super().__init__(llm=llm, db_conn=db_conn)

    async def run(self, input: BaseModel) -> ScoutBlueskySkillOutput:  # pragma: no cover
        raise NotImplementedError("BlueskyScoutAgent uses the A2A skill path only")

    def to_a2a_server(self, url: str) -> Starlette:
        card = build_multi_skill_card(
            name="Bluesky Scout",
            description="Fetches AI/ML posts from Bluesky's public AppView API.",
            url=url,
            skills=[
                SkillSpec(
                    id="scout_bluesky",
                    name="Scout Bluesky",
                    description=(
                        "Search Bluesky by hashtag and (optionally) by curated author "
                        "handles via the public unauthenticated AppView."
                    ),
                    tags=["bluesky", "social", "posts"],
                ),
                investigate_skill_spec("bluesky"),
            ],
        )
        return build_task_app(
            agent_card=card,
            skill_handlers={
                "scout_bluesky": _handle_scout_bluesky,
                "investigate_bluesky": make_empty_investigate_handler("bluesky"),
            },
            agent_name="bluesky_scout",
        )
