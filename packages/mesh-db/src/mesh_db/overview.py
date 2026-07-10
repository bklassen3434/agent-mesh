"""Field Overview reads — the shared, read-only queries behind both the
``write-field-brief`` skill (narrative inputs) and the ``/fields/{id}/overview``
API endpoint. Field-scoped and generic: nothing here assumes ai-robotics.
"""
from __future__ import annotations

from typing import Any

from mesh_models.belief import Belief
from mesh_models.investigation import Investigation, InvestigationStatus

from mesh_db.beliefs import _row_to_belief
from mesh_db.connection import MeshConnection

_BELIEF_COLS = (
    "id, topic, statement, supporting_claim_ids, contradicting_claim_ids, "
    "confidence, last_revised_at, revision_count, is_currently_held"
)


def strongest_beliefs(
    conn: MeshConnection, field_id: str, *, limit: int = 8
) -> list[Belief]:
    """Held beliefs ranked by confidence (evidence-derived), deepest-backed first
    on ties."""
    rows = conn.execute(
        f"""
        SELECT {_BELIEF_COLS} FROM beliefs
        WHERE field_id = %s AND is_currently_held
        ORDER BY confidence DESC, revision_count DESC
        LIMIT %s
        """,
        [field_id, max(int(limit), 0)],
    ).fetchall()
    return [_row_to_belief(r) for r in rows]


def contested_beliefs(
    conn: MeshConnection, field_id: str, *, limit: int = 5
) -> list[Belief]:
    """Held beliefs carrying at least one contradicting claim — the store's
    live disagreements, most-confident (highest-stakes) first."""
    rows = conn.execute(
        f"""
        SELECT {_BELIEF_COLS} FROM beliefs
        WHERE field_id = %s AND is_currently_held
          AND cardinality(contradicting_claim_ids) > 0
        ORDER BY confidence DESC
        LIMIT %s
        """,
        [field_id, max(int(limit), 0)],
    ).fetchall()
    return [_row_to_belief(r) for r in rows]


def recently_revised_beliefs(
    conn: MeshConnection, field_id: str, *, days: int = 7, limit: int = 5
) -> list[Belief]:
    """Held beliefs whose statement/confidence moved inside the window."""
    rows = conn.execute(
        f"""
        SELECT {_BELIEF_COLS} FROM beliefs
        WHERE field_id = %s AND is_currently_held
          AND last_revised_at > now() - make_interval(days => %s)
          AND revision_count > 1
        ORDER BY last_revised_at DESC
        LIMIT %s
        """,
        [field_id, int(days), max(int(limit), 0)],
    ).fetchall()
    return [_row_to_belief(r) for r in rows]


def movement_stats(
    conn: MeshConnection, field_id: str, *, days: int = 7
) -> dict[str, int]:
    """What changed in the window: beliefs born, revised, and dropped from the
    held set. All derived from the append-only revision trail."""
    new_count = conn.execute(
        """
        SELECT count(*) FROM beliefs b
        WHERE b.field_id = %s AND b.is_currently_held
          AND (SELECT min(r.revised_at) FROM belief_revisions r WHERE r.belief_id = b.id)
              > now() - make_interval(days => %s)
        """,
        [field_id, int(days)],
    ).fetchone()[0]  # type: ignore[index]
    revised = conn.execute(
        """
        SELECT count(DISTINCT r.belief_id) FROM belief_revisions r
        JOIN beliefs b ON b.id = r.belief_id
        WHERE b.field_id = %s AND r.revised_at > now() - make_interval(days => %s)
        """,
        [field_id, int(days)],
    ).fetchone()[0]  # type: ignore[index]
    dropped = conn.execute(
        """
        SELECT count(*) FROM beliefs
        WHERE field_id = %s AND NOT is_currently_held
          AND last_revised_at > now() - make_interval(days => %s)
        """,
        [field_id, int(days)],
    ).fetchone()[0]  # type: ignore[index]
    held = conn.execute(
        "SELECT count(*) FROM beliefs WHERE field_id = %s AND is_currently_held",
        [field_id],
    ).fetchone()[0]  # type: ignore[index]
    return {
        "held_total": int(held),
        "new": int(new_count),
        "revised": int(revised),
        "dropped": int(dropped),
        "window_days": int(days),
    }


def open_gaps(
    conn: MeshConnection, field_id: str, *, limit: int = 5
) -> list[Investigation]:
    """Open discovery-origin investigations — what the system itself flagged as
    under-evidenced and is (or should be) chasing."""
    from mesh_db.investigations import list_investigations

    out: list[Investigation] = []
    for status in (InvestigationStatus.open, InvestigationStatus.in_progress):
        for inv in list_investigations(conn, status=status, limit=50, field_id=field_id):
            if inv.origin.value == "discovery":
                out.append(inv)
    out.sort(key=lambda i: i.priority, reverse=True)
    return out[: max(int(limit), 0)]


def field_overview_inputs(conn: MeshConnection, field_id: str) -> dict[str, Any]:
    """The compact snapshot the brief-writing skill grounds its narrative in."""
    strongest = strongest_beliefs(conn, field_id, limit=8)
    contested = contested_beliefs(conn, field_id, limit=5)
    revised = recently_revised_beliefs(conn, field_id, limit=5)
    stats = movement_stats(conn, field_id)
    gaps = open_gaps(conn, field_id, limit=5)
    return {
        "stats": stats,
        "strongest": [
            {"topic": b.topic, "statement": b.statement, "confidence": round(b.confidence, 2)}
            for b in strongest
        ],
        "contested": [
            {
                "topic": b.topic,
                "statement": b.statement,
                "contradictions": len(b.contradicting_claim_ids),
            }
            for b in contested
        ],
        "recently_revised": [
            {"topic": b.topic, "confidence": round(b.confidence, 2)} for b in revised
        ],
        "gaps": [
            {"question": g.question, "rationale": g.trigger_rationale or ""} for g in gaps
        ],
    }
