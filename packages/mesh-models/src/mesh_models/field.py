"""Field model + stored FieldProfile (Phase 17).

A **Field** is a first-class scope that partitions all field-state in the
system — entities, sources, claims, beliefs, relationships, investigations,
heuristics, runs, de-dup, schedules, and connector config. ``field_id`` is a
*partition*, never a content axis: synthesis / confidence / curator logic never
reads it to branch behavior; it only scopes which rows a read or write touches.

The **FieldProfile** is the serialized, prompt-driving description of a field —
the grounding sentence(s), entity-naming hints, few-shot examples, and topic
wording that the extractor / skeptic / personalizer prompt builders template in
(Phase 17b). The predicate vocabulary and object schemas stay universal; only
framing and examples are field-supplied.

The seeded ``ai-robotics`` field reproduces today's AI/robotics behavior; its
profile carries the current grounding + few-shot verbatim so the built prompt is
byte-identical to the prior hardcoded string.
"""
from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel
from pydantic import Field as PydanticField

# The seeded default field. With no ``--field`` specified, the pipeline targets
# this field and behaves exactly as before Phase 17.
DEFAULT_FIELD_ID = "ai-robotics"
DEFAULT_FIELD_SLUG = "ai-robotics"


class FieldProfile(BaseModel):
    """Prompt-driving description of a field. Serialized to ``fields.profile``."""

    slug: str
    name: str
    # Grounding sentence(s): the domain framing that replaces the hardcoded
    # "AI/robotics research knowledge base" line in the system prompts.
    description: str
    # Entity-naming guidance surfaced to the extractor (e.g. how labs / models /
    # benchmarks are named in this field).
    entity_type_hints: list[str] = PydanticField(default_factory=list)
    # Few-shot examples block, inserted verbatim into the extraction prompt.
    extraction_examples: str = ""
    # The word used for the "state of the art" / topic framing (e.g. "sota").
    topic_label: str = "sota"


class Field(BaseModel):
    """A first-class field scope. Mirrors a ``knowledge.fields`` row."""

    id: str
    name: str
    slug: str
    profile: FieldProfile
    created_at: datetime = PydanticField(default_factory=lambda: datetime.now(UTC))
    is_active: bool = True


# Canonical profile for the seeded AI & Robotics field. The full byte-exact
# grounding + few-shot text is finalized in Phase 17b (the prompt-builder block);
# ``init_pg`` upserts this onto the migration-seeded row so the source of truth is
# Python, not the SQL literal.
AI_ROBOTICS_PROFILE = FieldProfile(
    slug=DEFAULT_FIELD_SLUG,
    name="AI & Robotics",
    description=(
        "an AI/robotics research knowledge base, tracking models, benchmarks, "
        "labs, methods, datasets, and the systems built on them"
    ),
    entity_type_hints=[
        "models (e.g. GPT-4, Gemini, GR00T N1)",
        "benchmarks / datasets (e.g. MMLU, SWE-bench)",
        "labs / organizations (e.g. OpenAI, DeepMind)",
        "methods / architectures (e.g. RLHF, transformer)",
    ],
    extraction_examples="",
    topic_label="sota",
)
