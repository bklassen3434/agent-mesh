"""Blog scout — RSS/Atom-based ingestion of curated AI/ML blog feeds.

No HTML scraping: every source contributes via its RSS or Atom feed.
The default feed list ships in ``config/blog_feeds.yaml`` and can be
overridden via ``$MESH_BLOG_FEEDS`` (comma-separated URLs) or
``$MESH_BLOG_FEEDS_FILE`` (path to a YAML file with the same shape).

Entries published outside the lookback window
(``MESH_BLOG_LOOKBACK_HOURS``, default 24) are skipped, so each pipeline
run only ingests genuinely new posts.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import mktime
from typing import Any

import feedparser
import yaml
from mesh_a2a.card_builder import build_agent_card
from mesh_a2a.task_server import build_task_app
from mesh_models.source import Source, SourceType
from pydantic import BaseModel
from starlette.applications import Starlette

from mesh_agents.arxiv_scout import ScoutedPaper
from mesh_agents.base import BaseAgent

logger = logging.getLogger(__name__)

_HTTP_TIMEOUT = 20.0
_DEFAULT_LOOKBACK_HOURS = 24
_MAX_ABSTRACT_LEN = 4000


class FeedEntry(BaseModel):
    name: str
    url: str


class ScoutBlogsSkillInput(BaseModel):
    feeds: list[FeedEntry] | None = None
    lookback_hours: int | None = None
    max_results: int = 30


class ScoutBlogsSkillOutput(BaseModel):
    papers: list[dict[str, Any]]


def _make_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _strip_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:_MAX_ABSTRACT_LEN]


def _default_feed_file() -> Path:
    # Repo-relative default: config/blog_feeds.yaml. The agent server may run
    # from a docker container where the repo root is /app, so try both.
    here = Path(__file__).resolve()
    candidates = [
        Path("config/blog_feeds.yaml"),
        here.parent.parent.parent.parent / "config" / "blog_feeds.yaml",
        Path("/app/config/blog_feeds.yaml"),
    ]
    for c in candidates:
        if c.is_file():
            return c
    return candidates[0]


def _load_feeds_from_file(path: Path) -> list[FeedEntry]:
    try:
        data = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError) as exc:
        logger.warning("blog_feeds_load_failed", extra={"path": str(path), "error": str(exc)})
        return []
    feeds = (data or {}).get("feeds") or []
    return [
        FeedEntry(name=str(f.get("name") or f.get("url")), url=str(f["url"]))
        for f in feeds
        if "url" in f
    ]


def _resolve_feeds(explicit: list[FeedEntry] | None) -> list[FeedEntry]:
    if explicit:
        return explicit
    env_urls = os.environ.get("MESH_BLOG_FEEDS")
    if env_urls:
        return [
            FeedEntry(name=u.strip(), url=u.strip())
            for u in env_urls.split(",")
            if u.strip()
        ]
    env_file = os.environ.get("MESH_BLOG_FEEDS_FILE")
    if env_file:
        return _load_feeds_from_file(Path(env_file))
    return _load_feeds_from_file(_default_feed_file())


def _resolve_lookback_hours(explicit: int | None) -> int:
    if explicit is not None:
        return explicit
    env = os.environ.get("MESH_BLOG_LOOKBACK_HOURS")
    if env:
        try:
            return int(env)
        except ValueError:
            pass
    return _DEFAULT_LOOKBACK_HOURS


def _entry_published(entry: Any) -> datetime | None:
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        ts = getattr(entry, key, None) or (entry.get(key) if isinstance(entry, dict) else None)
        if ts:
            try:
                return datetime.fromtimestamp(mktime(ts), UTC)
            except (TypeError, ValueError):
                continue
    return None


def _entry_to_paper(
    feed_name: str, entry: Any, cutoff: datetime
) -> ScoutedPaper | None:
    published = _entry_published(entry)
    if published is None:
        return None
    if published < cutoff:
        return None

    title = (entry.get("title") or "").strip()
    link = (entry.get("link") or "").strip()
    if not title and not link:
        return None

    # feedparser merges content/summary/description into different fields per
    # feed flavor; the body is whichever one has the most text.
    body_candidates: list[str] = []
    summary = entry.get("summary") or ""
    body_candidates.append(_strip_html(summary))
    content_list = entry.get("content") or []
    for c in content_list:
        body_candidates.append(_strip_html(c.get("value", "")))
    abstract = max(body_candidates, key=len) if body_candidates else ""
    if not abstract:
        abstract = title
    if not abstract:
        return None

    author = entry.get("author") or feed_name
    source = Source(
        type=SourceType.blog,
        url=link or f"blog://{feed_name}",
        author=author,
        published_at=published,
        raw_content_hash=_make_hash(abstract),
    )
    title_with_feed = f"{feed_name}: {title}" if title else feed_name
    return ScoutedPaper(
        source=source,
        title=title_with_feed,
        abstract=abstract,
        arxiv_id=f"blog_{_make_hash(link or title)[:12]}",
    )


def _fetch_feed(feed: FeedEntry, cutoff: datetime) -> list[ScoutedPaper]:
    try:
        parsed = feedparser.parse(feed.url)
    except Exception as exc:  # feedparser is robust; bare except is intentional
        logger.warning("blog_feed_parse_failed", extra={"feed": feed.name, "error": str(exc)})
        return []
    if parsed.bozo and not parsed.entries:
        logger.warning(
            "blog_feed_unparseable",
            extra={"feed": feed.name, "error": str(getattr(parsed, "bozo_exception", ""))},
        )
        return []

    out: list[ScoutedPaper] = []
    for entry in parsed.entries:
        paper = _entry_to_paper(feed.name, entry, cutoff)
        if paper is not None:
            out.append(paper)
    return out


def _fetch_blogs(
    feeds: list[FeedEntry], lookback_hours: int, max_results: int
) -> list[ScoutedPaper]:
    cutoff = datetime.now(UTC) - timedelta(hours=lookback_hours)
    seen: set[str] = set()
    out: list[ScoutedPaper] = []
    for feed in feeds:
        for paper in _fetch_feed(feed, cutoff):
            if paper.source.raw_content_hash in seen:
                continue
            seen.add(paper.source.raw_content_hash)
            out.append(paper)
            if len(out) >= max_results:
                return out
    return out


async def _handle_scout_blogs(payload: dict[str, Any]) -> dict[str, Any]:
    skill_input = ScoutBlogsSkillInput.model_validate(payload)
    feeds = _resolve_feeds(skill_input.feeds)
    lookback = _resolve_lookback_hours(skill_input.lookback_hours)
    papers = await asyncio.to_thread(
        _fetch_blogs, feeds, lookback, skill_input.max_results
    )
    return ScoutBlogsSkillOutput(
        papers=[p.model_dump(mode="json") for p in papers]
    ).model_dump(mode="json")


class BlogScoutAgent(BaseAgent):
    name = "blog_scout"

    def __init__(self, llm: Any | None = None, db_conn: Any | None = None) -> None:
        super().__init__(llm=llm, db_conn=db_conn)

    async def run(self, input: BaseModel) -> ScoutBlogsSkillOutput:  # pragma: no cover
        raise NotImplementedError("BlogScoutAgent uses the A2A skill path only")

    def to_a2a_server(self, url: str) -> Starlette:
        card = build_agent_card(
            name="Blog Scout",
            description="Ingests AI/ML blog posts from curated RSS/Atom feeds.",
            url=url,
            skill_id="scout_blogs",
            skill_name="Scout Blogs",
            skill_description=(
                "Pull recent entries (within MESH_BLOG_LOOKBACK_HOURS) from each feed "
                "in MESH_BLOG_FEEDS or the YAML feed file at MESH_BLOG_FEEDS_FILE."
            ),
            skill_tags=["blogs", "rss", "atom"],
        )
        return build_task_app(
            agent_card=card,
            skill_handlers={"scout_blogs": _handle_scout_blogs},
            agent_name="blog_scout",
        )
