"""Evidence-derived belief confidence (Phase 14d).

Pure tests pin the mapping; DB tests verify the exit criteria on real rows via
the belief_signals view: more independent sources score higher, a severe
skeptic critique scores lower.
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest
from mesh_agents.confidence import (
    BeliefSignals,
    ConfidenceWeights,
    compute_confidence,
)
from mesh_db.beliefs import create_belief, get_belief_signals
from mesh_db.claims import create_claim
from mesh_db.connection import MeshConnection
from mesh_db.entities import create_entity
from mesh_db.sources import create_source
from mesh_models.belief import Belief
from mesh_models.claim import Claim, FailureMode
from mesh_models.entity import Entity, EntityType
from mesh_models.source import Source, SourceType

# --- pure mapping ----------------------------------------------------------


def test_no_signals_returns_base() -> None:
    assert compute_confidence(BeliefSignals()) == pytest.approx(0.5)


def test_more_source_diversity_scores_higher() -> None:
    low = compute_confidence(BeliefSignals(source_type_diversity=1))
    high = compute_confidence(BeliefSignals(source_type_diversity=4))
    assert high > low > 0.5


def test_reproduction_lifts_confidence() -> None:
    base = compute_confidence(BeliefSignals(source_type_diversity=1))
    reproduced = compute_confidence(
        BeliefSignals(source_type_diversity=1, reproduction_count=3)
    )
    assert reproduced > base


def test_severe_critique_scores_lower() -> None:
    clean = compute_confidence(BeliefSignals(source_type_diversity=2))
    critiqued = compute_confidence(
        BeliefSignals(
            source_type_diversity=2,
            skeptic_counter_claim_count=2,
            severe_failure_mode_count=2,
        )
    )
    assert critiqued < clean


def test_confidence_is_clamped() -> None:
    crushed = compute_confidence(
        BeliefSignals(skeptic_counter_claim_count=99, severe_failure_mode_count=99)
    )
    assert crushed == 0.0
    maxed = compute_confidence(
        BeliefSignals(source_type_diversity=99, reproduction_count=99)
    )
    assert maxed == pytest.approx(1.0)


def test_weights_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MESH_CONFIDENCE_BASE", "0.3")
    monkeypatch.setenv("MESH_CONFIDENCE_ATTACK_WEIGHT", "0.9")
    w = ConfidenceWeights.from_env()
    assert w.base == 0.3 and w.attack_weight == 0.9


def test_zero_cap_does_not_divide_by_zero() -> None:
    w = ConfidenceWeights(source_diversity_cap=0.0, reproduction_cap=0.0)
    assert compute_confidence(BeliefSignals(source_type_diversity=5), w) == pytest.approx(
        0.5
    )


# --- on real rows via belief_signals ---------------------------------------


def _source(conn: MeshConnection, stype: SourceType, suffix: str) -> str:
    now = datetime.now(UTC)
    s = create_source(
        conn,
        Source(
            type=stype, url=f"http://t/{suffix}", published_at=now,
            raw_content_hash=f"h-{suffix}", fetched_at=now,
        ),
    )
    return s.id


def _claim(
    conn: MeshConnection, eid: str, sid: str, *,
    agent: str = "claim_extractor", failure_mode: FailureMode | None = None,
) -> str:
    c = create_claim(
        conn,
        Claim(
            predicate="achieves_score", subject_entity_id=eid,
            object={"benchmark": "MMLU", "score": 78.4}, source_id=sid,
            extracted_by_agent=agent, raw_excerpt="x", confidence=0.9,
            failure_mode=failure_mode,
        ),
    )
    return c.id


def test_multi_source_belief_outscores_single_source(tmp_db: MeshConnection) -> None:
    eid = create_entity(tmp_db, Entity(canonical_name="X", type=EntityType.model)).id
    single = create_belief(
        tmp_db,
        Belief(topic="t1", statement="s", supporting_claim_ids=[
            _claim(tmp_db, eid, _source(tmp_db, SourceType.arxiv, "1"))
        ]),
    )
    multi = create_belief(
        tmp_db,
        Belief(topic="t2", statement="s", supporting_claim_ids=[
            _claim(tmp_db, eid, _source(tmp_db, SourceType.arxiv, "2")),
            _claim(tmp_db, eid, _source(tmp_db, SourceType.blog, "3")),
            _claim(tmp_db, eid, _source(tmp_db, SourceType.leaderboard, "4")),
        ]),
    )
    c_single = compute_confidence(BeliefSignals.from_row(get_belief_signals(tmp_db, single.id)))
    c_multi = compute_confidence(BeliefSignals.from_row(get_belief_signals(tmp_db, multi.id)))
    assert c_multi > c_single


def test_severe_skeptic_critique_lowers_confidence(tmp_db: MeshConnection) -> None:
    eid = create_entity(tmp_db, Entity(canonical_name="Y", type=EntityType.model)).id
    support = _claim(tmp_db, eid, _source(tmp_db, SourceType.arxiv, "s1"))
    attack = _claim(
        tmp_db, eid, _source(tmp_db, SourceType.arxiv, "s2"),
        agent="skeptic", failure_mode=FailureMode.methodological_flaw,
    )
    clean = create_belief(
        tmp_db, Belief(topic="clean", statement="s", supporting_claim_ids=[support])
    )
    attacked = create_belief(
        tmp_db,
        Belief(
            topic="attacked", statement="s",
            supporting_claim_ids=[support], contradicting_claim_ids=[attack],
        ),
    )
    c_clean = compute_confidence(BeliefSignals.from_row(get_belief_signals(tmp_db, clean.id)))
    c_attacked = compute_confidence(
        BeliefSignals.from_row(get_belief_signals(tmp_db, attacked.id))
    )
    assert c_attacked < c_clean
