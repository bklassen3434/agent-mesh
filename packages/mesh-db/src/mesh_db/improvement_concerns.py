"""Accumulated fault-attributions — the self-improvement loop's evidence store.

An accuracy/freshness eval records concerns here (it edits nothing else); a derived
tension counts the OPEN rows per (field, component, target) to decide when enough
evidence has accumulated to fire an A/B improve pass. Content is append-only; only
``status`` flips at the end of an improve pass.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from mesh_models.improvement_concern import (
    ConcernComponent,
    ConcernStatus,
    ImprovementConcern,
)

from mesh_db.connection import MeshConnection

_COLS = (
    "id, field_id, component, target, belief_id, verdict, severity, summary, "
    "evidence_urls, status, created_at, resolved_at, resolved_by"
)


def record_concern(
    conn: MeshConnection, concern: ImprovementConcern
) -> ImprovementConcern | None:
    """Insert one concern. Returns it, or ``None`` when an OPEN concern for the same
    ``(field, component, target, belief)`` already exists — a re-eval of a still-open
    fault is not new evidence, so the partial-unique-open index de-dupes it and the
    count stays honest."""
    row = conn.execute(
        f"""
        INSERT INTO improvement_concerns ({_COLS})
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (field_id, component, target, belief_id)
            WHERE status = 'open'
            DO NOTHING
        RETURNING id
        """,
        [
            concern.id,
            concern.field_id,
            concern.component.value,
            concern.target,
            concern.belief_id,
            concern.verdict,
            concern.severity,
            concern.summary,
            list(concern.evidence_urls),
            concern.status.value,
            concern.created_at,
            concern.resolved_at,
            concern.resolved_by,
        ],
    ).fetchone()
    return concern if row is not None else None


class OpenConcernGroup:
    """A (component, target) bucket of open concerns + how much evidence it holds —
    what the derived tension thresholds on."""

    def __init__(self, component: str, target: str, count: int, severity: float) -> None:
        self.component = component
        self.target = target
        self.count = count
        self.severity = severity


def open_concern_groups(
    conn: MeshConnection, field_id: str
) -> list[OpenConcernGroup]:
    """Open concerns for ``field_id`` bucketed by (component, target), with the row
    count and summed severity — the activation signal the producer thresholds on.
    Ordered by summed severity descending (steepest first)."""
    rows = conn.execute(
        """
        SELECT component, target, count(*), coalesce(sum(severity), 0)
        FROM improvement_concerns
        WHERE field_id = %s AND status = 'open'
        GROUP BY component, target
        ORDER BY sum(severity) DESC
        """,
        [field_id],
    ).fetchall()
    return [OpenConcernGroup(r[0], r[1], int(r[2]), float(r[3])) for r in rows]


def list_open_concerns(
    conn: MeshConnection,
    field_id: str,
    component: ConcernComponent | str,
    target: str | None = None,
    *,
    limit: int = 100,
) -> list[ImprovementConcern]:
    """The open concerns for one (field, component[, target]) — the evidence an
    improve pass reads to draft its change, newest first."""
    comp = component.value if isinstance(component, ConcernComponent) else component
    params: list[Any] = [field_id, comp]
    target_clause = ""
    if target is not None:
        target_clause = "AND target = %s"
        params.append(target)
    params.append(limit)
    rows = conn.execute(
        f"""
        SELECT {_COLS}
        FROM improvement_concerns
        WHERE field_id = %s AND component = %s AND status = 'open' {target_clause}
        ORDER BY created_at DESC
        LIMIT %s
        """,
        params,
    ).fetchall()
    return [_row_to_concern(r) for r in rows]


def resolve_concerns(
    conn: MeshConnection,
    concern_ids: list[str],
    *,
    resolved_by: str = "",
    status: ConcernStatus = ConcernStatus.resolved,
    now: datetime | None = None,
) -> int:
    """Close the given concerns (open → resolved/dismissed). Append-only: rows are
    kept, only ``status``/``resolved_*`` change. Returns the number closed."""
    if not concern_ids:
        return 0
    cur = conn.execute(
        """
        UPDATE improvement_concerns
        SET status = %s, resolved_by = %s, resolved_at = coalesce(%s, now())
        WHERE id = ANY(%s) AND status = 'open'
        """,
        [status.value, resolved_by, now, list(concern_ids)],
    )
    return cur.rowcount if cur.rowcount is not None else 0


def _row_to_concern(row: tuple[Any, ...]) -> ImprovementConcern:
    (
        id_, field_id, component, target, belief_id, verdict, severity, summary,
        evidence_urls, status, created_at, resolved_at, resolved_by,
    ) = row[:13]
    try:
        comp = ConcernComponent(component)
    except ValueError:
        comp = ConcernComponent.other
    return ImprovementConcern(
        id=id_,
        field_id=field_id,
        component=comp,
        target=target or "",
        belief_id=belief_id or "",
        verdict=verdict or "",
        severity=float(severity) if severity is not None else 0.0,
        summary=summary or "",
        evidence_urls=list(evidence_urls) if evidence_urls else [],
        status=ConcernStatus(status),
        created_at=_dt(created_at),
        resolved_at=_dt(resolved_at) if resolved_at is not None else None,
        resolved_by=resolved_by or "",
    )


def _dt(v: Any) -> datetime:
    return v if isinstance(v, datetime) else datetime.fromisoformat(str(v))
