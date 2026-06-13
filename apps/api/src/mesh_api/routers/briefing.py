"""GET /api/v1/briefing — personalized daily digest.

The API computes briefings on demand (no DB-backed Briefing table) by
gathering candidate rows from the DB, dispatching to the Personalizer
agent via A2A, and returning the ranked Briefing.

Per-day result is cached in process memory keyed by (date, profile_hash)
so a wiki refresh inside the same day doesn't re-incur the LLM cost.
The profile hash means an edit to ~/.config/agent_mesh/profile.md
naturally invalidates the cache for that day.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query
from mesh_a2a.client import MeshA2AClient, SkillCallError, TaskTimeoutError
from mesh_db.connection import MeshConnection
from mesh_models.briefing import Briefing, BriefingSection

from mesh_api.deps import ConnDep

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/briefing", tags=["briefing"])

_CACHE: dict[tuple[str, str], Briefing] = {}
_CACHE_LOCK = asyncio.Lock()


# ── profile loading ────────────────────────────────────────────────────────


def _profile_path() -> Path:
    override = os.environ.get("MESH_PROFILE_PATH")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".config" / "agent_mesh" / "profile.md"


def _load_profile() -> str | None:
    path = _profile_path()
    try:
        return path.read_text()
    except FileNotFoundError:
        return None
    except OSError as exc:
        logger.warning("profile_read_failed", extra={"path": str(path), "error": str(exc)})
        return None


def _profile_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


# ── DB candidate gathering ─────────────────────────────────────────────────


def _window(target: date) -> tuple[datetime, datetime]:
    """Inclusive [start, end) window for the target date (UTC)."""
    start = datetime.combine(target, datetime.min.time(), tzinfo=UTC)
    end = start + timedelta(days=1)
    return start, end


def _gather_new_beliefs(
    conn: MeshConnection, start: datetime, end: datetime, field: str
) -> list[dict[str, Any]]:
    """Beliefs whose first appearance falls in the window.

    The beliefs table doesn't carry a created_at, but a row with
    revision_count=0 was never revised — meaning last_revised_at IS the
    creation timestamp.
    """
    rows = conn.execute(
        """
        SELECT id, topic, statement, confidence, last_revised_at
        FROM beliefs
        WHERE revision_count = 0
          AND is_currently_held = TRUE
          AND field_id = %s
          AND last_revised_at >= %s
          AND last_revised_at < %s
        ORDER BY last_revised_at DESC
        LIMIT 50
        """,
        [field, start, end],
    ).fetchall()
    return [
        {
            "id": r[0],
            "topic": r[1],
            "statement": r[2],
            "confidence": float(r[3]),
            "created_at": r[4],
        }
        for r in rows
    ]


def _gather_revisions(
    conn: MeshConnection, start: datetime, end: datetime, field: str
) -> list[dict[str, Any]]:
    # belief_revisions has no field_id; scope via the joined belief's field_id.
    rows = conn.execute(
        """
        SELECT r.id, r.belief_id, r.previous_statement, r.new_statement,
               r.previous_confidence, r.new_confidence, r.revised_by_agent,
               r.rationale, r.revised_at, b.topic
        FROM belief_revisions r
        JOIN beliefs b ON r.belief_id = b.id
        WHERE b.field_id = %s
          AND r.revised_at >= %s AND r.revised_at < %s
        ORDER BY r.revised_at DESC
        LIMIT 50
        """,
        [field, start, end],
    ).fetchall()
    return [
        {
            "id": r[0],
            "belief_id": r[1],
            "previous_statement": r[2] or "",
            "new_statement": r[3] or "",
            "previous_confidence": float(r[4]),
            "new_confidence": float(r[5]),
            "revised_by_agent": r[6],
            "rationale": r[7],
            "revised_at": r[8],
            "belief_topic": r[9] or "",
        }
        for r in rows
    ]


def _gather_claims(
    conn: MeshConnection,
    start: datetime,
    end: datetime,
    field: str,
    min_confidence: float = 0.8,
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT c.id, c.predicate, c.subject_entity_id, c.object, c.raw_excerpt,
               c.confidence, e.canonical_name
        FROM claims c
        LEFT JOIN entities e ON e.id = c.subject_entity_id
        WHERE c.status = 'active'
          AND c.field_id = %s
          AND c.extracted_at >= %s
          AND c.extracted_at < %s
          AND c.confidence >= %s
        ORDER BY c.confidence DESC, c.extracted_at DESC
        LIMIT 50
        """,
        [field, start, end, min_confidence],
    ).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        import json
        obj = r[3]
        if isinstance(obj, str):
            try:
                obj = json.loads(obj)
            except json.JSONDecodeError:
                obj = {}
        out.append(
            {
                "id": r[0],
                "predicate": r[1],
                "subject_entity_id": r[2],
                "subject_name": r[6],
                "object": obj,
                "raw_excerpt": r[4],
                "confidence": float(r[5]),
            }
        )
    return out


# ── A2A dispatch ───────────────────────────────────────────────────────────


def _agent_urls() -> list[str]:
    raw = os.environ.get("MESH_BRIEFING_AGENT_URLS") or os.environ.get(
        "MESH_PERSONALIZER_URL"
    )
    if raw:
        return [u.strip() for u in raw.split(",") if u.strip()]
    return ["http://personalizer:8013"]


async def _run_personalizer(
    profile_text: str,
    target: date,
    beliefs: list[dict[str, Any]],
    revisions: list[dict[str, Any]],
    claims: list[dict[str, Any]],
    field: str = "ai-robotics",
) -> Briefing:
    payload = {
        "profile_text": profile_text,
        "target_date": target.isoformat(),
        "beliefs": beliefs,
        "revisions": revisions,
        "claims": claims,
        "field_id": field,
    }
    async with MeshA2AClient() as client:
        discovered = await client.discover(_agent_urls())
        if "personalize_digest" not in discovered:
            raise HTTPException(
                status_code=503,
                detail=(
                    "Personalizer agent is not reachable. Ensure the personalizer "
                    "container is running and MESH_BRIEFING_AGENT_URLS is set "
                    "correctly."
                ),
            )
        try:
            result = await client.call_skill_blocking(
                "personalize_digest", payload, timeout=120.0
            )
        except TaskTimeoutError as exc:
            raise HTTPException(status_code=504, detail=str(exc)) from exc
        except SkillCallError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
    return Briefing.model_validate(result)


# ── route ──────────────────────────────────────────────────────────────────


@router.get(
    "",
    response_model=Briefing,
    summary="Personalized daily briefing",
    description=(
        "Loads the user's markdown profile from $MESH_PROFILE_PATH "
        "(default ~/.config/agent_mesh/profile.md), gathers candidate beliefs, "
        "revisions, and high-confidence claims for the requested date (default "
        "today), dispatches the Personalizer agent over A2A, and returns the "
        "ranked Briefing. Result is cached in-process by (date, profile_hash); "
        "edits to the profile invalidate that day's cache."
    ),
)
async def get_briefing(
    conn: ConnDep,
    target_date: Annotated[date | None, Query(alias="date")] = None,
    field: str = Query("ai-robotics", description="Field slug to scope results to"),
) -> Briefing:
    target = target_date or datetime.now(UTC).date()

    profile_text = _load_profile()
    if profile_text is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"No profile found at {_profile_path()}. Create one with a "
                "free-form markdown description of what you care about. See "
                "docs/personalization.md for a template."
            ),
        )

    profile_hash = _profile_hash(profile_text)
    cache_key = (f"{field}:{target.isoformat()}", profile_hash)
    async with _CACHE_LOCK:
        cached = _CACHE.get(cache_key)
        if cached is not None:
            return cached

    start, end = _window(target)
    beliefs = _gather_new_beliefs(conn, start, end, field)
    revisions = _gather_revisions(conn, start, end, field)
    claims = _gather_claims(conn, start, end, field)

    if not beliefs and not revisions and not claims:
        # Skip the LLM round-trip when there's nothing to rank.
        empty = Briefing(
            date=target,
            profile_excerpt=profile_text.splitlines()[0][:200] if profile_text else "",
            sections=[
                BriefingSection(
                    name="Quiet day",
                    description=(
                        "No new beliefs, revisions, or high-confidence "
                        "claims in the window."
                    ),
                    items=[],
                )
            ],
        )
        async with _CACHE_LOCK:
            _CACHE[cache_key] = empty
        return empty

    briefing = await _run_personalizer(
        profile_text, target, beliefs, revisions, claims, field
    )

    async with _CACHE_LOCK:
        _CACHE[cache_key] = briefing
    return briefing
