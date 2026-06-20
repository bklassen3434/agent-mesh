"""``consolidate-memory`` skill — episodic distillation as controller effects.

The controller analog of the consolidation sweep. These tests pin that the skill
distils seeded episodic history into a ``WriteHeuristicEffect`` (which the gateway
persists as an active heuristic with provenance), and that a second pass dedupes
against the existing heuristic rather than flooding the store.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from mesh_agents.consolidator import CandidateHeuristic, ConsolidationResult
from mesh_agents.skills.consolidate_memory import ConsolidateMemorySkill
from mesh_db.claims import create_claim
from mesh_db.connection import MeshConnection
from mesh_db.effects import apply_effects
from mesh_db.entities import create_entity
from mesh_db.heuristics import list_heuristics
from mesh_db.sources import create_source
from mesh_llm import LLMUsage
from mesh_models.claim import Claim
from mesh_models.effect import WriteHeuristicEffect
from mesh_models.entity import Entity, EntityType
from mesh_models.field import DEFAULT_FIELD_ID
from mesh_models.source import Source, SourceType
from mesh_models.tension import Tension, TensionKind


class _MockLLM:
    """Returns one canned candidate heuristic via the synchronous distil path."""

    model = "mock-model"

    def complete_with_usage(
        self, name: str, system: str, user: str,
        response_model: type | None = None, options: object | None = None,
    ) -> tuple[object, int, LLMUsage]:
        result = ConsolidationResult(
            heuristics=[
                CandidateHeuristic(
                    skill="extract_claims",
                    source="reddit",
                    heuristic="Treat single-source forum score claims as low-confidence.",
                    rationale="forum extractions recur without corroboration",
                )
            ]
        )
        return result, 100, LLMUsage(input_tokens=200, output_tokens=40)


def _seed_extraction_history(conn: MeshConnection) -> None:
    ent = create_entity(conn, Entity(canonical_name="ForumModel", type=EntityType.model))
    for i in range(3):
        src = create_source(
            conn,
            Source(
                type=SourceType.reddit, url=f"https://reddit.com/r/ml/{i}",
                published_at=datetime.now(UTC), raw_content_hash=f"hash-{i}",
            ),
        )
        create_claim(
            conn,
            Claim(
                predicate="achieves_score", subject_entity_id=ent.id,
                object={"score": 90.0 + i, "benchmark": "MMLU"}, source_id=src.id,
                extracted_by_agent="claim_extractor", raw_excerpt=f"forum claim {i}",
                confidence=0.7,
            ),
        )


def _tension() -> Tension:
    return Tension(
        id=f"consolidatable_memory:{DEFAULT_FIELD_ID}",
        field_id=DEFAULT_FIELD_ID,
        kind=TensionKind.consolidatable_memory,
        subject="memory consolidation",
        rationale="distil",
        value=0.2,
        est_cost_usd=0.04,
        handler_skill="consolidate-memory",
        target_ref={"field_id": DEFAULT_FIELD_ID},
    )


def test_consolidate_memory_distils_and_persists(tmp_db: MeshConnection) -> None:
    _seed_extraction_history(tmp_db)
    skill = ConsolidateMemorySkill(llm=_MockLLM())  # type: ignore[arg-type]

    effects = asyncio.run(skill.run(tmp_db, _tension(), budget_usd=0.0))
    assert effects, "expected at least one WriteHeuristicEffect"
    assert all(isinstance(e, WriteHeuristicEffect) for e in effects)
    # Distilled heuristics carry provenance back to the claims they came from.
    assert all(e.heuristic.provenance_claim_ids for e in effects)

    report = apply_effects(tmp_db, effects)
    assert report.heuristics_written == len(effects)
    assert not report.errors

    persisted = list_heuristics(
        tmp_db, agent="claim_extractor", active=True, field_id=DEFAULT_FIELD_ID
    )
    assert any("forum" in h.heuristic.lower() for h in persisted)


def test_consolidate_memory_dedupes_on_second_pass(tmp_db: MeshConnection) -> None:
    _seed_extraction_history(tmp_db)
    skill = ConsolidateMemorySkill(llm=_MockLLM())  # type: ignore[arg-type]

    first = asyncio.run(skill.run(tmp_db, _tension(), budget_usd=0.0))
    apply_effects(tmp_db, first)
    # Same candidate text already active → the second pass emits nothing.
    second = asyncio.run(skill.run(tmp_db, _tension(), budget_usd=0.0))
    assert second == []


def test_consolidate_memory_noop_without_llm(tmp_db: MeshConnection) -> None:
    _seed_extraction_history(tmp_db)
    # No injected client and no provider configured → degrades to no effects.
    effects = asyncio.run(ConsolidateMemorySkill().run(tmp_db, _tension(), budget_usd=0.0))
    assert effects == []
