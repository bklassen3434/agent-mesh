from __future__ import annotations

import psycopg
import pytest
from mesh_agents.consolidator import HeuristicProposal, validate_proposals
from mesh_db.connection import MeshConnection
from mesh_db.heuristics import get_heuristic_by_id, list_heuristic_revisions
from mesh_pipeline._heuristics import (
    MissingProvenanceError,
    persist_heuristic,
    revise_heuristic,
)


def _proposal(**kwargs: object) -> HeuristicProposal:
    defaults: dict[str, object] = {
        "agent": "skeptic",
        "skill": "challenge_belief",
        "heuristic": "Beliefs backed only by forum posts deserve a closer look.",
        "provenance_run_ids": ["run-1"],
        "rationale": "three forum-only beliefs were later contradicted",
    }
    defaults.update(kwargs)
    return HeuristicProposal(**defaults)  # type: ignore[arg-type]


def test_persist_creates_head_and_genesis_revision(tmp_db: MeshConnection) -> None:
    h = persist_heuristic(tmp_db, _proposal())
    fetched = get_heuristic_by_id(tmp_db, h.id)
    assert fetched is not None
    assert fetched.confidence == pytest.approx(0.3)  # low start
    assert fetched.revision_count == 0
    assert fetched.expires_at > fetched.created_at  # TTL set
    assert fetched.provenance_run_ids == ["run-1"]
    revs = list_heuristic_revisions(tmp_db, heuristic_id=h.id)
    assert len(revs) == 1
    assert revs[0].previous_heuristic == "" and revs[0].previous_confidence == 0.0


def test_persist_requires_provenance(tmp_db: MeshConnection) -> None:
    with pytest.raises(MissingProvenanceError):
        persist_heuristic(
            tmp_db, _proposal(provenance_run_ids=[], provenance_claim_ids=[])
        )


def test_revise_is_append_only_and_merges_provenance(tmp_db: MeshConnection) -> None:
    h = persist_heuristic(tmp_db, _proposal())
    revised = revise_heuristic(
        tmp_db,
        h.id,
        _proposal(
            heuristic="Forum-only beliefs: cap confidence and demand a second source.",
            confidence=0.45,
            provenance_run_ids=["run-2"],
        ),
    )
    assert revised is not None
    assert revised.revision_count == 1
    assert revised.confidence == pytest.approx(0.45)
    # Provenance only grows (union of prior + new).
    assert set(revised.provenance_run_ids) == {"run-1", "run-2"}
    revs = list_heuristic_revisions(tmp_db, heuristic_id=h.id)
    assert len(revs) == 2  # genesis + this revision


def test_validate_proposals_drops_provenance_less(tmp_db: MeshConnection) -> None:
    payload = {
        "proposals": [
            _proposal().model_dump(mode="json"),
            _proposal(provenance_run_ids=[], provenance_claim_ids=[]).model_dump(
                mode="json"
            ),
        ]
    }
    kept = validate_proposals(payload)
    assert len(kept) == 1


def test_reader_role_cannot_write_heuristics(_pg: str, tmp_db: MeshConnection) -> None:
    """A direct write from the agent/reader role is rejected by Postgres —
    only the coordinator-writer role persists heuristics (Phase 16b principle)."""
    reader_dsn = _pg.replace("test:test@", "mesh_reader:mesh_reader@")
    with psycopg.connect(reader_dsn, autocommit=True) as ro:
        ro.execute("SET search_path TO knowledge, agents, runtime, catalog, public")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            ro.execute(
                "INSERT INTO agent_heuristics "
                "(id, agent, skill, heuristic, created_at, last_revised_at, expires_at) "
                "VALUES ('x','skeptic','challenge_belief','h', now(), now(), now())"
            )


def test_writer_role_cannot_delete_heuristics(_pg: str, tmp_db: MeshConnection) -> None:
    """No DELETE is granted on the procedural store — the append-only /
    no-silent-overwrite invariant is enforced at the DB level (like claims)."""
    persist_heuristic(tmp_db, _proposal())
    writer_dsn = _pg.replace("test:test@", "mesh_writer:mesh_writer@")
    with psycopg.connect(writer_dsn, autocommit=True) as w:
        w.execute("SET search_path TO knowledge, agents, runtime, catalog, public")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            w.execute("DELETE FROM agent_heuristics")
