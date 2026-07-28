"""Append-only accuracy grade ledger.

Powers the rolling "grade all beliefs" sweep (order held beliefs by when they were
last graded) and the windowed accuracy signal a shadow experiment reads.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from mesh_models.belief_grade import BeliefGradeRow

from mesh_db.connection import MeshConnection


def record_grade(conn: MeshConnection, grade: BeliefGradeRow) -> BeliefGradeRow:
    conn.execute(
        """
        INSERT INTO belief_grades
            (id, field_id, belief_id, verdict, judge_confidence, weight, graded_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        """,
        [
            grade.id,
            grade.field_id,
            grade.belief_id,
            grade.verdict,
            grade.judge_confidence,
            grade.weight,
            grade.graded_at,
        ],
    )
    return grade


def last_graded_at(conn: MeshConnection, field_id: str) -> dict[str, datetime]:
    """belief_id → most-recent graded_at for a field. The rolling sweep grades the
    beliefs missing from this map (never graded) first, then the oldest."""
    rows = conn.execute(
        """
        SELECT belief_id, max(graded_at)
        FROM belief_grades
        WHERE field_id = %s
        GROUP BY belief_id
        """,
        [field_id],
    ).fetchall()
    return {r[0]: r[1] for r in rows}


def accuracy_over_window(
    conn: MeshConnection, field_id: str, since: datetime
) -> tuple[int, float | None]:
    """(#grades, mean weight) for a field since ``since`` — the windowed accuracy.
    Mean is ``None`` when nothing was graded in the window."""
    row = conn.execute(
        """
        SELECT count(*), avg(weight)
        FROM belief_grades
        WHERE field_id = %s AND graded_at >= %s
        """,
        [field_id, since],
    ).fetchone()
    n = int(row[0]) if row and row[0] is not None else 0
    mean = float(row[1]) if row and row[1] is not None else None
    return n, mean


def _row_to_grade(row: tuple[Any, ...]) -> BeliefGradeRow:
    id_, field_id, belief_id, verdict, jc, weight, graded_at = row[:7]
    return BeliefGradeRow(
        id=id_,
        field_id=field_id,
        belief_id=belief_id,
        verdict=verdict,
        judge_confidence=float(jc) if jc is not None else 0.0,
        weight=float(weight) if weight is not None else 0.0,
        graded_at=graded_at
        if isinstance(graded_at, datetime)
        else datetime.fromisoformat(str(graded_at)),
    )
