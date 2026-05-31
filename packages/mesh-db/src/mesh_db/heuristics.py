"""Procedural memory store access layer (Phase 16b).

Typed reads/writes for ``agent_heuristic`` + ``agent_heuristic_revision``,
mirroring ``beliefs.py`` / ``revisions.py``. Writes go through the coordinator
(mesh_writer role); the API and CLI read via mesh_reader. The head row is
mutable (confidence/heuristic/activity), but every change is also recorded as
an append-only revision row — the same belief/revision discipline.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from mesh_models.heuristic import AgentHeuristic, AgentHeuristicRevision

from mesh_db.connection import MeshConnection

MAX_LIMIT = 200

_COLS = (
    "id, agent, skill, source, entity_id, heuristic, confidence, "
    "provenance_run_ids, provenance_claim_ids, created_at, last_revised_at, "
    "revision_count, expires_at, is_currently_active"
)
_SELECT = f"SELECT {_COLS} FROM agent_heuristic"


def _as_dt(value: Any) -> datetime:
    return value if isinstance(value, datetime) else datetime.fromisoformat(str(value))


def _row_to_heuristic(row: tuple[Any, ...]) -> AgentHeuristic:
    (
        id_, agent, skill, source, entity_id, heuristic, confidence,
        provenance_run_ids, provenance_claim_ids, created_at, last_revised_at,
        revision_count, expires_at, is_currently_active,
    ) = row[:14]
    return AgentHeuristic(
        id=id_,
        agent=agent,
        skill=skill,
        source=source,
        entity_id=entity_id,
        heuristic=heuristic,
        confidence=float(confidence),
        provenance_run_ids=list(provenance_run_ids) if provenance_run_ids else [],
        provenance_claim_ids=list(provenance_claim_ids) if provenance_claim_ids else [],
        created_at=_as_dt(created_at),
        last_revised_at=_as_dt(last_revised_at),
        revision_count=int(revision_count),
        expires_at=_as_dt(expires_at),
        is_currently_active=bool(is_currently_active),
    )


def create_heuristic(conn: MeshConnection, model: AgentHeuristic) -> AgentHeuristic:
    conn.execute(
        """
        INSERT INTO agent_heuristic
            (id, agent, skill, source, entity_id, heuristic, confidence,
             provenance_run_ids, provenance_claim_ids, created_at, last_revised_at,
             revision_count, expires_at, is_currently_active)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        [
            model.id,
            model.agent,
            model.skill,
            model.source,
            model.entity_id,
            model.heuristic,
            model.confidence,
            model.provenance_run_ids,
            model.provenance_claim_ids,
            model.created_at,
            model.last_revised_at,
            model.revision_count,
            model.expires_at,
            model.is_currently_active,
        ],
    )
    return model


def get_heuristic_by_id(conn: MeshConnection, id: str) -> AgentHeuristic | None:
    row = conn.execute(f"{_SELECT} WHERE id = %s", [id]).fetchone()
    return _row_to_heuristic(row) if row else None


def update_heuristic(conn: MeshConnection, id: str, **fields: Any) -> AgentHeuristic:
    """Patch a heuristic head row. Mirrors ``update_belief`` — the allow-list
    keeps immutable identity/scope columns out of reach; provenance grows via
    revisions, not silent overwrite of the head, so it is updatable here only
    so the consolidation job can fold fresh provenance into the active row."""
    allowed = {
        "heuristic", "confidence", "provenance_run_ids", "provenance_claim_ids",
        "last_revised_at", "revision_count", "expires_at", "is_currently_active",
    }
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        existing = get_heuristic_by_id(conn, id)
        if existing is None:
            raise ValueError(f"AgentHeuristic {id} not found")
        return existing
    set_clauses = [f"{k} = %s" for k in updates]
    params: list[Any] = list(updates.values())
    params.append(id)
    conn.execute(
        f"UPDATE agent_heuristic SET {', '.join(set_clauses)} WHERE id = %s", params
    )
    updated = get_heuristic_by_id(conn, id)
    if updated is None:
        raise ValueError(f"AgentHeuristic {id} not found after update")
    return updated


def list_heuristics(
    conn: MeshConnection,
    agent: str | None = None,
    skill: str | None = None,
    active: bool | None = None,
    include_expired: bool = True,
    limit: int = 100,
    offset: int = 0,
    now: datetime | None = None,
) -> list[AgentHeuristic]:
    """General listing (CLI inspection). ``include_expired=False`` drops rows
    past their TTL; ``active`` filters on ``is_currently_active``."""
    limit = min(max(limit, 0), MAX_LIMIT)
    offset = max(offset, 0)
    conditions: list[str] = []
    params: list[Any] = []
    if agent is not None:
        conditions.append("agent = %s")
        params.append(agent)
    if skill is not None:
        conditions.append("skill = %s")
        params.append(skill)
    if active is not None:
        conditions.append("is_currently_active = %s")
        params.append(active)
    if not include_expired:
        conditions.append("expires_at > %s")
        params.append(now or datetime.now(UTC))
    where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
    params.extend([limit, offset])
    rows = conn.execute(
        f"{_SELECT}{where} ORDER BY confidence DESC, last_revised_at DESC "
        "LIMIT %s OFFSET %s",
        params,
    ).fetchall()
    return [_row_to_heuristic(r) for r in rows]


def list_applicable_heuristics(
    conn: MeshConnection,
    agent: str,
    skill: str,
    *,
    source: str | None = None,
    entity_id: str | None = None,
    now: datetime | None = None,
    limit: int = 10,
) -> list[AgentHeuristic]:
    """Scope-matched, unexpired, active heuristics for a skill (Phase 16d).

    A heuristic with ``source``/``entity_id`` NULL applies broadly; one with a
    finer scope set applies only when the caller's scope matches it. Expired
    (past ``expires_at``) and inactive rows are excluded. Ordered by confidence
    so the most trusted how-to leads the prompt."""
    limit = min(max(limit, 0), MAX_LIMIT)
    rows = conn.execute(
        f"""
        {_SELECT}
        WHERE agent = %s AND skill = %s AND is_currently_active
          AND expires_at > %s
          AND (source IS NULL OR source = %s)
          AND (entity_id IS NULL OR entity_id = %s)
        ORDER BY confidence DESC, last_revised_at DESC
        LIMIT %s
        """,
        [agent, skill, now or datetime.now(UTC), source, entity_id, limit],
    ).fetchall()
    return [_row_to_heuristic(r) for r in rows]


# ── revisions (append-only) ──────────────────────────────────────────────────

_REV_COLS = (
    "id, heuristic_id, previous_heuristic, new_heuristic, previous_confidence, "
    "new_confidence, provenance_run_ids, provenance_claim_ids, revised_by_agent, "
    "revised_at, rationale"
)
_REV_SELECT = f"SELECT {_REV_COLS} FROM agent_heuristic_revision"


def _row_to_revision(row: tuple[Any, ...]) -> AgentHeuristicRevision:
    (
        id_, heuristic_id, previous_heuristic, new_heuristic, previous_confidence,
        new_confidence, provenance_run_ids, provenance_claim_ids, revised_by_agent,
        revised_at, rationale,
    ) = row[:11]
    return AgentHeuristicRevision(
        id=id_,
        heuristic_id=heuristic_id,
        previous_heuristic=previous_heuristic,
        new_heuristic=new_heuristic,
        previous_confidence=float(previous_confidence),
        new_confidence=float(new_confidence),
        provenance_run_ids=list(provenance_run_ids) if provenance_run_ids else [],
        provenance_claim_ids=list(provenance_claim_ids) if provenance_claim_ids else [],
        revised_by_agent=revised_by_agent,
        revised_at=_as_dt(revised_at),
        rationale=rationale,
    )


def create_heuristic_revision(
    conn: MeshConnection, model: AgentHeuristicRevision
) -> AgentHeuristicRevision:
    conn.execute(
        """
        INSERT INTO agent_heuristic_revision
            (id, heuristic_id, previous_heuristic, new_heuristic, previous_confidence,
             new_confidence, provenance_run_ids, provenance_claim_ids, revised_by_agent,
             revised_at, rationale)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        [
            model.id,
            model.heuristic_id,
            model.previous_heuristic,
            model.new_heuristic,
            model.previous_confidence,
            model.new_confidence,
            model.provenance_run_ids,
            model.provenance_claim_ids,
            model.revised_by_agent,
            model.revised_at,
            model.rationale,
        ],
    )
    return model


def list_heuristic_revisions(
    conn: MeshConnection,
    heuristic_id: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[AgentHeuristicRevision]:
    limit = min(max(limit, 0), MAX_LIMIT)
    offset = max(offset, 0)
    params: list[Any] = []
    where = ""
    if heuristic_id is not None:
        where = " WHERE heuristic_id = %s"
        params.append(heuristic_id)
    params.extend([limit, offset])
    rows = conn.execute(
        f"{_REV_SELECT}{where} ORDER BY revised_at DESC LIMIT %s OFFSET %s", params
    ).fetchall()
    return [_row_to_revision(r) for r in rows]
