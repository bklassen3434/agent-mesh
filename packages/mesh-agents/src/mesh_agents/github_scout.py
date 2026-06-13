"""GitHub scout — surfaces high-signal ML/AI work from two distinct lanes.

GitHub does not expose an official "trending" API, so we use the search
API with ``topic:`` filters sorted by stars over a recent window — that
captures the same signal more reliably than scraping the trending HTML
page. Separately, the scout fetches release notes for a watchlist of
repos via their ``releases.atom`` feed, since released versions of
foundational tools (Transformers, vLLM, llama.cpp, …) drive a lot of
the field-wide discussion.

Both lanes produce ``ScoutedPaper``-shaped records with
``source.type=github``, so downstream extraction is unchanged.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
from datetime import UTC, datetime
from typing import Any
from xml.etree import ElementTree as ET

import httpx
from mesh_a2a.card_builder import SkillSpec, build_multi_skill_card
from mesh_a2a.task_server import build_task_app
from mesh_models.source import Source, SourceType
from pydantic import BaseModel
from starlette.applications import Starlette

from mesh_agents.arxiv_scout import ScoutedPaper
from mesh_agents.base import BaseAgent
from mesh_agents.investigation import (
    InvestigateSkillInput,
    InvestigateSkillOutput,
    investigate_skill_spec,
    keywords_from_hypothesis,
)

logger = logging.getLogger(__name__)

_DEFAULT_TOPICS = ["llm", "agents", "machine-learning", "ai", "robotics"]
_DEFAULT_TRENDING_MAX = 10
_GITHUB_API = "https://api.github.com"
_HTTP_TIMEOUT = 20.0


class ScoutGithubSkillInput(BaseModel):
    topics: list[str] | None = None
    max_results: int = _DEFAULT_TRENDING_MAX
    # Comma-separated owner/repo list; overrides $MESH_GITHUB_WATCHLIST.
    watchlist: list[str] | None = None
    # Pushed-after window for the trending search. ISO-8601, optional.
    since: str | None = None


class ScoutGithubSkillOutput(BaseModel):
    papers: list[dict[str, Any]]


# ── helpers ────────────────────────────────────────────────────────────────


def _make_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _resolve_topics(explicit: list[str] | None) -> list[str]:
    if explicit:
        return explicit
    env = os.environ.get("MESH_GITHUB_TOPICS")
    if env:
        return [t.strip() for t in env.split(",") if t.strip()]
    return list(_DEFAULT_TOPICS)


def _resolve_watchlist(explicit: list[str] | None) -> list[str]:
    if explicit:
        return explicit
    env = os.environ.get("MESH_GITHUB_WATCHLIST")
    if env:
        return [r.strip() for r in env.split(",") if r.strip()]
    return []


def _auth_headers() -> dict[str, str]:
    token = os.environ.get("GITHUB_TOKEN")
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "agent-mesh"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _strip_markdown(text: str, limit: int = 2000) -> str:
    """Cheap markdown cleanup so the abstract is LLM-friendly without bloat."""
    text = re.sub(r"```.*?```", " ", text, flags=re.DOTALL)
    text = re.sub(r"!\[[^\]]*\]\([^)]+\)", " ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


# ── trending lane ──────────────────────────────────────────────────────────


def _build_trending_query(topics: list[str], since: datetime | None) -> str:
    topic_clause = " ".join(f"topic:{t}" for t in topics)
    if since:
        pushed = since.strftime("%Y-%m-%d")
        return f"{topic_clause} pushed:>{pushed}"
    return topic_clause


def _fetch_readme(client: httpx.Client, owner: str, repo: str) -> str | None:
    """Best-effort README fetch. Returns plain-text-ish content or None."""
    try:
        resp = client.get(
            f"{_GITHUB_API}/repos/{owner}/{repo}/readme",
            headers={**_auth_headers(), "Accept": "application/vnd.github.raw"},
            timeout=_HTTP_TIMEOUT,
            follow_redirects=True,
        )
        if resp.status_code != 200:
            return None
        return _strip_markdown(resp.text)
    except httpx.HTTPError as exc:
        logger.warning(
            "github_readme_fetch_failed",
            extra={"repo": f"{owner}/{repo}", "error": str(exc)},
        )
        return None


def _fetch_trending(
    client: httpx.Client, topics: list[str], max_results: int, since: datetime | None
) -> list[ScoutedPaper]:
    query = _build_trending_query(topics, since)
    try:
        resp = client.get(
            f"{_GITHUB_API}/search/repositories",
            params={
                "q": query,
                "sort": "stars",
                "order": "desc",
                "per_page": min(max_results, 30),
            },
            headers=_auth_headers(),
            timeout=_HTTP_TIMEOUT,
        )
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        logger.warning("github_trending_fetch_failed", extra={"error": str(exc)})
        return []

    items = resp.json().get("items", [])
    papers: list[ScoutedPaper] = []
    for item in items[:max_results]:
        owner = item["owner"]["login"]
        repo = item["name"]
        full = item["full_name"]
        url = item["html_url"]
        description = (item.get("description") or "").strip()
        topics_list = item.get("topics") or []
        pushed_at = item.get("pushed_at")
        try:
            published_at = (
                datetime.fromisoformat(pushed_at.replace("Z", "+00:00"))
                if pushed_at
                else datetime.now(UTC)
            )
        except ValueError:
            published_at = datetime.now(UTC)

        readme = _fetch_readme(client, owner, repo)
        abstract_parts: list[str] = []
        if description:
            abstract_parts.append(description)
        if topics_list:
            abstract_parts.append("Topics: " + ", ".join(topics_list))
        if readme:
            abstract_parts.append(readme)
        abstract = "\n\n".join(abstract_parts).strip() or full
        if not abstract:
            continue

        source = Source(
            type=SourceType.github,
            url=url,
            author=owner,
            published_at=published_at,
            raw_content_hash=_make_hash(abstract),
        )
        papers.append(
            ScoutedPaper(
                source=source,
                title=full,
                abstract=abstract,
                # Reuse the arxiv_id slot for the GitHub repo full name —
                # downstream code only treats it as an opaque identifier.
                arxiv_id=full.replace("/", "_"),
            )
        )
    return papers


# ── watchlist releases lane ────────────────────────────────────────────────


def _parse_atom_entries(xml: str) -> list[dict[str, str]]:
    """Parse a GitHub releases.atom feed into [{id, url, updated, title, content}]."""
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return []
    entries: list[dict[str, str]] = []
    for entry in root.findall("atom:entry", ns):
        entry_id = (entry.findtext("atom:id", default="", namespaces=ns) or "").strip()
        title = (entry.findtext("atom:title", default="", namespaces=ns) or "").strip()
        updated = (entry.findtext("atom:updated", default="", namespaces=ns) or "").strip()
        link_el = entry.find("atom:link", ns)
        url = link_el.attrib.get("href", "") if link_el is not None else ""
        content = (entry.findtext("atom:content", default="", namespaces=ns) or "").strip()
        entries.append(
            {"id": entry_id, "title": title, "updated": updated, "url": url, "content": content}
        )
    return entries


def _fetch_releases_for_repo(
    client: httpx.Client, owner_repo: str, since: datetime | None
) -> list[ScoutedPaper]:
    url = f"https://github.com/{owner_repo}/releases.atom"
    try:
        resp = client.get(url, timeout=_HTTP_TIMEOUT, follow_redirects=True)
        if resp.status_code != 200:
            return []
        entries = _parse_atom_entries(resp.text)
    except httpx.HTTPError as exc:
        logger.warning(
            "github_releases_fetch_failed",
            extra={"repo": owner_repo, "error": str(exc)},
        )
        return []

    papers: list[ScoutedPaper] = []
    for e in entries:
        try:
            updated = (
                datetime.fromisoformat(e["updated"].replace("Z", "+00:00"))
                if e["updated"]
                else datetime.now(UTC)
            )
        except ValueError:
            updated = datetime.now(UTC)
        if since is not None and updated < since:
            continue

        body = _strip_markdown(e["content"]) if e["content"] else ""
        abstract = body or e["title"]
        if not abstract:
            continue
        source = Source(
            type=SourceType.github,
            url=e["url"] or f"https://github.com/{owner_repo}",
            author=owner_repo.split("/")[0],
            published_at=updated,
            raw_content_hash=_make_hash(abstract),
        )
        papers.append(
            ScoutedPaper(
                source=source,
                title=f"{owner_repo} — {e['title']}",
                abstract=abstract,
                arxiv_id=f"{owner_repo.replace('/', '_')}_{e['id'].split('/')[-1]}",
            )
        )
    return papers


def _fetch_github(
    topics: list[str],
    max_results: int,
    watchlist: list[str],
    since: datetime | None,
) -> list[ScoutedPaper]:
    seen_hashes: set[str] = set()
    out: list[ScoutedPaper] = []
    with httpx.Client() as client:
        for paper in _fetch_trending(client, topics, max_results, since):
            if paper.source.raw_content_hash in seen_hashes:
                continue
            seen_hashes.add(paper.source.raw_content_hash)
            out.append(paper)

        for repo in watchlist:
            for paper in _fetch_releases_for_repo(client, repo, since):
                if paper.source.raw_content_hash in seen_hashes:
                    continue
                seen_hashes.add(paper.source.raw_content_hash)
                out.append(paper)
    return out


async def _handle_scout_github(payload: dict[str, Any]) -> dict[str, Any]:
    skill_input = ScoutGithubSkillInput.model_validate(payload)
    since: datetime | None = None
    if skill_input.since:
        try:
            since = datetime.fromisoformat(skill_input.since)
        except ValueError:
            since = None
    topics = _resolve_topics(skill_input.topics)
    watchlist = _resolve_watchlist(skill_input.watchlist)
    papers = await asyncio.to_thread(
        _fetch_github, topics, skill_input.max_results, watchlist, since
    )
    return ScoutGithubSkillOutput(
        papers=[p.model_dump(mode="json") for p in papers]
    ).model_dump(mode="json")


# ── Phase 22b investigation ────────────────────────────────────────────────


def _fetch_repos_by_query(query: str, max_results: int) -> list[ScoutedPaper]:
    """Hypothesis-directed variant of the trending lane: a free-text repo
    search (GitHub's search API accepts free text in ``q``) sorted by stars,
    enriched with each repo's README — the same ``ScoutedPaper`` shape the
    trending lane emits."""
    with httpx.Client() as client:
        try:
            resp = client.get(
                f"{_GITHUB_API}/search/repositories",
                params={
                    "q": query,
                    "sort": "stars",
                    "order": "desc",
                    "per_page": min(max_results, 30),
                },
                headers=_auth_headers(),
                timeout=_HTTP_TIMEOUT,
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            logger.warning("github_investigate_fetch_failed", extra={"error": str(exc)})
            return []

        items = resp.json().get("items", [])
        papers: list[ScoutedPaper] = []
        for item in items[:max_results]:
            owner = item["owner"]["login"]
            repo = item["name"]
            full = item["full_name"]
            url = item["html_url"]
            description = (item.get("description") or "").strip()
            topics_list = item.get("topics") or []
            pushed_at = item.get("pushed_at")
            try:
                published_at = (
                    datetime.fromisoformat(pushed_at.replace("Z", "+00:00"))
                    if pushed_at
                    else datetime.now(UTC)
                )
            except ValueError:
                published_at = datetime.now(UTC)

            readme = _fetch_readme(client, owner, repo)
            abstract_parts: list[str] = []
            if description:
                abstract_parts.append(description)
            if topics_list:
                abstract_parts.append("Topics: " + ", ".join(topics_list))
            if readme:
                abstract_parts.append(readme)
            abstract = "\n\n".join(abstract_parts).strip() or full
            if not abstract:
                continue

            source = Source(
                type=SourceType.github,
                url=url,
                author=owner,
                published_at=published_at,
                raw_content_hash=_make_hash(abstract),
            )
            papers.append(
                ScoutedPaper(
                    source=source,
                    title=full,
                    abstract=abstract,
                    arxiv_id=full.replace("/", "_"),
                )
            )
    return papers


async def _handle_investigate_github(payload: dict[str, Any]) -> dict[str, Any]:
    skill_input = InvestigateSkillInput.model_validate(payload)
    query = keywords_from_hypothesis(skill_input.hypothesis)
    papers = await asyncio.to_thread(
        _fetch_repos_by_query, query, skill_input.max_results
    )
    return InvestigateSkillOutput(
        investigation_id=skill_input.investigation_id,
        source_records=[p.model_dump(mode="json") for p in papers],
    ).model_dump(mode="json")


class GitHubScoutAgent(BaseAgent):
    name = "github_scout"

    def __init__(self, llm: Any | None = None, db_conn: Any | None = None) -> None:
        super().__init__(llm=llm, db_conn=db_conn)

    async def run(self, input: BaseModel) -> ScoutGithubSkillOutput:  # pragma: no cover
        raise NotImplementedError("GitHubScoutAgent uses the A2A skill path only")

    def to_a2a_server(self, url: str) -> Starlette:
        card = build_multi_skill_card(
            name="GitHub Scout",
            description=(
                "Surfaces trending ML/AI repos by topic search and release notes "
                "for a watchlist of foundational repos."
            ),
            url=url,
            skills=[
                SkillSpec(
                    id="scout_github",
                    name="Scout GitHub",
                    description=(
                        "Search GitHub by topic for trending repos and fetch release "
                        "notes for watchlist repos via /releases.atom."
                    ),
                    tags=["github", "code", "releases", "trending"],
                ),
                investigate_skill_spec("github"),
            ],
        )
        return build_task_app(
            agent_card=card,
            skill_handlers={
                "scout_github": _handle_scout_github,
                "investigate_github": _handle_investigate_github,
            },
            agent_name="github_scout",
        )
