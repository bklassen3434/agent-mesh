"""Accuracy eval — sample held beliefs, ground each against the web, grade.

The output is a repeatable number (:attr:`AccuracyReport.accuracy_score`) plus
the list of beliefs the web *contradicts* — the concrete fix-list. Sampling is
deliberately biased toward the beliefs that matter: ``recent`` (most-recently
revised, i.e. the current picture) by default, or ``strong`` (highest
confidence — the ones stated most assertively) / ``random``.
"""
from __future__ import annotations

import random
from datetime import UTC, datetime

from mesh_db.beliefs import list_beliefs
from mesh_db.connection import MeshConnection
from mesh_db.fields import get_field, get_field_by_slug
from mesh_models.belief import Belief

from mesh_agents.eval.judge import BeliefJudge
from mesh_agents.eval.models import AccuracyReport, BeliefGrade, Verdict

DEFAULT_SAMPLE_SIZE = 15
_CANDIDATE_CAP = 200  # list_beliefs MAX_LIMIT; we sort/sample within this page


def _field_label(conn: MeshConnection, field_id: str) -> str:
    fld = get_field(conn, field_id)
    if fld is None:
        return field_id
    name = getattr(fld, "name", "") or field_id
    desc = getattr(fld, "description", "") or ""
    return f"{name} — {desc}" if desc else name


def sample_beliefs(
    conn: MeshConnection,
    field_id: str,
    *,
    sample_size: int,
    strategy: str,
    min_confidence: float,
    rng: random.Random | None = None,
) -> list[Belief]:
    """Pick which held beliefs to grade. Ordered so the returned slice is the
    most decision-relevant for the chosen strategy."""
    candidates = [
        b
        for b in list_beliefs(
            conn, currently_held=True, field_id=field_id, limit=_CANDIDATE_CAP
        )
        if b.confidence >= min_confidence
    ]
    if strategy == "strong":
        candidates.sort(key=lambda b: b.confidence, reverse=True)
    elif strategy == "random":
        (rng or random.Random()).shuffle(candidates)
    else:  # "recent" — list_beliefs already returns last_revised_at DESC
        strategy = "recent"
    return candidates[:sample_size]


def assess_accuracy(
    conn: MeshConnection,
    field_id: str,
    judge: BeliefJudge,
    *,
    sample_size: int = DEFAULT_SAMPLE_SIZE,
    strategy: str = "recent",
    min_confidence: float = 0.0,
    now: datetime | None = None,
    rng: random.Random | None = None,
) -> AccuracyReport:
    now = now or datetime.now(UTC)
    # Accept either a field id or a slug (the CLI passes a slug).
    if get_field(conn, field_id) is None:
        fld = get_field_by_slug(conn, field_id)
        if fld is not None:
            field_id = fld.id
    field_label = _field_label(conn, field_id)

    beliefs = sample_beliefs(
        conn,
        field_id,
        sample_size=sample_size,
        strategy=strategy,
        min_confidence=min_confidence,
        rng=rng,
    )

    grades: list[BeliefGrade] = []
    total_tokens = 0
    for belief in beliefs:
        result = judge.grade(belief.statement, field_label)
        total_tokens += int(result.get("tokens", 0) or 0)
        grades.append(
            BeliefGrade(
                belief_id=belief.id,
                statement=belief.statement,
                stored_confidence=belief.confidence,
                verdict=result.get("verdict", Verdict.unverifiable),
                judge_confidence=float(result.get("judge_confidence", 0.0) or 0.0),
                rationale=str(result.get("rationale", "")),
                evidence_urls=list(result.get("evidence_urls", []) or []),
            )
        )

    judge_model = getattr(judge, "model", "unknown")
    return AccuracyReport(
        field_id=field_id,
        generated_at=now,
        sample_size=len(grades),
        strategy=strategy,
        judge_model=judge_model,
        grades=grades,
        judge_tokens=total_tokens,
    )
