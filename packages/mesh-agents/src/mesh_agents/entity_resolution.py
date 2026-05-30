"""Semantic entity resolution — match + merge adjudication (Phase 13b).

Blocking (pgvector nearest-neighbour) lives in ``mesh_db.entities``; merge
(transactional re-point + alias fold + delete) lives there too. This module is
the **match** layer in between: turn a similarity score into a decision, and
adjudicate the uncertain middle band with the LLM.

Design posture — **conservative**: a false merge corrupts provenance and is
painful to unwind, while a missed merge is cheap (caught next pass). So the
bands favour leaving duplicates, and the LLM defaults to "not the same" whenever
it is unsure or its response fails to parse.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Literal

from mesh_llm.batch import BatchRequestItem
from mesh_llm.client import LLMResponseError
from mesh_llm.protocol import LLMClient
from pydantic import BaseModel, Field

MatchDecision = Literal["merge", "reject", "adjudicate"]

# Default thresholds. Empirically (bge-small): near-duplicate name variants score
# ~0.88-0.93 cosine similarity, unrelated entities ~0.5. ``high`` sits at the top
# of that band so only very confident pairs auto-merge; everything plausible but
# uncertain is routed to the LLM; clearly-unrelated pairs auto-reject.
_DEFAULT_HIGH = 0.93
_DEFAULT_LOW = 0.80


@dataclass(frozen=True)
class ResolutionConfig:
    """Match thresholds (cosine similarity), tunable via env. Conservative
    defaults — raise ``high`` if reconciliation surfaces a false merge."""

    high: float = _DEFAULT_HIGH
    low: float = _DEFAULT_LOW

    @classmethod
    def from_env(cls) -> ResolutionConfig:
        def _f(name: str, default: float) -> float:
            raw = (os.environ.get(name) or "").strip()
            try:
                return float(raw) if raw else default
            except ValueError:
                return default

        return cls(
            high=_f("MESH_ENTITY_MERGE_HIGH", _DEFAULT_HIGH),
            low=_f("MESH_ENTITY_MERGE_LOW", _DEFAULT_LOW),
        )


def classify_pair(similarity: float, config: ResolutionConfig | None = None) -> MatchDecision:
    """Map a cosine similarity to a band. ``>= high`` → auto-merge; ``<= low`` →
    auto-reject; otherwise → LLM adjudication."""
    cfg = config or ResolutionConfig()
    if similarity >= cfg.high:
        return "merge"
    if similarity <= cfg.low:
        return "reject"
    return "adjudicate"


class EntityMatchDecision(BaseModel):
    """LLM adjudication output. ``same_entity`` defaults False — the conservative
    fallback used on any parse failure."""

    same_entity: bool = Field(
        default=False,
        description="True ONLY if the two refer to the same real-world entity.",
    )
    reason: str = Field(default="", description="One-sentence justification.")


@dataclass(frozen=True)
class EntityForMatch:
    """Minimal entity view handed to the adjudicator."""

    canonical_name: str
    entity_type: str
    aliases: tuple[str, ...] = ()
    sample_claims: tuple[str, ...] = ()


ADJUDICATION_SYSTEM = (
    "You decide whether two entity records from an AI/ML research knowledge base "
    "refer to the SAME real-world entity (e.g. the same model, benchmark, method, "
    "lab, or person). Name variants, abbreviations, versions of the same named "
    "system, and re-phrasings count as the SAME entity. Distinct systems that "
    "merely share a family name, lineage, or topic are DIFFERENT entities. "
    "Be conservative: if the evidence is ambiguous or insufficient, answer "
    "same_entity=false. Merging two different entities is a costly error; leaving "
    "a duplicate is cheap."
)


def _entity_block(label: str, ent: EntityForMatch) -> str:
    lines = [
        f"{label}:",
        f"  name: {ent.canonical_name}",
        f"  type: {ent.entity_type}",
    ]
    if ent.aliases:
        lines.append(f"  aliases: {', '.join(ent.aliases)}")
    if ent.sample_claims:
        lines.append("  example claims:")
        lines.extend(f"    - {c}" for c in ent.sample_claims)
    return "\n".join(lines)


def build_adjudication_prompt(a: EntityForMatch, b: EntityForMatch) -> tuple[str, str]:
    """(system, user) prompt for one same-or-not adjudication. Shared by the
    synchronous path and the Batch-API path so both reason identically."""
    user = (
        "Are these two records the same real-world entity?\n\n"
        f"{_entity_block('Entity A', a)}\n\n"
        f"{_entity_block('Entity B', b)}"
    )
    return ADJUDICATION_SYSTEM, user


def adjudicate_same_entity(
    llm: LLMClient, a: EntityForMatch, b: EntityForMatch
) -> EntityMatchDecision:
    """Single synchronous adjudication. Returns ``same_entity=False`` on any
    parse failure (conservative). Provider-not-ready errors propagate."""
    system, user = build_adjudication_prompt(a, b)
    try:
        result, _ = llm.complete_with_latency(
            name="adjudicate_entity_match",
            system=system,
            user=user,
            response_model=EntityMatchDecision,
        )
    except LLMResponseError:
        return EntityMatchDecision(same_entity=False, reason="parse failure (conservative)")
    assert isinstance(result, EntityMatchDecision)
    return result


def build_adjudication_batch_items(
    pairs: list[tuple[str, EntityForMatch, EntityForMatch]],
) -> list[BatchRequestItem]:
    """Build Batch-API requests for a list of ``(custom_id, a, b)`` pairs. The
    reconciliation pass (13c) submits these as one batch (50% cheaper)."""
    items: list[BatchRequestItem] = []
    for custom_id, a, b in pairs:
        system, user = build_adjudication_prompt(a, b)
        items.append(BatchRequestItem(custom_id=custom_id, system=system, user=user))
    return items


def entity_for_match_from_claims(
    canonical_name: str,
    entity_type: str,
    aliases: list[str] | None = None,
    claims: list[Any] | None = None,
    max_claims: int = 3,
) -> EntityForMatch:
    """Build an ``EntityForMatch`` from a name/type plus a few representative
    ``Claim`` objects (rendered as ``predicate object``)."""
    samples: list[str] = []
    for c in (claims or [])[:max_claims]:
        obj = getattr(c, "object", "")
        predicate = getattr(c, "predicate", "")
        samples.append(f"{predicate} {obj}".strip())
    return EntityForMatch(
        canonical_name=canonical_name,
        entity_type=entity_type,
        aliases=tuple(aliases or ()),
        sample_claims=tuple(samples),
    )
