"""Leaderboard scout — targeted scrapes of well-defined evaluation surfaces.

Three sub-fetchers, each independently failure-isolated so one breaking
parser doesn't take down the other two:

* HuggingFace Open LLM Leaderboard (current canonical eval gauntlet)
* Papers-with-Code SOTA per benchmark (long tail of academic benchmarks)
* Chatbot Arena (LMSys) leaderboard (human-preference vs. eval-suite)

Each lane produces 0-1 ``ScoutedPaper`` with ``source.type=leaderboard``
and an abstract that lists the top entries in a claim-extractor-friendly
text format ("ModelA achieves X on Y").
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
from mesh_agents.investigation import (
    InvestigateSkillInput,
    InvestigateSkillOutput,
    investigate_skill_spec,
)

logger = logging.getLogger(__name__)

_HTTP_TIMEOUT = 20.0
_TOP_N = 10


class ScoutLeaderboardsSkillInput(BaseModel):
    # Subset of lanes to run; defaults to all three.
    lanes: list[str] | None = None
    top_n: int = _TOP_N


class ScoutLeaderboardsSkillOutput(BaseModel):
    papers: list[dict[str, Any]]


# ── HuggingFace Open LLM Leaderboard ───────────────────────────────────────


_HF_DATASET = "open-llm-leaderboard/contents"
_HF_ROWS_API = "https://datasets-server.huggingface.co/rows"


def _fetch_hf_open_llm(client: httpx.Client, top_n: int) -> list[ScoutedPaper]:
    try:
        resp = client.get(
            _HF_ROWS_API,
            params={
                "dataset": _HF_DATASET,
                "config": "default",
                "split": "train",
                "offset": 0,
                "length": top_n,
            },
            timeout=_HTTP_TIMEOUT,
        )
        resp.raise_for_status()
        body = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("leaderboard_hf_fetch_failed", extra={"error": str(exc)})
        return []

    rows = body.get("rows") or []
    if not rows:
        return []

    today = datetime.now(UTC)
    today_str = today.strftime("%Y-%m-%d")
    lines: list[str] = [f"HuggingFace Open LLM Leaderboard (as of {today_str}):"]
    for i, item in enumerate(rows[:top_n], start=1):
        row = item.get("row") or {}
        model = row.get("model") or row.get("fullname") or "<unknown>"
        avg = row.get("average") or row.get("Average")
        if isinstance(avg, (int, float)):
            lines.append(f"{i}. {model} — Average {avg:.2f} on Open LLM Leaderboard")
        else:
            lines.append(f"{i}. {model}")
    abstract = "\n".join(lines)
    return [_leaderboard_paper("HuggingFace Open LLM Leaderboard", abstract, today)]


# ── Papers-with-Code SOTA ──────────────────────────────────────────────────


_PWC_BASE = "https://paperswithcode.com/api/v1"
_PWC_BENCHMARKS = ["mmlu", "humaneval", "gsm8k", "hellaswag", "arc"]


def _fetch_paperswithcode(client: httpx.Client, top_n: int) -> list[ScoutedPaper]:
    today = datetime.now(UTC)
    today_str = today.strftime("%Y-%m-%d")
    block_lines: list[str] = []
    for benchmark in _PWC_BENCHMARKS:
        try:
            resp = client.get(
                f"{_PWC_BASE}/sota/{benchmark}/",
                timeout=_HTTP_TIMEOUT,
            )
            if resp.status_code != 200:
                continue
            data = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning(
                "leaderboard_pwc_benchmark_failed",
                extra={"benchmark": benchmark, "error": str(exc)},
            )
            continue

        results = data.get("results") or data.get("rows") or []
        if not results:
            continue
        block_lines.append(f"Papers-with-Code SOTA on {benchmark} (as of {today_str}):")
        for i, item in enumerate(results[:top_n], start=1):
            model = (
                item.get("model")
                or item.get("paper", {}).get("title")
                or "<unknown>"
            )
            metric = item.get("metric") or item.get("metric_value") or {}
            value = (
                metric.get("value")
                if isinstance(metric, dict)
                else item.get("score")
            )
            if value is not None:
                block_lines.append(f"{i}. {model} achieves {value} on {benchmark}")
            else:
                block_lines.append(f"{i}. {model}")
        block_lines.append("")  # blank line between benchmarks

    if not block_lines:
        return []
    abstract = "\n".join(block_lines).strip()
    return [_leaderboard_paper("Papers-with-Code SOTA", abstract, today)]


# ── Chatbot Arena (LMSys) ──────────────────────────────────────────────────


_ARENA_CSV_URL = (
    "https://huggingface.co/spaces/lmsys/chatbot-arena-leaderboard/raw/main/"
    "leaderboard_table.csv"
)


def _fetch_chatbot_arena(client: httpx.Client, top_n: int) -> list[ScoutedPaper]:
    try:
        resp = client.get(_ARENA_CSV_URL, timeout=_HTTP_TIMEOUT, follow_redirects=True)
        if resp.status_code != 200:
            return []
        text = resp.text
    except httpx.HTTPError as exc:
        logger.warning("leaderboard_arena_fetch_failed", extra={"error": str(exc)})
        return []

    lines = [ln for ln in text.splitlines() if ln.strip()]
    if len(lines) < 2:
        return []
    header = [h.strip() for h in lines[0].split(",")]
    try:
        model_idx = next(
            i for i, h in enumerate(header) if h.lower() in ("model", "name")
        )
    except StopIteration:
        model_idx = 0
    try:
        rating_idx = next(
            i for i, h in enumerate(header) if "rating" in h.lower() or h.lower() == "score"
        )
    except StopIteration:
        rating_idx = -1

    today = datetime.now(UTC)
    today_str = today.strftime("%Y-%m-%d")
    out_lines: list[str] = [f"Chatbot Arena (LMSys) leaderboard (as of {today_str}):"]
    for i, row in enumerate(lines[1 : top_n + 1], start=1):
        cells = [c.strip() for c in row.split(",")]
        if not cells:
            continue
        model = cells[model_idx] if model_idx < len(cells) else cells[0]
        if rating_idx >= 0 and rating_idx < len(cells):
            rating = cells[rating_idx]
            out_lines.append(f"{i}. {model} — Arena rating {rating}")
        else:
            out_lines.append(f"{i}. {model}")
    abstract = "\n".join(out_lines)
    return [_leaderboard_paper("Chatbot Arena (LMSys)", abstract, today)]


# ── shared helpers ─────────────────────────────────────────────────────────


_LANE_FETCHERS: dict[str, Any] = {
    "hf_open_llm": _fetch_hf_open_llm,
    "papers_with_code": _fetch_paperswithcode,
    "chatbot_arena": _fetch_chatbot_arena,
}


def _leaderboard_paper(
    name: str, abstract: str, published: datetime
) -> ScoutedPaper:
    slug = name.lower().replace(" ", "_").replace("(", "").replace(")", "")
    date_iso = published.strftime("%Y%m%d")
    source = Source(
        type=SourceType.leaderboard,
        url=f"leaderboard://{slug}/{date_iso}",
        author=name,
        published_at=published,
        raw_content_hash=hashlib.sha256(abstract.encode()).hexdigest(),
    )
    return ScoutedPaper(
        source=source,
        title=f"{name} — snapshot {published.strftime('%Y-%m-%d')}",
        abstract=abstract,
        arxiv_id=f"leaderboard_{slug}_{date_iso}",
    )


def _fetch_leaderboards(lanes: list[str], top_n: int) -> list[ScoutedPaper]:
    out: list[ScoutedPaper] = []
    with httpx.Client() as client:
        for lane in lanes:
            fetcher = _LANE_FETCHERS.get(lane)
            if fetcher is None:
                logger.warning("leaderboard_unknown_lane", extra={"lane": lane})
                continue
            try:
                out.extend(fetcher(client, top_n))
            except Exception as exc:  # belt + suspenders around each lane
                logger.warning(
                    "leaderboard_lane_crashed", extra={"lane": lane, "error": str(exc)}
                )
    return out


async def _handle_scout_leaderboards(payload: dict[str, Any]) -> dict[str, Any]:
    skill_input = ScoutLeaderboardsSkillInput.model_validate(payload)
    lanes = skill_input.lanes or list(_LANE_FETCHERS.keys())
    papers = await asyncio.to_thread(_fetch_leaderboards, lanes, skill_input.top_n)
    return ScoutLeaderboardsSkillOutput(
        papers=[p.model_dump(mode="json") for p in papers]
    ).model_dump(mode="json")


# ── Phase 22b investigation ────────────────────────────────────────────────


async def _handle_investigate_leaderboard(payload: dict[str, Any]) -> dict[str, Any]:
    """Hypothesis-directed leaderboard fetch. Leaderboards aren't keyword-
    searchable, so the "search" is a fresh snapshot of all three lanes — the
    current SOTA numbers are exactly the evidence an investigation about a
    model/benchmark belief needs. Extraction downstream surfaces whatever rows
    bear on the hypothesis."""
    skill_input = InvestigateSkillInput.model_validate(payload)
    papers = await asyncio.to_thread(
        _fetch_leaderboards, list(_LANE_FETCHERS.keys()), skill_input.max_results
    )
    return InvestigateSkillOutput(
        investigation_id=skill_input.investigation_id,
        source_records=[p.model_dump(mode="json") for p in papers],
    ).model_dump(mode="json")


class LeaderboardScoutAgent(BaseAgent):
    name = "leaderboard_scout"

    def __init__(self, llm: Any | None = None, db_conn: Any | None = None) -> None:
        super().__init__(llm=llm, db_conn=db_conn)

    async def run(self, input: BaseModel) -> ScoutLeaderboardsSkillOutput:  # pragma: no cover
        raise NotImplementedError("LeaderboardScoutAgent uses the A2A skill path only")

    def to_a2a_server(self, url: str) -> Starlette:
        card = build_multi_skill_card(
            name="Leaderboard Scout",
            description=(
                "Snapshots the top entries of HuggingFace Open LLM Leaderboard, "
                "Papers-with-Code SOTA, and Chatbot Arena."
            ),
            url=url,
            skills=[
                SkillSpec(
                    id="scout_leaderboards",
                    name="Scout Leaderboards",
                    description=(
                        "Three failure-isolated lanes: hf_open_llm, papers_with_code, "
                        "chatbot_arena. One lane failing does not affect the others."
                    ),
                    tags=["leaderboard", "benchmarks", "evaluations"],
                ),
                investigate_skill_spec("leaderboard"),
            ],
        )
        return build_task_app(
            agent_card=card,
            skill_handlers={
                "scout_leaderboards": _handle_scout_leaderboards,
                "investigate_leaderboard": _handle_investigate_leaderboard,
            },
            agent_name="leaderboard_scout",
        )
