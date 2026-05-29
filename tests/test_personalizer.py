from __future__ import annotations

import asyncio
from datetime import date
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from mesh_agents.personalizer import (
    PersonalizeDigestSkillInput,
    PersonalizerAgent,
    _build_handler,
    _filter_to_candidates,
    _personalize_sync,
)
from mesh_llm import LLMResponseError
from mesh_models.briefing import Briefing, BriefingSection, PersonalizedItem


def _llm_with(briefing: Briefing) -> MagicMock:
    client = MagicMock()
    client.complete_with_latency = MagicMock(return_value=(briefing, 1234))
    return client


def _input(**overrides: Any) -> PersonalizeDigestSkillInput:
    base = {
        "profile_text": "I care about LLM observability and agent eval.",
        "target_date": date(2026, 5, 25),
        "beliefs": [
            {"id": "b1", "topic": "sota:MMLU", "statement": "X scores 88", "confidence": 0.6},
        ],
        "revisions": [
            {
                "id": "r1",
                "belief_id": "b1",
                "belief_topic": "sota:MMLU",
                "previous_statement": "X scores 80",
                "new_statement": "X scores 88",
                "previous_confidence": 0.5,
                "new_confidence": 0.6,
                "revised_by_agent": "sota_tracker",
            },
        ],
        "claims": [
            {
                "id": "c1",
                "predicate": "achieves_score",
                "subject_entity_id": "e1",
                "object": {"benchmark": "MMLU", "score": 88.0},
                "raw_excerpt": "X achieves 88 on MMLU.",
                "confidence": 0.9,
            },
        ],
    }
    base.update(overrides)
    return PersonalizeDigestSkillInput.model_validate(base)


def test_claim_block_includes_subject_name() -> None:
    """Claims must surface the resolved entity name, not just the opaque
    subject_entity_id, so the LLM can judge relevance (regression guard)."""
    expected = Briefing(date=date(2026, 5, 25), sections=[])
    llm = _llm_with(expected)
    claims = [
        {
            "id": "c1",
            "predicate": "achieves_score",
            "subject_entity_id": "e1",
            "subject_name": "GR00T N1",
            "object": {"benchmark": "RoboArena", "score": 78.0},
            "raw_excerpt": "achieves 78% on RoboArena.",
            "confidence": 0.9,
        },
    ]
    _personalize_sync(llm, _input(claims=claims))
    user_prompt = llm.complete_with_latency.call_args.kwargs["user"]
    assert "GR00T N1" in user_prompt


def test_personalize_sync_returns_briefing() -> None:
    expected = Briefing(
        date=date(2026, 5, 25),
        profile_excerpt="LLM observability + agent eval",
        sections=[
            BriefingSection(
                name="New Beliefs",
                items=[
                    PersonalizedItem(
                        item_type="belief",
                        item_id="b1",
                        relevance_score=0.85,
                        rationale="MMLU bumps are observable evals.",
                    )
                ],
            )
        ],
    )
    llm = _llm_with(expected)
    out = _personalize_sync(llm, _input())
    assert out.profile_excerpt == "LLM observability + agent eval"
    assert out.sections[0].items[0].item_id == "b1"


def test_filter_drops_invalid_ids() -> None:
    briefing = Briefing(
        date=date(2026, 5, 25),
        sections=[
            BriefingSection(
                name="Worth Reading",
                items=[
                    PersonalizedItem(
                        item_type="claim", item_id="c1", relevance_score=0.7, rationale="ok"
                    ),
                    PersonalizedItem(
                        item_type="claim",
                        item_id="HALLUCINATED",
                        relevance_score=0.7,
                        rationale="bad",
                    ),
                ],
            )
        ],
    )
    cleaned = _filter_to_candidates(briefing, _input())
    assert [i.item_id for i in cleaned.sections[0].items] == ["c1"]


def test_handler_returns_dict_on_success() -> None:
    expected = Briefing(date=date(2026, 5, 25), sections=[])
    llm = _llm_with(expected)
    handler = _build_handler(llm)
    out = asyncio.run(handler(_input().model_dump(mode="json")))
    assert out["date"] == "2026-05-25"
    assert "sections" in out


def test_handler_returns_empty_briefing_on_llm_parse_failure() -> None:
    llm = MagicMock()
    llm.complete_with_latency = MagicMock(side_effect=LLMResponseError("bad json"))
    handler = _build_handler(llm)
    out = asyncio.run(handler(_input().model_dump(mode="json")))
    assert out["sections"] == []


@patch("mesh_agents.personalizer.build_agent_card")
def test_a2a_card_declares_personalize_digest_skill(mock_card: MagicMock) -> None:
    llm = MagicMock()
    PersonalizerAgent(llm=llm).to_a2a_server(url="http://personalizer:8013")
    kwargs = mock_card.call_args.kwargs
    assert kwargs["skill_id"] == "personalize_digest"
    assert kwargs["name"] == "Personalizer"


def test_to_a2a_server_requires_llm() -> None:
    with pytest.raises(AssertionError):
        PersonalizerAgent().to_a2a_server(url="http://x")
