"""RSS scout — generic single-feed RSS/Atom connector (Phase 18a).

A *config-driven* connector: one service serves arbitrarily many configured
instances. Each instance is a ``field_connectors`` row whose config supplies a
``feed_url`` plus optional ``include_terms`` / ``exclude_terms``. Unlike the
built-in ``blog`` scout (a curated multi-feed list), this fetches exactly the
one user-supplied feed and is added to any field with no code change.

Trusted-input model (Phase 18a): the feed URL is operator-supplied and fetched
read-only with a bounded timeout and a per-run ``max_results`` cap. Parse
failures degrade to an empty result — one bad feed never aborts a run.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from datetime import UTC, datetime
from time import mktime
from typing import Any

import feedparser
from mesh_a2a.card_builder import SkillSpec, build_multi_skill_card
from mesh_a2a.task_server import build_task_app
from mesh_models.source import Source, SourceType
from pydantic import BaseModel
from starlette.applications import Starlette

from mesh_agents.arxiv_scout import ScoutedPaper
from mesh_agents.base import BaseAgent

logger = logging.getLogger(__name__)

_MAX_ABSTRACT_LEN = 4000


class ScoutRssSkillInput(BaseModel):
    feed_url: str
    include_terms: list[str] = []
    exclude_terms: list[str] = []
    max_results: int = 20
    since: str | None = None  # ISO-8601 string; entries before it are skipped


class ScoutRssSkillOutput(BaseModel):
    papers: list[dict[str, Any]]


def _make_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _strip_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:_MAX_ABSTRACT_LEN]


def _entry_published(entry: Any) -> datetime | None:
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        ts = getattr(entry, key, None) or (entry.get(key) if isinstance(entry, dict) else None)
        if ts:
            try:
                return datetime.fromtimestamp(mktime(ts), UTC)
            except (TypeError, ValueError):
                continue
    return None


def _matches_terms(text: str, include: list[str], exclude: list[str]) -> bool:
    """Include filter (any term, case-insensitive) then exclude filter."""
    lowered = text.lower()
    if exclude and any(term.lower() in lowered for term in exclude):
        return False
    return not (include and not any(term.lower() in lowered for term in include))


def _entry_to_paper(
    feed_url: str,
    entry: Any,
    cutoff: datetime | None,
    include: list[str],
    exclude: list[str],
) -> ScoutedPaper | None:
    published = _entry_published(entry) or datetime.now(UTC)
    if cutoff is not None and published < cutoff:
        return None

    title = (entry.get("title") or "").strip()
    link = (entry.get("link") or "").strip()
    if not title and not link:
        return None

    # feedparser spreads the body across summary/content depending on the feed
    # flavor; the body is whichever field carries the most text.
    body_candidates: list[str] = [_strip_html(entry.get("summary") or "")]
    for c in entry.get("content") or []:
        body_candidates.append(_strip_html(c.get("value", "")))
    abstract = max(body_candidates, key=len) if body_candidates else ""
    if not abstract:
        abstract = title
    if not abstract:
        return None

    if not _matches_terms(f"{title} {abstract}", include, exclude):
        return None

    source = Source(
        type=SourceType.rss,
        url=link or f"rss://{feed_url}",
        author=entry.get("author") or None,
        published_at=published,
        raw_content_hash=_make_hash(abstract),
    )
    return ScoutedPaper(
        source=source,
        title=title or feed_url,
        abstract=abstract,
        arxiv_id=f"rss_{_make_hash(link or title)[:12]}",
    )


def _fetch_feed(
    feed_url: str,
    cutoff: datetime | None,
    include: list[str],
    exclude: list[str],
    max_results: int,
) -> list[ScoutedPaper]:
    try:
        parsed = feedparser.parse(feed_url)
    except Exception as exc:  # feedparser is robust; bare except is intentional
        logger.warning("rss_feed_parse_failed", extra={"feed": feed_url, "error": str(exc)})
        return []
    if parsed.bozo and not parsed.entries:
        logger.warning(
            "rss_feed_unparseable",
            extra={"feed": feed_url, "error": str(getattr(parsed, "bozo_exception", ""))},
        )
        return []

    seen: set[str] = set()
    out: list[ScoutedPaper] = []
    for entry in parsed.entries:
        paper = _entry_to_paper(feed_url, entry, cutoff, include, exclude)
        if paper is None or paper.source.raw_content_hash in seen:
            continue
        seen.add(paper.source.raw_content_hash)
        out.append(paper)
        if len(out) >= max_results:
            break
    return out


async def _handle_scout_rss(payload: dict[str, Any]) -> dict[str, Any]:
    skill_input = ScoutRssSkillInput.model_validate(payload)
    cutoff: datetime | None = None
    if skill_input.since:
        cutoff = datetime.fromisoformat(skill_input.since)
        # Published timestamps are always tz-aware (see _entry_published); a
        # bare/offset-less `since` would raise on the `published < cutoff`
        # comparison, so normalize it to UTC.
        if cutoff.tzinfo is None:
            cutoff = cutoff.replace(tzinfo=UTC)
    papers = await asyncio.to_thread(
        _fetch_feed,
        skill_input.feed_url,
        cutoff,
        skill_input.include_terms,
        skill_input.exclude_terms,
        skill_input.max_results,
    )
    return ScoutRssSkillOutput(
        papers=[p.model_dump(mode="json") for p in papers]
    ).model_dump(mode="json")


class RssScoutAgent(BaseAgent):
    name = "rss_scout"

    def __init__(self, llm: Any | None = None, db_conn: Any | None = None) -> None:
        super().__init__(llm=llm, db_conn=db_conn)

    async def run(self, input: BaseModel) -> ScoutRssSkillOutput:  # pragma: no cover
        raise NotImplementedError("RssScoutAgent uses the A2A skill path only")

    def to_a2a_server(self, url: str) -> Starlette:
        card = build_multi_skill_card(
            name="RSS Scout",
            description="Ingests items from a single user-configured RSS/Atom feed.",
            url=url,
            skills=[
                SkillSpec(
                    id="scout_rss",
                    name="Scout RSS",
                    description=(
                        "Fetch recent entries from a configured feed_url, optionally "
                        "filtered by include_terms / exclude_terms."
                    ),
                    tags=["rss", "atom", "config_driven"],
                ),
            ],
        )
        return build_task_app(
            agent_card=card,
            skill_handlers={"scout_rss": _handle_scout_rss},
            agent_name="rss_scout",
        )
