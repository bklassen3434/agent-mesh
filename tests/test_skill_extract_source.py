"""Phase 2b: the ``extract-source`` skill.

Drives the skill end to end against a real (tmp) store with a mocked LLM: it
loads an unread source, extracts canned claims, resolves their subject to an
existing entity, and returns ``CreateClaimEffect``s — writing nothing itself. The
gateway then applies the effects, proving the decision/write split holds.
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from typing import Any

from mesh_agents.skill import clear_registry, load_builtin_skills
from mesh_agents.skills.extract_source import ExtractSourceSkill
from mesh_db.claims import count_claims
from mesh_db.connection import MeshConnection
from mesh_db.effects import apply_effects
from mesh_db.entities import create_entity
from mesh_db.sources import create_source
from mesh_models.effect import CreateClaimEffect
from mesh_models.entity import Entity, EntityType
from mesh_models.field import DEFAULT_FIELD_ID
from mesh_models.source import Source, SourceType
from mesh_models.tension import Tension, TensionKind

_FIXTURE = Path(__file__).parent / "fixtures" / "llm_responses" / "claim_extraction_result.json"


class MockLLMClient:
    """Returns the canned ClaimExtractionResult fixture (mirrors test_claim_extractor)."""

    model = "mock-model"

    def __init__(self, response_json: str | None = None) -> None:
        self._response_json = response_json or _FIXTURE.read_text()

    def complete_with_usage(
        self,
        name: str,
        system: str,
        user: str,
        response_model: type | None = None,
        options: object | None = None,
    ) -> tuple[object, int, object]:
        from mesh_llm import LLMUsage
        from mesh_llm.client import LLMResponseError

        usage = LLMUsage(input_tokens=120, output_tokens=60)
        if response_model is not None:
            try:
                parsed = response_model.model_validate_json(self._response_json)  # type: ignore[attr-defined]
            except Exception as exc:
                raise LLMResponseError(f"mock parse failure: {exc}") from exc
            return parsed, 500, usage
        return self._response_json, 500, usage


def _seed_source(conn: MeshConnection) -> Source:
    source = Source(
        type=SourceType.arxiv,
        url="https://arxiv.org/abs/2401.00001",
        published_at=datetime(2024, 1, 15),
        raw_content_hash="abc123",
    )
    create_source(conn, source, field_id=DEFAULT_FIELD_ID)
    return source


def _seed_entity(conn: MeshConnection, name: str) -> Entity:
    entity = Entity(canonical_name=name, type=EntityType.model)
    create_entity(conn, entity, field_id=DEFAULT_FIELD_ID)
    return entity


def _tension(source_id: str) -> Tension:
    return Tension(
        id=f"{TensionKind.unextracted_source.value}:{source_id}",
        field_id=DEFAULT_FIELD_ID,
        kind=TensionKind.unextracted_source,
        subject="https://arxiv.org/abs/2401.00001",
        rationale="unread",
        value=0.5,
        est_cost_usd=0.008,
        handler_skill="extract-source",
        target_ref={"source_id": source_id},
    )


def _run(skill: ExtractSourceSkill, conn: Any, tension: Tension) -> list[Any]:
    return asyncio.run(skill.run(conn, tension, budget_usd=0.008))


def test_bid_uses_tension_value_and_fixed_cost(tmp_db: MeshConnection) -> None:
    skill = ExtractSourceSkill(llm=MockLLMClient())  # type: ignore[arg-type]
    tension = _tension("nope")
    bid = skill.bid(tmp_db, tension)
    assert bid is not None
    assert bid.value == tension.value
    assert bid.est_cost_usd == 0.008


def test_emits_one_create_claim_effect_per_known_subject(tmp_db: MeshConnection) -> None:
    source = _seed_source(tmp_db)
    _seed_entity(tmp_db, "TestModel-7B")  # all four canned claims share this subject

    skill = ExtractSourceSkill(llm=MockLLMClient())  # type: ignore[arg-type]
    effects = _run(skill, tmp_db, _tension(source.id))

    assert len(effects) == 4
    assert all(isinstance(e, CreateClaimEffect) for e in effects)
    assert {e.claim.predicate for e in effects} == {
        "achieves_score",
        "outperforms",
        "developed_by",
        "evaluated_on",
    }
    # Every effect points at the seeded entity + source, and the field is threaded.
    assert all(e.field_id == DEFAULT_FIELD_ID for e in effects)
    assert all(e.claim.source_id == source.id for e in effects)

    # The skill itself wrote nothing — claims only land once the gateway applies.
    assert count_claims(tmp_db, field_id=DEFAULT_FIELD_ID) == 0
    report = apply_effects(tmp_db, effects)
    assert report.claims_created == 4
    assert not report.errors
    assert count_claims(tmp_db, field_id=DEFAULT_FIELD_ID) == 4


def test_skips_claims_whose_subject_is_unknown(tmp_db: MeshConnection) -> None:
    # Source exists but the subject entity does not → nothing resolvable, no effects.
    source = _seed_source(tmp_db)
    skill = ExtractSourceSkill(llm=MockLLMClient())  # type: ignore[arg-type]
    effects = _run(skill, tmp_db, _tension(source.id))
    assert effects == []


def test_missing_source_returns_empty(tmp_db: MeshConnection) -> None:
    skill = ExtractSourceSkill(llm=MockLLMClient())  # type: ignore[arg-type]
    effects = _run(skill, tmp_db, _tension("does-not-exist"))
    assert effects == []


def test_no_claims_extracted_returns_empty(tmp_db: MeshConnection) -> None:
    source = _seed_source(tmp_db)
    _seed_entity(tmp_db, "TestModel-7B")
    skill = ExtractSourceSkill(llm=MockLLMClient(response_json='{"claims": []}'))  # type: ignore[arg-type]
    effects = _run(skill, tmp_db, _tension(source.id))
    assert effects == []


def test_registered_in_builtin_registry() -> None:
    # The registry is global module state; another test may have cleared it, and
    # re-importing an already-loaded module won't re-run @register_skill. Reload
    # the module on a clean registry to exercise the decorator deterministically.
    import importlib

    from mesh_agents.skills import extract_source as extract_source_mod

    clear_registry()
    try:
        importlib.reload(extract_source_mod)
        skills = load_builtin_skills()
        ids = {s.skill_id for s in skills}
        assert "extract-source" in ids
        matched = [s for s in skills if TensionKind.unextracted_source in s.handles]
        assert any(s.skill_id == "extract-source" for s in matched)
    finally:
        clear_registry()
        importlib.reload(extract_source_mod)
