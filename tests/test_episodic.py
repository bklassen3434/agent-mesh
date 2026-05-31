"""Phase 15a: episodic read model (mesh_db.episodic.recall_history).

Seeds a known two-run scenario and cross-checks that recall_history
reconstructs each agent's first-person action history — correct ordering,
run linkage (via timestamp containment), derived skill, refs, and the scope
filters. Pure read model: no LLM, no writes beyond the seed.
"""
from __future__ import annotations

from datetime import UTC, datetime

from mesh_db.beliefs import create_belief
from mesh_db.claims import create_claim
from mesh_db.connection import MeshConnection
from mesh_db.entities import create_entity
from mesh_db.episodic import recall_history
from mesh_db.pipeline_runs import PipelineRun, create_pipeline_run
from mesh_db.revisions import create_revision
from mesh_db.sources import create_source
from mesh_models.belief import Belief
from mesh_models.claim import Claim
from mesh_models.entity import Entity, EntityType
from mesh_models.revision import BeliefRevision
from mesh_models.source import Source, SourceType


def _dt(hour: int, minute: int) -> datetime:
    return datetime(2026, 5, 31, hour, minute, 0, tzinfo=UTC)


def _seed(conn: MeshConnection) -> dict[str, str]:
    """A pipeline run (10:00-10:10) and a skeptic sweep (11:00-11:10).

    Run A: claim_extractor extracts 2 claims from S1 (entity E1) at 10:05;
           sota_tracker revises belief BL at 10:08.
    Run B: skeptic writes a counter-claim from S2 (entity E2) at 11:05 and a
           belief_revision on BL at 11:06.
    """
    run_a = create_pipeline_run(
        conn,
        PipelineRun(started_at=_dt(10, 0), finished_at=_dt(10, 10), run_type="pipeline"),
    )
    run_b = create_pipeline_run(
        conn,
        PipelineRun(
            started_at=_dt(11, 0), finished_at=_dt(11, 10), run_type="skeptic_sweep"
        ),
    )

    e1 = create_entity(conn, Entity(canonical_name="GPT-X", type=EntityType.model))
    e2 = create_entity(conn, Entity(canonical_name="BenchY", type=EntityType.benchmark))

    s1 = create_source(
        conn,
        Source(
            type=SourceType.arxiv,
            url="https://arxiv.org/abs/0001",
            published_at=_dt(9, 0),
            raw_content_hash="h1",
        ),
    )
    s2 = create_source(
        conn,
        Source(
            type=SourceType.agent_reasoning,
            url="agent://skeptic/belief/bl/x",
            author="skeptic",
            published_at=_dt(11, 5),
            raw_content_hash="h2",
        ),
    )

    c1 = create_claim(
        conn,
        Claim(
            predicate="achieves_score",
            subject_entity_id=e1.id,
            object={"benchmark": "BenchY", "score": 90.0},
            source_id=s1.id,
            extracted_by_agent="claim_extractor",
            raw_excerpt="...",
            extracted_at=_dt(10, 5),
        ),
    )
    create_claim(
        conn,
        Claim(
            predicate="has_capability",
            subject_entity_id=e1.id,
            object={"capability": "long context"},
            source_id=s1.id,
            extracted_by_agent="claim_extractor",
            raw_excerpt="...",
            extracted_at=_dt(10, 5),
        ),
    )
    cc = create_claim(
        conn,
        Claim(
            predicate="critiques",
            subject_entity_id=e2.id,
            object={"target": "BenchY"},
            source_id=s2.id,
            extracted_by_agent="skeptic",
            raw_excerpt="...",
            extracted_at=_dt(11, 5),
        ),
    )

    bl = create_belief(
        conn,
        Belief(topic="sota:BenchY", statement="GPT-X leads", supporting_claim_ids=[c1.id]),
    )
    create_revision(
        conn,
        BeliefRevision(
            belief_id=bl.id,
            previous_statement="old",
            new_statement="GPT-X leads",
            previous_confidence=0.5,
            new_confidence=0.7,
            trigger_claim_ids=[c1.id],
            revised_by_agent="sota_tracker",
            revised_at=_dt(10, 8),
            rationale="new score",
        ),
    )
    create_revision(
        conn,
        BeliefRevision(
            belief_id=bl.id,
            previous_statement="GPT-X leads",
            new_statement="GPT-X leads",  # skeptic does not rewrite the statement
            previous_confidence=0.7,
            new_confidence=0.6,
            trigger_claim_ids=[cc.id],
            revised_by_agent="skeptic",
            revised_at=_dt(11, 6),
            rationale="methodological concern",
        ),
    )
    return {
        "run_a": run_a.id, "run_b": run_b.id,
        "e1": e1.id, "e2": e2.id, "s1": s1.id, "s2": s2.id, "bl": bl.id,
    }


def test_claim_extractor_history(tmp_db: MeshConnection) -> None:
    ids = _seed(tmp_db)
    entries = recall_history(tmp_db, "claim_extractor")
    # Two claims from one source in one run collapse to a single extraction event.
    assert len(entries) == 1
    ev = entries[0]
    assert ev.event_type == "extraction"
    assert ev.skill == "extract_claims"
    assert ev.run_id == ids["run_a"]  # recovered via timestamp containment
    assert ev.refs["source_id"] == ids["s1"]
    assert len(ev.refs["claim_ids"]) == 2
    assert ids["e1"] in ev.refs["entity_ids"]
    assert "2 claim(s)" in ev.action_summary


def test_revision_agent_history(tmp_db: MeshConnection) -> None:
    ids = _seed(tmp_db)
    entries = recall_history(tmp_db, "sota_tracker")
    assert len(entries) == 1
    ev = entries[0]
    assert ev.event_type == "belief_revision"
    assert ev.skill == "update_sota"
    assert ev.run_id == ids["run_a"]
    assert ev.refs["belief_id"] == ids["bl"]
    assert ev.refs["trigger_claim_ids"]


def test_skeptic_history_merges_and_orders(tmp_db: MeshConnection) -> None:
    ids = _seed(tmp_db)
    entries = recall_history(tmp_db, "skeptic")
    # Skeptic produced both a counter-claim (extraction) and a revision, both in
    # run B. Most-recent-first: the 11:06 revision precedes the 11:05 extraction.
    assert [e.event_type for e in entries] == ["belief_revision", "extraction"]
    assert all(e.run_id == ids["run_b"] for e in entries)
    assert all(e.skill == "challenge_belief" for e in entries)
    assert entries[0].action_summary.startswith("Challenged belief")


def test_entity_scope_filter(tmp_db: MeshConnection) -> None:
    ids = _seed(tmp_db)
    assert recall_history(tmp_db, "claim_extractor", entity_id=ids["e1"])
    assert recall_history(tmp_db, "claim_extractor", entity_id=ids["e2"]) == []


def test_source_filter_excludes_revisions(tmp_db: MeshConnection) -> None:
    ids = _seed(tmp_db)
    # source_id is meaningful only for extraction events; revisions are dropped.
    entries = recall_history(tmp_db, "skeptic", source_id=ids["s2"])
    assert [e.event_type for e in entries] == ["extraction"]


def test_topic_filter_excludes_extractions(tmp_db: MeshConnection) -> None:
    _seed(tmp_db)
    # topic is meaningful only for revision events; extractions are dropped.
    entries = recall_history(tmp_db, "skeptic", topic="benchy")
    assert [e.event_type for e in entries] == ["belief_revision"]
    assert recall_history(tmp_db, "skeptic", topic="no-such-topic") == []


def test_time_window_and_limit(tmp_db: MeshConnection) -> None:
    _seed(tmp_db)
    # Window that excludes the 11:06 revision but keeps the 11:05 extraction.
    entries = recall_history(tmp_db, "skeptic", until=_dt(11, 5))
    assert [e.event_type for e in entries] == ["extraction"]
    # Limit caps the merged, sorted result.
    assert len(recall_history(tmp_db, "skeptic", limit=1)) == 1


def test_run_id_none_when_outside_any_run(tmp_db: MeshConnection) -> None:
    e = create_entity(tmp_db, Entity(canonical_name="Z", type=EntityType.model))
    s = create_source(
        tmp_db,
        Source(type=SourceType.arxiv, url="u", published_at=_dt(8, 0), raw_content_hash="h"),
    )
    create_claim(
        tmp_db,
        Claim(
            predicate="has_capability",
            subject_entity_id=e.id,
            object={"capability": "x"},
            source_id=s.id,
            extracted_by_agent="claim_extractor",
            raw_excerpt="...",
            extracted_at=_dt(8, 30),  # no pipeline_runs row → no containing window
        ),
    )
    entries = recall_history(tmp_db, "claim_extractor")
    assert len(entries) == 1
    assert entries[0].run_id is None
