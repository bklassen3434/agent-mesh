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
from mesh_db.episodic import EpisodicEntry, recall_history
from mesh_db.investigations import create_investigation
from mesh_db.pipeline_runs import PipelineRun, create_pipeline_run
from mesh_db.revisions import create_revision
from mesh_db.sources import create_source
from mesh_models.belief import Belief
from mesh_models.claim import Claim, ClaimStatus, FailureMode
from mesh_models.entity import Entity, EntityType
from mesh_models.investigation import Investigation, InvestigationStatus
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


# ── 15b: outcome tagging ──────────────────────────────────────────────────────


def _src(conn: MeshConnection, n: int) -> str:
    return create_source(
        conn,
        Source(
            type=SourceType.arxiv,
            url=f"https://arxiv.org/abs/{n:04d}",
            published_at=_dt(9, 0),
            raw_content_hash=f"oh{n}",
        ),
    ).id


def _claim(
    conn: MeshConnection,
    entity_id: str,
    source_id: str,
    *,
    agent: str = "claim_extractor",
    status: ClaimStatus = ClaimStatus.active,
    failure_mode: FailureMode | None = None,
) -> str:
    return create_claim(
        conn,
        Claim(
            predicate="has_capability",
            subject_entity_id=entity_id,
            object={"capability": "x"},
            source_id=source_id,
            extracted_by_agent=agent,
            raw_excerpt="...",
            status=status,
            failure_mode=failure_mode,
            extracted_at=_dt(10, 5),
        ),
    ).id


def _outcomes_seed(conn: MeshConnection) -> dict[str, str]:
    """A pipeline run with claims of known, distinct fates — each on its own
    source so it forms a separate (cleanly-labelled) extraction event."""
    create_pipeline_run(
        conn, PipelineRun(started_at=_dt(10, 0), finished_at=_dt(10, 10))
    )
    ent = create_entity(conn, Entity(canonical_name="M", type=EntityType.model))

    s_surv, s_cont, s_skept = _src(conn, 1), _src(conn, 2), _src(conn, 3)
    s_res, s_aband, s_sup = _src(conn, 4), _src(conn, 5), _src(conn, 6)

    c_surv = _claim(conn, ent.id, s_surv)
    c_cont = _claim(conn, ent.id, s_cont)
    cc = _claim(
        conn, ent.id, s_skept, agent="skeptic",
        failure_mode=FailureMode.methodological_flaw,
    )
    c_res = _claim(conn, ent.id, s_res)
    c_aband = _claim(conn, ent.id, s_aband)
    _claim(conn, ent.id, s_sup, status=ClaimStatus.superseded)

    # c_surv supports a held belief with no contradictions → survived.
    create_belief(
        conn, Belief(topic="b:held", statement="s", supporting_claim_ids=[c_surv])
    )
    # c_cont supports a held belief that drew a skeptic counter-claim (cc) →
    # contested; cc is applied as contradicting evidence.
    create_belief(
        conn,
        Belief(
            topic="b:attacked", statement="s",
            supporting_claim_ids=[c_cont], contradicting_claim_ids=[cc],
        ),
    )
    # Investigations collecting the produced claims, with known terminal states.
    create_investigation(
        conn,
        Investigation(
            question="q1", status=InvestigationStatus.resolved,
            collected_claim_ids=[c_res],
        ),
    )
    create_investigation(
        conn,
        Investigation(
            question="q2", status=InvestigationStatus.abandoned,
            collected_claim_ids=[c_aband],
        ),
    )
    return {"source_surv": s_surv, "source_cont": s_cont, "source_skept": s_skept,
            "source_res": s_res, "source_aband": s_aband, "source_sup": s_sup}


def _by_source(entries: list[EpisodicEntry], source_id: str) -> EpisodicEntry:
    [ev] = [e for e in entries if e.refs.get("source_id") == source_id]
    return ev


def test_outcome_survived_vs_contradicted(tmp_db: MeshConnection) -> None:
    ids = _outcomes_seed(tmp_db)
    entries = recall_history(tmp_db, "claim_extractor")

    survived = _by_source(entries, ids["source_surv"])
    assert survived.outcome.label == "survived"
    assert survived.outcome.claims_supporting == 1
    assert survived.outcome.claims_contested == 0

    contradicted = _by_source(entries, ids["source_cont"])
    assert contradicted.outcome.label == "contradicted"
    assert contradicted.outcome.claims_contested == 1

    superseded = _by_source(entries, ids["source_sup"])
    assert superseded.outcome.label == "superseded"
    assert superseded.outcome.claims_superseded == 1


def test_outcome_skeptic_counter_claim_applied(tmp_db: MeshConnection) -> None:
    ids = _outcomes_seed(tmp_db)
    entries = recall_history(tmp_db, "skeptic")
    applied = _by_source(entries, ids["source_skept"])
    assert applied.outcome.label == "applied"
    assert applied.outcome.claims_contradicting == 1
    assert "methodological_flaw" in applied.outcome.failure_modes


def test_outcome_investigation_status(tmp_db: MeshConnection) -> None:
    ids = _outcomes_seed(tmp_db)
    entries = recall_history(tmp_db, "claim_extractor")
    assert _by_source(entries, ids["source_res"]).outcome.investigations == {
        "resolved": 1
    }
    assert _by_source(entries, ids["source_aband"]).outcome.investigations == {
        "abandoned": 1
    }


def test_outcome_belief_held_vs_retired(tmp_db: MeshConnection) -> None:
    create_pipeline_run(
        tmp_db, PipelineRun(started_at=_dt(10, 0), finished_at=_dt(10, 10))
    )
    held = create_belief(tmp_db, Belief(topic="b:live", statement="s"))
    retired = create_belief(
        tmp_db, Belief(topic="b:dead", statement="s", is_currently_held=False)
    )
    create_revision(
        tmp_db,
        BeliefRevision(
            belief_id=held.id, previous_statement="a", new_statement="b",
            previous_confidence=0.5, new_confidence=0.6, revised_by_agent="sota_tracker",
            revised_at=_dt(10, 3), rationale="r",
        ),
    )
    create_revision(
        tmp_db,
        BeliefRevision(
            belief_id=retired.id, previous_statement="a", new_statement="b",
            previous_confidence=0.5, new_confidence=0.2, revised_by_agent="synthesizer",
            revised_at=_dt(10, 4), rationale="r",
        ),
    )
    held_ev = _by_belief(recall_history(tmp_db, "sota_tracker"), held.id)
    assert held_ev.outcome.label == "held"
    assert held_ev.outcome.belief_currently_held is True
    retired_ev = _by_belief(recall_history(tmp_db, "synthesizer"), retired.id)
    assert retired_ev.outcome.label == "retired"
    assert retired_ev.outcome.belief_currently_held is False


def _by_belief(entries: list[EpisodicEntry], belief_id: str) -> EpisodicEntry:
    [ev] = [e for e in entries if e.refs.get("belief_id") == belief_id]
    return ev
