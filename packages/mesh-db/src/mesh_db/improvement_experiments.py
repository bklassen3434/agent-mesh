"""Shadow A/B experiments — a candidate change tested beside the live pipeline.

open → (shadow-eval accumulates per-arm scores) → decide (promote/reject). Running
per-arm sums live on the row; the decision reads them. Never deletes a row.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from mesh_models.improvement_experiment import (
    ExperimentArm,
    ExperimentStatus,
    ImprovementExperiment,
)

from mesh_db.connection import MeshConnection

_COLS = (
    "id, field_id, component, target, treatment_prompt, concern_ids, status, "
    "control_n, control_score_sum, treatment_n, treatment_score_sum, "
    "min_sample, margin, rationale, started_at, decided_at"
)


def open_experiment(
    conn: MeshConnection, exp: ImprovementExperiment
) -> ImprovementExperiment | None:
    """Open a running experiment. Returns ``None`` if one is already running for the
    same (field, component, target) — the partial-unique index blocks a duplicate."""
    row = conn.execute(
        f"""
        INSERT INTO improvement_experiments ({_COLS})
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (field_id, component, target) WHERE status = 'running'
            DO NOTHING
        RETURNING id
        """,
        [
            exp.id, exp.field_id, exp.component, exp.target, exp.treatment_prompt,
            list(exp.concern_ids), exp.status.value, exp.control_n, exp.control_score_sum,
            exp.treatment_n, exp.treatment_score_sum, exp.min_sample, exp.margin,
            exp.rationale, exp.started_at, exp.decided_at,
        ],
    ).fetchone()
    return exp if row is not None else None


def record_sample(
    conn: MeshConnection, experiment_id: str, arm: ExperimentArm, score: float
) -> None:
    """Fold one graded shadow sample into an arm's running mean."""
    col = "control" if arm is ExperimentArm.control else "treatment"
    conn.execute(
        f"""
        UPDATE improvement_experiments
        SET {col}_n = {col}_n + 1, {col}_score_sum = {col}_score_sum + %s
        WHERE id = %s AND status = 'running'
        """,
        [score, experiment_id],
    )


def decide_experiment(
    conn: MeshConnection, experiment_id: str, *, promoted: bool,
    rationale: str = "", now: datetime | None = None,
) -> None:
    """Close a running experiment as promoted or rejected (append-only: the row and
    its accumulated scores stay; only status/decided_at change)."""
    status = ExperimentStatus.promoted if promoted else ExperimentStatus.rejected
    conn.execute(
        """
        UPDATE improvement_experiments
        SET status = %s, rationale = %s, decided_at = coalesce(%s, now())
        WHERE id = %s AND status = 'running'
        """,
        [status.value, rationale, now, experiment_id],
    )


def get_running_experiment(
    conn: MeshConnection, field_id: str, component: str, target: str
) -> ImprovementExperiment | None:
    row = conn.execute(
        f"""
        SELECT {_COLS} FROM improvement_experiments
        WHERE field_id = %s AND component = %s AND target = %s AND status = 'running'
        LIMIT 1
        """,
        [field_id, component, target],
    ).fetchone()
    return _row_to_exp(row) if row else None


def list_running_experiments(
    conn: MeshConnection, field_id: str
) -> list[ImprovementExperiment]:
    rows = conn.execute(
        f"""
        SELECT {_COLS} FROM improvement_experiments
        WHERE field_id = %s AND status = 'running'
        ORDER BY started_at ASC
        """,
        [field_id],
    ).fetchall()
    return [_row_to_exp(r) for r in rows]


def _row_to_exp(row: tuple[Any, ...]) -> ImprovementExperiment:
    (
        id_, field_id, component, target, treatment_prompt, concern_ids, status,
        control_n, control_score_sum, treatment_n, treatment_score_sum,
        min_sample, margin, rationale, started_at, decided_at,
    ) = row[:16]
    return ImprovementExperiment(
        id=id_, field_id=field_id, component=component, target=target,
        treatment_prompt=treatment_prompt or "",
        concern_ids=list(concern_ids) if concern_ids else [],
        status=ExperimentStatus(status),
        control_n=int(control_n), control_score_sum=float(control_score_sum),
        treatment_n=int(treatment_n), treatment_score_sum=float(treatment_score_sum),
        min_sample=int(min_sample), margin=float(margin), rationale=rationale or "",
        started_at=_dt(started_at),
        decided_at=_dt(decided_at) if decided_at is not None else None,
    )


def _dt(v: Any) -> datetime:
    return v if isinstance(v, datetime) else datetime.fromisoformat(str(v))
