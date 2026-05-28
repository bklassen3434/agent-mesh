"""Phase 7b.1 tests for the belief_reproduction + belief_hype_substance views.

The views are recomputed on read, so the tests just seed a small fixture
DB, query the views, and assert on the values.
"""
from __future__ import annotations

from datetime import UTC, datetime

import duckdb
from mesh_db.beliefs import create_belief
from mesh_db.claims import create_claim
from mesh_db.entities import create_entity
from mesh_db.sources import create_source
from mesh_models.belief import Belief
from mesh_models.claim import Claim, FailureMode
from mesh_models.entity import Entity, EntityType
from mesh_models.source import Source, SourceType


def _seed_entity(conn: duckdb.DuckDBPyConnection) -> str:
    e = create_entity(conn, Entity(canonical_name="Model X", type=EntityType.model))
    return e.id


def _seed_source(
    conn: duckdb.DuckDBPyConnection,
    *,
    source_type: SourceType = SourceType.arxiv,
    url_suffix: str = "1",
) -> str:
    now = datetime.now(UTC)
    s = create_source(
        conn,
        Source(
            type=source_type,
            url=f"http://test/{url_suffix}",
            published_at=now,
            raw_content_hash=f"h-{url_suffix}",
            fetched_at=now,
        ),
    )
    return s.id


def _seed_claim(
    conn: duckdb.DuckDBPyConnection,
    *,
    entity_id: str,
    source_id: str,
    predicate: str = "achieves_score",
    obj: dict[str, object] | None = None,
    extracted_by_agent: str = "claim_extractor",
    failure_mode: FailureMode | None = None,
) -> str:
    c = create_claim(
        conn,
        Claim(
            predicate=predicate,
            subject_entity_id=entity_id,
            object=obj or {"benchmark": "MMLU", "score": 78.4},
            source_id=source_id,
            extracted_by_agent=extracted_by_agent,
            raw_excerpt="x",
            confidence=0.9,
            failure_mode=failure_mode,
        ),
    )
    return c.id


def test_reproduction_count_zero_for_belief_with_no_claims(
    tmp_db: duckdb.DuckDBPyConnection,
) -> None:
    b = create_belief(
        tmp_db,
        Belief(topic="t", statement="orphan", confidence=0.5, revision_count=0),
    )
    rows = tmp_db.execute(
        "SELECT reproduction_count FROM belief_reproduction WHERE belief_id = ?",
        [b.id],
    ).fetchall()
    assert rows == [(0,)]


def test_reproduction_count_matches_distinct_source_types_per_canonical(
    tmp_db: duckdb.DuckDBPyConnection,
) -> None:
    eid = _seed_entity(tmp_db)
    # Same canonical claim reported by three source types — counts as 3.
    src_arxiv = _seed_source(tmp_db, source_type=SourceType.arxiv, url_suffix="a")
    src_blog = _seed_source(tmp_db, source_type=SourceType.blog, url_suffix="b")
    src_lb = _seed_source(tmp_db, source_type=SourceType.leaderboard, url_suffix="l")
    obj = {"benchmark": "MMLU", "score": 78.4}
    c1 = _seed_claim(tmp_db, entity_id=eid, source_id=src_arxiv, obj=obj)
    c2 = _seed_claim(tmp_db, entity_id=eid, source_id=src_blog, obj=obj)
    c3 = _seed_claim(tmp_db, entity_id=eid, source_id=src_lb, obj=obj)
    b = create_belief(
        tmp_db,
        Belief(
            topic="t",
            statement="reproduced",
            confidence=0.8,
            supporting_claim_ids=[c1, c2, c3],
            revision_count=0,
        ),
    )
    rows = tmp_db.execute(
        "SELECT reproduction_count FROM belief_reproduction WHERE belief_id = ?",
        [b.id],
    ).fetchall()
    assert rows == [(3,)]


def test_reproduction_canonicalizes_close_scores(
    tmp_db: duckdb.DuckDBPyConnection,
) -> None:
    # 78.42 and 78.38 both round to 78.4 → same canonical, two source types.
    eid = _seed_entity(tmp_db)
    src_a = _seed_source(tmp_db, source_type=SourceType.arxiv, url_suffix="a")
    src_b = _seed_source(tmp_db, source_type=SourceType.blog, url_suffix="b")
    c1 = _seed_claim(tmp_db, entity_id=eid, source_id=src_a, obj={"benchmark": "MMLU", "score": 78.42})
    c2 = _seed_claim(tmp_db, entity_id=eid, source_id=src_b, obj={"benchmark": "MMLU", "score": 78.38})
    b = create_belief(
        tmp_db,
        Belief(
            topic="t", statement="approximately reproduced",
            confidence=0.8, supporting_claim_ids=[c1, c2], revision_count=0,
        ),
    )
    rows = tmp_db.execute(
        "SELECT reproduction_count FROM belief_reproduction WHERE belief_id = ?",
        [b.id],
    ).fetchall()
    assert rows == [(2,)]


def test_hype_substance_anchors_at_half_for_empty(
    tmp_db: duckdb.DuckDBPyConnection,
) -> None:
    b = create_belief(
        tmp_db,
        Belief(topic="t", statement="empty", confidence=0.5, revision_count=0),
    )
    rows = tmp_db.execute(
        "SELECT hype_substance_score FROM belief_hype_substance WHERE belief_id = ?",
        [b.id],
    ).fetchall()
    score = rows[0][0]
    assert 0.49 <= score <= 0.51


def test_hype_substance_rewards_diverse_sources_and_reproduction(
    tmp_db: duckdb.DuckDBPyConnection,
) -> None:
    eid = _seed_entity(tmp_db)
    obj = {"benchmark": "MMLU", "score": 78.4}
    types = [SourceType.arxiv, SourceType.blog, SourceType.leaderboard,
             SourceType.github]
    claim_ids = []
    for i, t in enumerate(types):
        sid = _seed_source(tmp_db, source_type=t, url_suffix=str(i))
        claim_ids.append(_seed_claim(tmp_db, entity_id=eid, source_id=sid, obj=obj))
    b = create_belief(
        tmp_db,
        Belief(
            topic="t", statement="reproduced", confidence=0.9,
            supporting_claim_ids=claim_ids, revision_count=0,
        ),
    )
    row = tmp_db.execute(
        "SELECT hype_substance_score FROM belief_hype_substance WHERE belief_id = ?",
        [b.id],
    ).fetchone()
    assert row is not None
    score = row[0]
    # 4 source types, reproduction=4 (capped to 3 in formula)
    # substance term contributes ~0.5 * (1.0 + 1.0)/2 = 0.5 on top of 0.5 anchor → 1.0
    assert score == 1.0


def test_hype_substance_penalizes_severe_skeptic_attacks(
    tmp_db: duckdb.DuckDBPyConnection,
) -> None:
    eid = _seed_entity(tmp_db)
    # One supporting claim from arxiv to give the belief some baseline
    src_supp = _seed_source(tmp_db, source_type=SourceType.arxiv, url_suffix="s")
    c_supp = _seed_claim(tmp_db, entity_id=eid, source_id=src_supp)
    # Three skeptic counter-claims, all with severe failure modes
    skeptic_ids = []
    for i in range(3):
        sid = _seed_source(tmp_db, source_type=SourceType.agent_reasoning, url_suffix=f"sk{i}")
        skeptic_ids.append(
            _seed_claim(
                tmp_db,
                entity_id=eid,
                source_id=sid,
                extracted_by_agent="skeptic",
                failure_mode=FailureMode.cherry_picked_evidence,
            )
        )
    b = create_belief(
        tmp_db,
        Belief(
            topic="t", statement="attacked", confidence=0.6,
            supporting_claim_ids=[c_supp],
            contradicting_claim_ids=skeptic_ids,
            revision_count=0,
        ),
    )
    row = tmp_db.execute(
        "SELECT hype_substance_score FROM belief_hype_substance WHERE belief_id = ?",
        [b.id],
    ).fetchone()
    assert row is not None
    score = row[0]
    # Should be below 0.5 — attacks dominate the modest supporting evidence.
    assert score < 0.5


def test_signal_columns_exposed_on_belief_hype_substance(
    tmp_db: duckdb.DuckDBPyConnection,
) -> None:
    b = create_belief(
        tmp_db,
        Belief(topic="t", statement="x", confidence=0.5, revision_count=0),
    )
    row = tmp_db.execute(
        """
        SELECT source_type_diversity, reproduction_count,
               skeptic_counter_claim_count, severe_failure_mode_count,
               claims_last_30d, hype_substance_score
        FROM belief_hype_substance WHERE belief_id = ?
        """,
        [b.id],
    ).fetchone()
    assert row == (0, 0, 0, 0, 0, 0.5)
