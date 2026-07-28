"""Grade ledger + the rolling all-beliefs coverage sweep."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from mesh_db.beliefs import create_belief
from mesh_models.belief import Belief
from mesh_models.belief_grade import BeliefGradeRow
from mesh_models.field import DEFAULT_FIELD_ID

_NOW = datetime(2026, 6, 20, 12, 0, 0, tzinfo=UTC)


def _reset(conn: Any) -> str:
    conn.execute("DELETE FROM belief_grades WHERE field_id = %s", [DEFAULT_FIELD_ID])
    return DEFAULT_FIELD_ID


def _grade(field_id: str, belief_id: str, weight: float, at: datetime) -> BeliefGradeRow:
    return BeliefGradeRow(
        field_id=field_id, belief_id=belief_id, verdict="supported",
        judge_confidence=0.9, weight=weight, graded_at=at,
    )


def test_record_and_windowed_accuracy(tmp_db: Any) -> None:
    from mesh_db.belief_grades import accuracy_over_window, record_grade

    field_id = _reset(tmp_db)
    record_grade(tmp_db, _grade(field_id, "b1", 1.0, _NOW - timedelta(hours=1)))
    record_grade(tmp_db, _grade(field_id, "b2", 0.0, _NOW - timedelta(hours=1)))
    record_grade(tmp_db, _grade(field_id, "b3", 0.5, _NOW - timedelta(days=10)))  # old

    n, mean = accuracy_over_window(tmp_db, field_id, _NOW - timedelta(days=1))
    assert n == 2 and mean == 0.5  # (1.0 + 0.0)/2 — the old one is outside the window
    _reset(tmp_db)


def test_last_graded_at_tracks_the_most_recent_per_belief(tmp_db: Any) -> None:
    from mesh_db.belief_grades import last_graded_at, record_grade

    field_id = _reset(tmp_db)
    record_grade(tmp_db, _grade(field_id, "b1", 1.0, _NOW - timedelta(days=3)))
    record_grade(tmp_db, _grade(field_id, "b1", 1.0, _NOW - timedelta(hours=2)))  # newer
    m = last_graded_at(tmp_db, field_id)
    assert m["b1"] == _NOW - timedelta(hours=2)
    _reset(tmp_db)


def test_record_grade_effect_through_gateway(tmp_db: Any) -> None:
    from mesh_db.belief_grades import accuracy_over_window
    from mesh_db.effects import apply_effects
    from mesh_models.effect import RecordGradeEffect

    field_id = _reset(tmp_db)
    rep = apply_effects(
        tmp_db, [RecordGradeEffect(grade=_grade(field_id, "b1", 1.0, _NOW))]
    )
    assert rep.grades_recorded == 1
    n, mean = accuracy_over_window(tmp_db, field_id, _NOW - timedelta(minutes=1))
    assert n == 1 and mean == 1.0
    _reset(tmp_db)


def test_coverage_strategy_grades_least_recently_graded_first(tmp_db: Any) -> None:
    from mesh_agents.eval.accuracy import sample_beliefs
    from mesh_db.belief_grades import record_grade

    field_id = _reset(tmp_db)
    # three held beliefs; b_old graded long ago, b_recent graded just now, b_never never.
    ids = {}
    for name in ("old", "recent", "never"):
        b = Belief(topic="t", statement=name, confidence=0.8, last_revised_at=_NOW)
        create_belief(tmp_db, b, field_id=field_id)
        ids[name] = b.id
    record_grade(tmp_db, _grade(field_id, ids["old"], 1.0, _NOW - timedelta(days=5)))
    record_grade(tmp_db, _grade(field_id, ids["recent"], 1.0, _NOW - timedelta(minutes=1)))

    picked = sample_beliefs(
        tmp_db, field_id, sample_size=2, strategy="coverage", min_confidence=0.0
    )
    picked_ids = [b.id for b in picked]
    # never-graded first, then the oldest graded — the recently-graded one is last.
    assert picked_ids[0] == ids["never"]
    assert ids["old"] in picked_ids
    assert ids["recent"] not in picked_ids
    tmp_db.execute("DELETE FROM belief_grades WHERE field_id = %s", [field_id])
    tmp_db.execute("DELETE FROM beliefs WHERE field_id = %s", [field_id])
