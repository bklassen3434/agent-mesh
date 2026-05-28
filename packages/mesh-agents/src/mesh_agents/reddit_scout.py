"""Reddit scout — top posts of the day from AI/ML subreddits.

Reddit's API is free with a reasonable rate limit (60 req/min) via OAuth2
client credentials. Requires REDDIT_CLIENT_ID and REDDIT_CLIENT_SECRET in
the environment. Without creds, the scout returns an empty result and
logs a single warning; the rest of the pipeline keeps running.

Top posts of the configured ``listing`` window (default ``day``) for each
subreddit in ``MESH_REDDIT_SUBS``. Each post becomes a ScoutedPaper with
``source.type=reddit``.
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

_DEFAULT_SUBS = ["MachineLearning", "LocalLLaMA", "singularity", "artificial"]
_TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
_OAUTH_BASE = "https://oauth.reddit.com"
_USER_AGENT = "agent-mesh:v0.5 (by /u/agent-mesh)"
_HTTP_TIMEOUT = 15.0
_MIN_SCORE = 20


class ScoutRedditSkillInput(BaseModel):
    subreddits: list[str] | None = None
    listing: str = "day"  # hour, day, week, month, year, all
    max_results: int = 20
    min_score: int = _MIN_SCORE


class ScoutRedditSkillOutput(BaseModel):
    papers: list[dict[str, Any]]


class _RedditCredsMissing(RuntimeError):
    pass


def _make_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _normalize_sub(name: str) -> str:
    s = name.strip()
    for prefix in ("/r/", "r/"):
        if s.startswith(prefix):
            s = s[len(prefix):]
    return s


def _resolve_subs(explicit: list[str] | None) -> list[str]:
    if explicit:
        return explicit
    env = os.environ.get("MESH_REDDIT_SUBS")
    if env:
        return [_normalize_sub(s) for s in env.split(",") if s.strip()]
    return list(_DEFAULT_SUBS)


def _get_creds() -> tuple[str, str]:
    cid = os.environ.get("REDDIT_CLIENT_ID", "").strip()
    csec = os.environ.get("REDDIT_CLIENT_SECRET", "").strip()
    if not cid or not csec:
        raise _RedditCredsMissing("REDDIT_CLIENT_ID/REDDIT_CLIENT_SECRET not set")
    return cid, csec


def _get_token(client: httpx.Client) -> str:
    cid, csec = _get_creds()
    resp = client.post(
        _TOKEN_URL,
        data={"grant_type": "client_credentials"},
        auth=(cid, csec),
        headers={"User-Agent": _USER_AGENT},
        timeout=_HTTP_TIMEOUT,
    )
    resp.raise_for_status()
    token = resp.json().get("access_token")
    if not token:
        raise RuntimeError("Reddit returned no access_token")
    return str(token)


def _fetch_sub(
    client: httpx.Client,
    token: str,
    subreddit: str,
    listing: str,
    per_sub: int,
    min_score: int,
) -> list[ScoutedPaper]:
    try:
        resp = client.get(
            f"{_OAUTH_BASE}/r/{subreddit}/top",
            params={"t": listing, "limit": per_sub},
            headers={"Authorization": f"Bearer {token}", "User-Agent": _USER_AGENT},
            timeout=_HTTP_TIMEOUT,
        )
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        logger.warning(
            "reddit_sub_fetch_failed",
            extra={"sub": subreddit, "error": str(exc)},
        )
        return []

    children = (resp.json().get("data") or {}).get("children", [])
    out: list[ScoutedPaper] = []
    for child in children:
        data = child.get("data") or {}
        score = int(data.get("score") or 0)
        if score < min_score:
            continue

        title = (data.get("title") or "").strip()
        selftext = (data.get("selftext") or "").strip()
        permalink = data.get("permalink")
        url = f"https://www.reddit.com{permalink}" if permalink else data.get("url", "")
        author = data.get("author")
        created_utc = data.get("created_utc")
        try:
            published_at = (
                datetime.fromtimestamp(float(created_utc), UTC)
                if created_utc
                else datetime.now(UTC)
            )
        except (TypeError, ValueError):
            published_at = datetime.now(UTC)

        abstract = selftext or title
        if not abstract:
            continue
        source = Source(
            type=SourceType.reddit,
            url=url,
            author=author,
            published_at=published_at,
            raw_content_hash=_make_hash(abstract),
        )
        out.append(
            ScoutedPaper(
                source=source,
                title=title or url,
                abstract=abstract,
                arxiv_id=f"reddit_{data.get('id') or _make_hash(abstract)[:12]}",
            )
        )
    return out


def _fetch_reddit(
    subreddits: list[str],
    listing: str,
    max_results: int,
    min_score: int,
) -> list[ScoutedPaper]:
    per_sub = max(max_results // max(len(subreddits), 1), 1)
    seen: set[str] = set()
    out: list[ScoutedPaper] = []
    with httpx.Client() as client:
        try:
            token = _get_token(client)
        except _RedditCredsMissing as exc:
            logger.warning("reddit_scout_disabled", extra={"reason": str(exc)})
            return []
        except httpx.HTTPError as exc:
            logger.warning("reddit_token_failed", extra={"error": str(exc)})
            return []

        for sub in subreddits:
            for paper in _fetch_sub(client, token, sub, listing, per_sub, min_score):
                if paper.source.raw_content_hash in seen:
                    continue
                seen.add(paper.source.raw_content_hash)
                out.append(paper)
                if len(out) >= max_results:
                    return out
    return out


async def _handle_scout_reddit(payload: dict[str, Any]) -> dict[str, Any]:
    skill_input = ScoutRedditSkillInput.model_validate(payload)
    subs = _resolve_subs(skill_input.subreddits)
    papers = await asyncio.to_thread(
        _fetch_reddit,
        subs,
        skill_input.listing,
        skill_input.max_results,
        skill_input.min_score,
    )
    return ScoutRedditSkillOutput(
        papers=[p.model_dump(mode="json") for p in papers]
    ).model_dump(mode="json")


class RedditScoutAgent(BaseAgent):
    name = "reddit_scout"

    def __init__(self, llm: Any | None = None, db_conn: Any | None = None) -> None:
        super().__init__(llm=llm, db_conn=db_conn)

    async def run(self, input: BaseModel) -> ScoutRedditSkillOutput:  # pragma: no cover
        raise NotImplementedError("RedditScoutAgent uses the A2A skill path only")

    def to_a2a_server(self, url: str) -> Starlette:
        card = build_multi_skill_card(
            name="Reddit Scout",
            description="Fetches top posts from AI/ML subreddits via Reddit's OAuth2 API.",
            url=url,
            skills=[
                SkillSpec(
                    id="scout_reddit",
                    name="Scout Reddit",
                    description=(
                        "Top posts of the configured listing window for each subreddit "
                        "in MESH_REDDIT_SUBS. Requires REDDIT_CLIENT_ID/REDDIT_CLIENT_SECRET."
                    ),
                    tags=["reddit", "social", "posts"],
                ),
                investigate_skill_spec("reddit"),
            ],
        )
        return build_task_app(
            agent_card=card,
            skill_handlers={
                "scout_reddit": _handle_scout_reddit,
                "investigate_reddit": make_empty_investigate_handler("reddit"),
            },
            agent_name="reddit_scout",
        )
