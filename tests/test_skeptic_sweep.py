from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

from mesh_db.beliefs import create_belief, get_belief_by_id
from mesh_db.claims import create_claim, list_claims
from mesh_db.connection import get_connection
from mesh_db.entities import create_entity
from mesh_db.migrations import apply_migrations
from mesh_db.pipeline_runs import list_pipeline_runs
from mesh_db.revisions import list_revisions
from mesh_db.sources import create_source, list_sources
from mesh_models.belief import Belief
from mesh_models.claim import Claim
from mesh_models.entity import Entity, EntityType
from mesh_models.source import Source, SourceType


def _seed(db_path: str) -> tuple[str, str]:
    """Seed one belief + supporting claim + entity. Returns (belief_id, entity_id)."""
    conn = get_connection(db_path)
    apply_migrations(conn)

    entity = Entity(canonical_name="TestModel-7B", type=EntityType.model)
    create_entity(conn, entity)

    source = Source(
        type=SourceType.arxiv,
        url="https://arxiv.org/abs/2023.06.0001",
        published_at=datetime(2023, 6, 15, tzinfo=UTC),
        raw_content_hash="hash-arxiv-seed",
    )
    create_source(conn, source)

    claim = Claim(
        predicate="achieves_score",
        subject_entity_id=entity.id,
        object={"score": 87.5, "benchmark": "MMLU", "metric": "accuracy"},
        source_id=source.id,
        extracted_by_agent="claim_extractor",
        raw_excerpt="TestModel-7B achieves 87.5% on MMLU",
        confidence=0.95,
    )
    create_claim(conn, claim)

    belief = Belief(
        topic="sota:MMLU",
        statement="TestModel-7B achieves 87.5% on MMLU (as of 2023-06-15)",
        supporting_claim_ids=[claim.id],
        confidence=0.7,
        last_revised_at=datetime.now(UTC) - timedelta(days=120),
    )
    create_belief(conn, belief)
    conn.close()
    return belief.id, entity.id


class _FakeA2AClient:
    """In-memory stand-in for MeshA2AClient.

    Records call_skill invocations and returns canned results keyed by skill_id.
    """

    def __init__(self, responses: dict[str, Any]) -> None:
        self._responses = responses
        self.discovered: list[str] = []
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def __aenter__(self) -> _FakeA2AClient:
        return self

    async def __aexit__(self, *args: Any) -> None:
        pass

    async def discover(self, base_urls: list[str]) -> dict[str, str]:
        self.discovered = list(base_urls)
        return {sid: f"http://fake/{sid}" for sid in self._responses}

    async def call_skill(
        self, skill_id: str, payload: dict[str, Any], *, traceparent: str | None = None
    ) -> dict[str, Any]:
        self.calls.append((skill_id, payload))
        result: dict[str, Any] = self._responses[skill_id]
        return result

    async def call_skill_blocking(
        self,
        skill_id: str,
        payload: dict[str, Any],
        *,
        traceparent: str | None = None,
        poll_interval: float | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        return await self.call_skill(skill_id, payload, traceparent=traceparent)


def _patch_a2a(responses: dict[str, Any]) -> tuple[Any, _FakeA2AClient]:
    fake = _FakeA2AClient(responses)
    return patch(
        "mesh_pipeline.skeptic_sweep.MeshA2AClient", return_value=fake
    ), fake


def _run(db_path: str, responses: dict[str, Any]) -> _FakeA2AClient:
    from mesh_pipeline.skeptic_sweep import run_skeptic_sweep

    cm, fake = _patch_a2a(responses)
    with cm:
        asyncio.run(run_skeptic_sweep(db_path=db_path))
    fake_client: _FakeA2AClient = fake
    return fake_client


def test_weakened_assessment_inserts_counter_claim_and_revision(tmp_path: Path) -> None:
    db = str(tmp_path / "sweep.db")
    belief_id, entity_id = _seed(db)

    responses = {
        "select_beliefs_to_challenge": {
            "picks": [
                {"belief_id": belief_id, "score": 2.5, "rationale": "stale + thin support"}
            ]
        },
        "challenge_belief": {
            "verdict": "weakened",
            "confidence": 0.85,
            "rationale": "Supporting claim is from 2023-06-15 — over a year stale.",
            "suggested_confidence_delta": -0.2,
            "counter_claims": [
                {
                    "predicate": "achieves_score",
                    "subject_entity_id": entity_id,
                    "object": {"score": 81.2, "benchmark": "MMLU"},
                    "raw_excerpt": "Recent extraction reports 81.2% — supporting claim is stale.",
                    "confidence": 0.8,
                }
            ],
        },
    }

    _run(db, responses)

    conn = get_connection(db)
    # 1 original arxiv + 1 synthetic agent_reasoning
    sources = list_sources(conn, limit=200)
    assert len(sources) == 2
    synthetic = [s for s in sources if s.type == SourceType.agent_reasoning]
    assert len(synthetic) == 1
    assert synthetic[0].url.startswith("agent://skeptic/belief/")
    assert synthetic[0].author == "skeptic"

    # 1 original claim + 1 skeptic counter-claim
    claims = list_claims(conn, limit=200)
    assert len(claims) == 2
    counters = [c for c in claims if c.extracted_by_agent == "skeptic"]
    assert len(counters) == 1
    assert counters[0].source_id == synthetic[0].id

    # Revision recorded, belief confidence reduced
    revisions = list_revisions(conn, belief_id=belief_id)
    assert len(revisions) == 1
    rev = revisions[0]
    assert rev.revised_by_agent == "skeptic"
    assert rev.trigger_claim_ids == [counters[0].id]
    assert rev.previous_confidence == 0.7
    assert rev.new_confidence == pytest.approx(0.5)

    belief = get_belief_by_id(conn, belief_id)
    assert belief is not None
    assert belief.confidence == pytest.approx(0.5)
    # `weakened` does NOT touch contradicting_claim_ids
    assert belief.contradicting_claim_ids == []

    # pipeline_runs row recorded as skeptic_sweep
    runs = list_pipeline_runs(conn, limit=10, run_type="skeptic_sweep")
    assert len(runs) == 1
    assert runs[0].beliefs_revised == 1
    assert runs[0].claims_inserted == 1
    conn.close()


def test_contradicted_assessment_updates_contradicting_claim_ids(tmp_path: Path) -> None:
    db = str(tmp_path / "sweep.db")
    belief_id, entity_id = _seed(db)

    responses = {
        "select_beliefs_to_challenge": {
            "picks": [{"belief_id": belief_id, "score": 2.5, "rationale": "extreme"}]
        },
        "challenge_belief": {
            "verdict": "contradicted",
            "confidence": 0.9,
            "rationale": "Direct contradiction from a more authoritative source.",
            "suggested_confidence_delta": -0.4,
            "counter_claims": [
                {
                    "predicate": "achieves_score",
                    "subject_entity_id": entity_id,
                    "object": {"score": 60.0, "benchmark": "MMLU"},
                    "raw_excerpt": "Authoritative re-evaluation: 60% on MMLU.",
                    "confidence": 0.9,
                }
            ],
        },
    }

    _run(db, responses)

    conn = get_connection(db)
    belief = get_belief_by_id(conn, belief_id)
    assert belief is not None
    counters = [c for c in list_claims(conn, limit=200) if c.extracted_by_agent == "skeptic"]
    assert len(belief.contradicting_claim_ids) == 1
    assert belief.contradicting_claim_ids == [counters[0].id]
    conn.close()


def test_below_threshold_is_no_op(tmp_path: Path) -> None:
    db = str(tmp_path / "sweep.db")
    belief_id, entity_id = _seed(db)

    responses = {
        "select_beliefs_to_challenge": {
            "picks": [{"belief_id": belief_id, "score": 2.5, "rationale": "stale"}]
        },
        "challenge_belief": {
            # Low self-confidence — coordinator should ignore even with counter_claims present
            "verdict": "weakened",
            "confidence": 0.3,
            "rationale": "Unsure.",
            "suggested_confidence_delta": -0.1,
            "counter_claims": [
                {
                    "predicate": "achieves_score",
                    "subject_entity_id": entity_id,
                    "object": {"score": 80.0, "benchmark": "MMLU"},
                    "raw_excerpt": "Tentative.",
                    "confidence": 0.4,
                }
            ],
        },
    }

    _run(db, responses)

    conn = get_connection(db)
    assert list_revisions(conn, belief_id=belief_id) == []
    skeptic_claims = [
        c for c in list_claims(conn, limit=200) if c.extracted_by_agent == "skeptic"
    ]
    assert skeptic_claims == []
    belief = get_belief_by_id(conn, belief_id)
    assert belief is not None
    assert belief.confidence == 0.7  # unchanged
    conn.close()


def test_supported_verdict_is_no_op(tmp_path: Path) -> None:
    db = str(tmp_path / "sweep.db")
    belief_id, _ = _seed(db)

    responses = {
        "select_beliefs_to_challenge": {
            "picks": [{"belief_id": belief_id, "score": 2.5, "rationale": "stale"}]
        },
        "challenge_belief": {
            "verdict": "supported",
            "confidence": 0.95,
            "rationale": "Belief holds up under scrutiny.",
            "suggested_confidence_delta": 0.0,
            "counter_claims": [],
        },
    }
    _run(db, responses)

    conn = get_connection(db)
    assert list_revisions(conn, belief_id=belief_id) == []
    conn.close()


def test_curator_payload_includes_last_challenged_at(tmp_path: Path) -> None:
    """Re-running after a successful sweep should pass last_challenged_at to Curator."""
    db = str(tmp_path / "sweep.db")
    belief_id, entity_id = _seed(db)

    responses = {
        "select_beliefs_to_challenge": {
            "picks": [{"belief_id": belief_id, "score": 2.5, "rationale": "stale"}]
        },
        "challenge_belief": {
            "verdict": "weakened",
            "confidence": 0.85,
            "rationale": "stale supporter",
            "suggested_confidence_delta": -0.15,
            "counter_claims": [
                {
                    "predicate": "achieves_score",
                    "subject_entity_id": entity_id,
                    "object": {"score": 81.0, "benchmark": "MMLU"},
                    "raw_excerpt": "stale",
                    "confidence": 0.8,
                }
            ],
        },
    }
    _run(db, responses)

    # Second run — Curator payload should now carry a non-None last_challenged_at
    fake = _run(db, responses)
    curator_call = next(c for c in fake.calls if c[0] == "select_beliefs_to_challenge")
    payload_beliefs = curator_call[1]["beliefs"]
    assert len(payload_beliefs) == 1
    assert payload_beliefs[0]["last_challenged_at"] is not None


def test_no_held_beliefs_records_empty_run(tmp_path: Path) -> None:
    db = str(tmp_path / "sweep.db")
    conn = get_connection(db)
    apply_migrations(conn)
    conn.close()

    _run(db, {})  # discovery never called because we short-circuit

    conn = get_connection(db)
    runs = list_pipeline_runs(conn, limit=10, run_type="skeptic_sweep")
    assert len(runs) == 1
    assert runs[0].beliefs_revised == 0
    conn.close()


def test_contradicted_verdict_triggers_curator_second_pass(tmp_path: Path) -> None:
    """Conditional edge: a contradicted verdict routes evaluate → trigger_curator,
    which re-dispatches the Curator (a second select_beliefs_to_challenge call)."""
    db = str(tmp_path / "sweep.db")
    belief_id, entity_id = _seed(db)

    responses = {
        "select_beliefs_to_challenge": {
            "picks": [{"belief_id": belief_id, "score": 2.5, "rationale": "x"}]
        },
        "challenge_belief": {
            "verdict": "contradicted",
            "confidence": 0.9,
            "rationale": "Direct contradiction.",
            "suggested_confidence_delta": -0.4,
            "counter_claims": [
                {
                    "predicate": "achieves_score",
                    "subject_entity_id": entity_id,
                    "object": {"score": 60.0, "benchmark": "MMLU"},
                    "raw_excerpt": "60% on MMLU.",
                    "confidence": 0.9,
                }
            ],
        },
    }
    fake = _run(db, responses)
    curator_calls = [c for c in fake.calls if c[0] == "select_beliefs_to_challenge"]
    assert len(curator_calls) == 2  # load_beliefs + trigger_curator


def test_weakened_verdict_does_not_trigger_curator(tmp_path: Path) -> None:
    """No contradicted verdict → evaluate routes straight to finalize, so the
    Curator is dispatched only once (in load_beliefs)."""
    db = str(tmp_path / "sweep.db")
    belief_id, entity_id = _seed(db)

    responses = {
        "select_beliefs_to_challenge": {
            "picks": [{"belief_id": belief_id, "score": 2.5, "rationale": "x"}]
        },
        "challenge_belief": {
            "verdict": "weakened",
            "confidence": 0.85,
            "rationale": "Stale supporter.",
            "suggested_confidence_delta": -0.2,
            "counter_claims": [
                {
                    "predicate": "achieves_score",
                    "subject_entity_id": entity_id,
                    "object": {"score": 81.0, "benchmark": "MMLU"},
                    "raw_excerpt": "81% on MMLU.",
                    "confidence": 0.8,
                }
            ],
        },
    }
    fake = _run(db, responses)
    curator_calls = [c for c in fake.calls if c[0] == "select_beliefs_to_challenge"]
    assert len(curator_calls) == 1


# Local pytest import for approx
import pytest  # noqa: E402
