"""Tests for the accuracy + freshness eval (mesh_agents.eval).

Freshness runs against seeded sources (real DB, testcontainer). Accuracy runs
against a stub judge so no LLM/web is touched — we assert the sampling picks the
right beliefs and the scoring aggregates verdicts correctly.
"""
from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta

from mesh_agents.eval import assess_accuracy, assess_freshness
from mesh_agents.eval.judge import GradeResult, _parse_verdict
from mesh_agents.eval.models import (
    AccuracyReport,
    BeliefGrade,
    Verdict,
)
from mesh_db.beliefs import create_belief
from mesh_db.connection import MeshConnection
from mesh_db.sources import create_source
from mesh_models.belief import Belief
from mesh_models.source import Source, SourceType

_NOW = datetime(2026, 7, 26, 18, 0, tzinfo=UTC)


def _seed_source(
    conn: MeshConnection, type_: SourceType, published: datetime, *, field_id: str
) -> None:
    create_source(
        conn,
        Source(
            type=type_,
            url=f"https://example.test/{type_.value}/{published.isoformat()}",
            published_at=published,
            raw_content_hash=f"{type_.value}-{published.isoformat()}",
        ),
        field_id=field_id,
    )


def _seed_belief(
    conn: MeshConnection,
    statement: str,
    *,
    field_id: str,
    confidence: float,
    revised: datetime,
) -> str:
    b = Belief(
        topic="t",
        statement=statement,
        confidence=confidence,
        last_revised_at=revised,
    )
    create_belief(conn, b, field_id=field_id)
    return b.id


# --------------------------------------------------------------------------
# freshness
# --------------------------------------------------------------------------


def test_freshness_flags_stale_type_but_field_fresh(tmp_db: MeshConnection) -> None:
    fid = "ai-robotics"
    _seed_source(tmp_db, SourceType.blog, _NOW - timedelta(hours=5), field_id=fid)
    _seed_source(tmp_db, SourceType.rss, _NOW - timedelta(days=10), field_id=fid)

    report = assess_freshness(tmp_db, fid, stale_after_hours=36, now=_NOW)

    by_type = {r.source_type: r for r in report.rows}
    assert by_type["blog"].stale is False
    assert by_type["rss"].stale is True
    # Field is fresh overall because blog is recent, even though rss is stale.
    assert report.ok is True
    assert report.stale_types == ["rss"]
    assert report.newest_source_age_hours is not None
    assert report.newest_source_age_hours < 6


def test_freshness_stale_when_everything_old(tmp_db: MeshConnection) -> None:
    fid = "ai-robotics"
    _seed_source(tmp_db, SourceType.blog, _NOW - timedelta(days=4), field_id=fid)
    report = assess_freshness(tmp_db, fid, stale_after_hours=36, now=_NOW)
    assert report.ok is False
    assert report.stale_types == ["blog"]


def test_freshness_empty_field_is_stale(tmp_db: MeshConnection) -> None:
    report = assess_freshness(tmp_db, "ai-robotics", stale_after_hours=36, now=_NOW)
    assert report.rows == []
    assert report.newest_source_age_hours is None
    assert report.ok is False


# --------------------------------------------------------------------------
# accuracy — sampling + scoring with a stub judge
# --------------------------------------------------------------------------


class _StubJudge:
    """Grades by a canned {statement -> verdict} map; records what it saw."""

    model = "stub-judge"

    def __init__(self, verdicts: dict[str, Verdict]) -> None:
        self._verdicts = verdicts
        self.seen: list[str] = []

    def grade(self, statement: str, field_label: str) -> GradeResult:
        self.seen.append(statement)
        verdict = self._verdicts.get(statement, Verdict.unverifiable)
        return GradeResult(
            verdict=verdict,
            judge_confidence=0.9,
            rationale=f"stub:{verdict.value}",
            evidence_urls=["https://example.test/evidence"],
            tokens=100,
        )


def test_accuracy_scoring_excludes_unverifiable(tmp_db: MeshConnection) -> None:
    fid = "ai-robotics"
    _seed_belief(tmp_db, "true one", field_id=fid, confidence=0.8, revised=_NOW)
    _seed_belief(tmp_db, "half right", field_id=fid, confidence=0.7, revised=_NOW)
    _seed_belief(tmp_db, "false one", field_id=fid, confidence=0.6, revised=_NOW)
    _seed_belief(tmp_db, "cant tell", field_id=fid, confidence=0.6, revised=_NOW)

    judge = _StubJudge(
        {
            "true one": Verdict.supported,
            "half right": Verdict.partially_supported,
            "false one": Verdict.contradicted,
            "cant tell": Verdict.unverifiable,
        }
    )
    report = assess_accuracy(tmp_db, fid, judge, sample_size=10, now=_NOW)

    assert report.sample_size == 4
    assert report.verifiable == 3  # unverifiable excluded from denominator
    # (1.0 + 0.5 + 0.0) / 3
    assert report.accuracy_score is not None
    assert abs(report.accuracy_score - 0.5) < 1e-9
    assert report.unverifiable_rate == 0.25
    assert [g.statement for g in report.contradicted] == ["false one"]
    assert report.judge_tokens == 400


def test_accuracy_strong_strategy_samples_highest_confidence(
    tmp_db: MeshConnection,
) -> None:
    fid = "ai-robotics"
    _seed_belief(tmp_db, "low", field_id=fid, confidence=0.30, revised=_NOW)
    _seed_belief(tmp_db, "high", field_id=fid, confidence=0.95, revised=_NOW)
    _seed_belief(tmp_db, "mid", field_id=fid, confidence=0.60, revised=_NOW)

    judge = _StubJudge({})
    report = assess_accuracy(
        tmp_db, fid, judge, sample_size=2, strategy="strong", now=_NOW
    )
    assert judge.seen == ["high", "mid"]  # top-2 by confidence, in order
    assert report.strategy == "strong"


def test_accuracy_min_confidence_filters(tmp_db: MeshConnection) -> None:
    fid = "ai-robotics"
    _seed_belief(tmp_db, "weak", field_id=fid, confidence=0.2, revised=_NOW)
    _seed_belief(tmp_db, "solid", field_id=fid, confidence=0.8, revised=_NOW)
    judge = _StubJudge({"solid": Verdict.supported})
    report = assess_accuracy(
        tmp_db, fid, judge, sample_size=10, min_confidence=0.5, now=_NOW
    )
    assert judge.seen == ["solid"]
    assert report.accuracy_score == 1.0


def test_accuracy_all_unverifiable_scores_none(tmp_db: MeshConnection) -> None:
    fid = "ai-robotics"
    _seed_belief(tmp_db, "uuid-ish", field_id=fid, confidence=0.6, revised=_NOW)
    judge = _StubJudge({})  # everything -> unverifiable
    report = assess_accuracy(tmp_db, fid, judge, sample_size=5, now=_NOW)
    assert report.accuracy_score is None  # 0/0 reported as n/a, not 0%
    assert report.unverifiable_rate == 1.0


def test_random_strategy_is_deterministic_with_seed(tmp_db: MeshConnection) -> None:
    fid = "ai-robotics"
    for i in range(6):
        _seed_belief(tmp_db, f"b{i}", field_id=fid, confidence=0.6, revised=_NOW)
    r1 = assess_accuracy(
        tmp_db, fid, _StubJudge({}), sample_size=3, strategy="random",
        now=_NOW, rng=random.Random(42),
    )
    r2 = assess_accuracy(
        tmp_db, fid, _StubJudge({}), sample_size=3, strategy="random",
        now=_NOW, rng=random.Random(42),
    )
    assert [g.belief_id for g in r1.grades] == [g.belief_id for g in r2.grades]


# --------------------------------------------------------------------------
# judge verdict parsing (no network)
# --------------------------------------------------------------------------


def test_parse_verdict_takes_trailing_json() -> None:
    text = (
        "Let me research this. Based on NHL.com, the signing is confirmed.\n"
        '{"verdict": "supported", "confidence": 0.9, '
        '"rationale": "confirmed by NHL.com", "evidence_urls": ["https://nhl.com/x"]}'
    )
    verdict, conf, rationale, urls = _parse_verdict(text)
    assert verdict == Verdict.supported
    assert conf == 0.9
    assert "confirmed" in rationale
    assert urls == ["https://nhl.com/x"]


def test_parse_verdict_unparsable_is_unverifiable() -> None:
    verdict, conf, _, urls = _parse_verdict("no json here at all")
    assert verdict == Verdict.unverifiable
    assert conf == 0.0
    assert urls == []


def test_parse_verdict_bad_label_defaults_unverifiable() -> None:
    verdict, _, _, _ = _parse_verdict('{"verdict": "definitely-true"}')
    assert verdict == Verdict.unverifiable


def test_accuracy_report_scoring_helpers() -> None:
    report = AccuracyReport(
        field_id="f",
        generated_at=_NOW,
        sample_size=2,
        strategy="recent",
        judge_model="stub",
        grades=[
            BeliefGrade(
                belief_id="1", statement="a", stored_confidence=0.8,
                verdict=Verdict.supported, judge_confidence=0.9, rationale="",
            ),
            BeliefGrade(
                belief_id="2", statement="b", stored_confidence=0.5,
                verdict=Verdict.unverifiable, judge_confidence=0.1, rationale="",
            ),
        ],
    )
    assert report.verifiable == 1
    assert report.accuracy_score == 1.0
    assert report.unverifiable_rate == 0.5
