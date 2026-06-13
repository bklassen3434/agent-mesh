"""REST/JSON scout — generic JSON HTTP API connector (Phase 18a).

A *config-driven* connector: one service serves arbitrarily many configured
instances. Each instance is a ``field_connectors`` row whose config gives an
``endpoint`` and a small field-mapping of dotted JSON paths
(``items_path`` / ``title_path`` / ``text_path`` / ``url_path`` /
``published_path``) describing how to pull source records out of the response.

Trusted-input model (Phase 18a): the endpoint is operator-supplied and fetched
read-only (GET) with a bounded timeout and a per-run ``max_results`` cap. HTTP
or JSON failures degrade to an empty result — one bad endpoint never aborts a
run.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
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

logger = logging.getLogger(__name__)

_HTTP_TIMEOUT = 20.0
_MAX_ABSTRACT_LEN = 4000


class ScoutRestJsonSkillInput(BaseModel):
    endpoint: str
    query_template: str = ""
    items_path: str = ""
    title_path: str = ""
    text_path: str = ""
    url_path: str = ""
    published_path: str = ""
    max_results: int = 20
    since: str | None = None  # ISO-8601 string; items before it are skipped


class ScoutRestJsonSkillOutput(BaseModel):
    papers: list[dict[str, Any]]


def _make_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _dig(obj: Any, path: str) -> Any:
    """Resolve a dotted path against nested dicts/lists. None if absent.

    A numeric segment indexes a list (``results.0.title``); everything else is a
    dict key. An empty path returns ``obj`` unchanged.
    """
    if not path:
        return obj
    current = obj
    for segment in path.split("."):
        if isinstance(current, dict):
            current = current.get(segment)
        elif isinstance(current, list) and segment.isdigit():
            idx = int(segment)
            current = current[idx] if idx < len(current) else None
        else:
            return None
        if current is None:
            return None
    return current


def _parse_published(raw: Any) -> datetime | None:
    """Best-effort parse of a published value: epoch seconds or ISO-8601."""
    if raw is None:
        return None
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        try:
            return datetime.fromtimestamp(float(raw), UTC)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(raw, str):
        text = raw.strip().replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            return None
        return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
    return None


def _build_url(endpoint: str, query_template: str) -> str:
    if not query_template:
        return endpoint
    sep = "&" if "?" in endpoint else "?"
    return f"{endpoint}{sep}{query_template.lstrip('?&')}"


def _item_to_paper(
    endpoint: str,
    item: Any,
    cfg: ScoutRestJsonSkillInput,
    cutoff: datetime | None,
) -> ScoutedPaper | None:
    if not isinstance(item, dict):
        return None
    title = str(_dig(item, cfg.title_path) or "").strip()
    text = str(_dig(item, cfg.text_path) or "").strip()[:_MAX_ABSTRACT_LEN]
    abstract = text or title
    if not abstract:
        return None

    published = _parse_published(_dig(item, cfg.published_path)) or datetime.now(UTC)
    if cutoff is not None and published < cutoff:
        return None

    url = str(_dig(item, cfg.url_path) or "").strip()
    content_hash = _make_hash(abstract)
    if not url:
        # No url field — synthesize a stable external id from the endpoint +
        # content so the dedup ledger still keys consistently.
        url = f"rest://{endpoint}#{content_hash[:16]}"

    source = Source(
        type=SourceType.rest,
        url=url,
        published_at=published,
        raw_content_hash=content_hash,
    )
    return ScoutedPaper(
        source=source,
        title=title or endpoint,
        abstract=abstract,
        arxiv_id=f"rest_{content_hash[:12]}",
    )


def _fetch_rest(cfg: ScoutRestJsonSkillInput, cutoff: datetime | None) -> list[ScoutedPaper]:
    url = _build_url(cfg.endpoint, cfg.query_template)
    try:
        with httpx.Client() as client:
            resp = client.get(url, timeout=_HTTP_TIMEOUT, follow_redirects=True)
            resp.raise_for_status()
            data = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning(
            "rest_json_fetch_failed",
            extra={"endpoint": cfg.endpoint, "error": str(exc)},
        )
        return []

    items = _dig(data, cfg.items_path)
    if not isinstance(items, list):
        logger.warning(
            "rest_json_items_not_list",
            extra={"endpoint": cfg.endpoint, "items_path": cfg.items_path},
        )
        return []

    seen: set[str] = set()
    out: list[ScoutedPaper] = []
    for item in items:
        paper = _item_to_paper(cfg.endpoint, item, cfg, cutoff)
        if paper is None or paper.source.url in seen:
            continue
        seen.add(paper.source.url)
        out.append(paper)
        if len(out) >= cfg.max_results:
            break
    return out


async def _handle_scout_rest_json(payload: dict[str, Any]) -> dict[str, Any]:
    skill_input = ScoutRestJsonSkillInput.model_validate(payload)
    cutoff: datetime | None = None
    if skill_input.since:
        cutoff = datetime.fromisoformat(skill_input.since)
        # Parsed timestamps are normalized to tz-aware (see _parse_dt); a
        # bare/offset-less `since` would raise on the `published < cutoff`
        # comparison, so normalize it to UTC.
        if cutoff.tzinfo is None:
            cutoff = cutoff.replace(tzinfo=UTC)
    papers = await asyncio.to_thread(_fetch_rest, skill_input, cutoff)
    return ScoutRestJsonSkillOutput(
        papers=[p.model_dump(mode="json") for p in papers]
    ).model_dump(mode="json")


class RestJsonScoutAgent(BaseAgent):
    name = "rest_json_scout"

    def __init__(self, llm: Any | None = None, db_conn: Any | None = None) -> None:
        super().__init__(llm=llm, db_conn=db_conn)

    async def run(self, input: BaseModel) -> ScoutRestJsonSkillOutput:  # pragma: no cover
        raise NotImplementedError("RestJsonScoutAgent uses the A2A skill path only")

    def to_a2a_server(self, url: str) -> Starlette:
        card = build_multi_skill_card(
            name="REST/JSON Scout",
            description="Ingests items from a user-configured JSON HTTP endpoint.",
            url=url,
            skills=[
                SkillSpec(
                    id="scout_rest_json",
                    name="Scout REST/JSON",
                    description=(
                        "GET a configured endpoint and map its JSON to sources via "
                        "items_path / title_path / text_path / url_path / published_path."
                    ),
                    tags=["rest", "json", "config_driven"],
                ),
            ],
        )
        return build_task_app(
            agent_card=card,
            skill_handlers={"scout_rest_json": _handle_scout_rest_json},
            agent_name="rest_json_scout",
        )
