"""Phase 17b — profile-driven system prompts.

The three coupled prompts (extractor / skeptic / personalizer) are now built from
a FieldProfile. The ai-robotics profile must rebuild them BYTE-FOR-BYTE (pinning
the cache prefix and proving zero behavior change), and a different field must
yield a reframed prompt that drops the AI/robotics framing.
"""
from __future__ import annotations

from mesh_llm.prompts import (
    _LEGACY_CLAIM_EXTRACTION_SYSTEM,
    _LEGACY_PERSONALIZER_SYSTEM,
    _LEGACY_SKEPTIC_SYSTEM,
    build_claim_extraction_system,
    build_personalizer_system,
    build_skeptic_system,
)
from mesh_models.field import AI_ROBOTICS_PROFILE, FieldProfile

_PHYSICS = FieldProfile(
    slug="physics",
    name="Condensed-Matter Physics",
    description="a condensed-matter physics knowledge base",
    entity_type_hints=["BCS theory", "Hubbard model", "graphene"],
    extraction_examples="=== EXAMPLE 1 ===\nTitle: \"A superconductor\"\n(physics)\n",
    topic_label="frontier",
)


def test_extraction_prompt_byte_identical_for_ai_robotics() -> None:
    assert build_claim_extraction_system(AI_ROBOTICS_PROFILE) == _LEGACY_CLAIM_EXTRACTION_SYSTEM
    # No profile → same default.
    assert build_claim_extraction_system() == _LEGACY_CLAIM_EXTRACTION_SYSTEM


def test_skeptic_prompt_byte_identical_for_ai_robotics() -> None:
    assert build_skeptic_system(AI_ROBOTICS_PROFILE) == _LEGACY_SKEPTIC_SYSTEM
    assert build_skeptic_system() == _LEGACY_SKEPTIC_SYSTEM


def test_personalizer_prompt_byte_identical_for_ai_robotics() -> None:
    assert build_personalizer_system(AI_ROBOTICS_PROFILE) == _LEGACY_PERSONALIZER_SYSTEM
    assert build_personalizer_system() == _LEGACY_PERSONALIZER_SYSTEM


def test_extraction_prompt_reframes_for_other_field() -> None:
    p = build_claim_extraction_system(_PHYSICS)
    assert p.startswith(
        "You are a claim extractor for a condensed-matter physics knowledge base."
    )
    # entity hints drive the rule-4 examples; legacy AI hints are gone.
    assert '"BCS theory", "Hubbard model", "graphene"' in p
    assert "GPT-4" not in p and "RoboAgent" not in p
    # field-supplied few-shot replaces the AI examples.
    assert "(physics)" in p
    assert "LLaMA 2" not in p
    # the universal core (predicate vocabulary) is unchanged.
    assert "achieves_score:" in p and "has_capability:" in p
    assert p.rstrip().endswith(
        "Now extract claims from the following source. Return only valid JSON "
        "matching the schema."
    )


def test_skeptic_and_personalizer_reframe() -> None:
    s = build_skeptic_system(_PHYSICS)
    assert s.startswith("You are a skeptic in a condensed-matter physics knowledge base.")
    assert "AI/robotics" not in s
    # universal verdict taxonomy preserved.
    assert '"supported" | "weakened" | "contradicted" | "inconclusive"' in s

    pz = build_personalizer_system(_PHYSICS)
    assert pz.startswith(
        "You are a personalization filter for a condensed-matter physics "
        "knowledge base."
    )
    assert "Hot from Skeptic" in pz  # universal section taxonomy preserved


def test_cache_prefix_is_per_field_stable() -> None:
    # Deterministic: building twice yields the identical string (a stable cache
    # prefix), and it never embeds per-item content.
    a = build_claim_extraction_system(_PHYSICS)
    b = build_claim_extraction_system(_PHYSICS)
    assert a == b
    assert a != build_claim_extraction_system(AI_ROBOTICS_PROFILE)
