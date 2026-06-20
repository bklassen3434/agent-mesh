"""Phase 2 fan-out: the ``investigate-gap`` skill.

A gap-family tension → one ``OpenInvestigationEffect`` (origin ``discovery``),
proving the skill plans an investigation without ever writing or searching. The
LLM path uses a mock ``LLMClient``; the fallback path injects no LLM at all.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest
from mesh_agents.discovery import DiscoveryProposal, DiscoveryProposals
from mesh_agents.skill import (
    Skill,
    clear_registry,
    get_skill,
    load_builtin_skills,
    register_skill,
    skills_for,
)
from mesh_agents.skills.investigate_gap import InvestigateGapSkill
from mesh_db.connection import MeshConnection
from mesh_db.connectors import enable_connector
from mesh_db.effects import apply_effects
from mesh_db.investigations import list_investigations
from mesh_models.effect import OpenInvestigationEffect
from mesh_models.investigation import InvestigationOrigin, InvestigationStatus
from mesh_models.tension import Tension, TensionKind

_FIELD = "ai-robotics"


@pytest.fixture(autouse=True)
def _registry() -> Any:
    # Other test files clear the registry on teardown; re-register for isolation.
    clear_registry()
    register_skill(InvestigateGapSkill)
    yield
    clear_registry()


class _MockLLM:
    """Minimal LLMClient stand-in returning a fixed DiscoveryProposals."""

    model = "mock-model"

    def __init__(self, result: Any) -> None:
        self._result = result

    def health_check(self) -> None:  # pragma: no cover - unused
        return None

    def complete_with_latency(self, *a: Any, **kw: Any) -> Any:  # pragma: no cover
        raise NotImplementedError

    def complete_with_usage(self, *a: Any, **kw: Any) -> Any:
        from mesh_llm.usage import LLMUsage

        return self._result, 10, LLMUsage(input_tokens=1, output_tokens=1)


def _tension(
    kind: TensionKind = TensionKind.under_evidenced_entity,
    *,
    entity_id: str | None = "e1",
    belief_id: str | None = None,
    value: float = 0.55,
) -> Tension:
    target_ref: dict[str, str] = {}
    if entity_id:
        target_ref["entity_id"] = entity_id
    if belief_id:
        target_ref["belief_id"] = belief_id
    return Tension(
        id=f"{kind.value}:{entity_id or belief_id}",
        field_id=_FIELD,
        kind=kind,
        subject="ObscureNet",
        rationale="Entity 'ObscureNet' has only 1 claim — under-evidenced.",
        value=value,
        est_cost_usd=0.05,
        handler_skill="investigate-gap",
        target_ref=target_ref,
        signals={"claim_count": 1},
    )


# ── registration / contract ──────────────────────────────────────────────────


def test_registered_and_handles_gap_kinds() -> None:
    skill = get_skill("investigate-gap")
    assert skill is not None
    assert isinstance(skill, Skill)  # satisfies the runtime-checkable Protocol
    for kind in (
        TensionKind.under_evidenced_entity,
        TensionKind.thin_belief,
        TensionKind.rising_topic,
        TensionKind.missing_reciprocal_edge,
    ):
        assert skill in skills_for(kind)
    # Not a gap kind it claims.
    assert get_skill("investigate-gap") not in skills_for(TensionKind.stale_belief)
    assert any(s.skill_id == "investigate-gap" for s in load_builtin_skills())


# ── run → one OpenInvestigationEffect, never writes ──────────────────────────


def test_run_opens_investigation_from_llm(tmp_db: MeshConnection) -> None:
    enable_connector(tmp_db, _FIELD, "arxiv", config={"categories": ["cs.LG"]})
    tension = _tension()
    result = DiscoveryProposals(
        proposals=[
            DiscoveryProposal(
                gap_id=tension.id,
                hypothesis="Search arxiv for ObscureNet benchmark results",
                suggested_source_types=["arxiv"],
                rationale="closes the under-evidenced gap",
            )
        ]
    )
    skill = InvestigateGapSkill(llm_factory=lambda: _MockLLM(result))

    effects = asyncio.run(skill.run(tmp_db, tension, budget_usd=0.05))

    assert len(effects) == 1
    effect = effects[0]
    assert isinstance(effect, OpenInvestigationEffect)
    assert effect.field_id == _FIELD
    inv = effect.investigation
    assert inv.origin == InvestigationOrigin.discovery
    assert inv.status == InvestigationStatus.open
    assert inv.hypothesis == "Search arxiv for ObscureNet benchmark results"
    assert inv.target_entity_id == "e1"
    assert inv.suggested_source_types == ["arxiv"]
    assert inv.trigger_rationale and "under-evidenced" in inv.trigger_rationale

    # The skill itself wrote nothing — the investigation only exists once the
    # gateway applies the effect.
    assert list_investigations(tmp_db, field_id=_FIELD) == []
    report = apply_effects(tmp_db, effects)
    assert report.investigations_opened == 1
    assert len(list_investigations(tmp_db, field_id=_FIELD)) == 1


def test_run_falls_back_without_llm(tmp_db: MeshConnection) -> None:
    """No LLM available → still exactly one investigation, hypothesis = gap rationale."""
    tension = _tension()
    skill = InvestigateGapSkill(llm_factory=lambda: None)

    effects = asyncio.run(skill.run(tmp_db, tension, budget_usd=0.05))

    assert len(effects) == 1
    inv = effects[0].investigation
    assert inv.origin == InvestigationOrigin.discovery
    assert inv.hypothesis == tension.rationale
    assert inv.target_entity_id == "e1"
    assert list_investigations(tmp_db, field_id=_FIELD) == []


def test_run_handles_belief_anchored_gap(tmp_db: MeshConnection) -> None:
    """A thin-belief tension carries a belief id, not an entity id."""
    tension = _tension(TensionKind.thin_belief, entity_id=None, belief_id="b1")
    skill = InvestigateGapSkill(llm_factory=lambda: None)

    effects = asyncio.run(skill.run(tmp_db, tension, budget_usd=0.05))

    assert len(effects) == 1
    inv = effects[0].investigation
    assert inv.opened_by_belief_id == "b1"
    assert inv.target_entity_id is None
