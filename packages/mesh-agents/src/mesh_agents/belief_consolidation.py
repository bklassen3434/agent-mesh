"""Semantic belief consolidation — match + merge adjudication (Phase 19c).

The world-model analog of ``entity_resolution``. Blocking (pgvector
nearest-neighbour over currently-held, same-field beliefs) and merge
(transactional, append-only fold) live in ``mesh_db.beliefs``. This module is
the **match** layer in between: turn a similarity score into a band, and
adjudicate the uncertain middle band with the LLM.

Design posture — **conservative**, even more so than entity resolution: a false
belief merge corrupts the knowledge base and is painful to unwind, while a missed
merge is cheap (caught next sweep). So the bands start *tighter* than entity
resolution's (0.95 / 0.85 vs 0.93 / 0.80), and the LLM defaults to "not the same
proposition" whenever it is unsure or its response fails to parse.

The resolver is **write-free**: it returns decisions, and the sweep job applies
them under the coordinator-writer role (the role model is preserved).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal

from mesh_db.beliefs import (
    ConfidenceFn,
    belief_family,
    find_candidate_duplicate_beliefs,
    get_belief_signals,
)
from mesh_db.connection import MeshConnection
from mesh_llm.batch import BatchRequestItem
from mesh_llm.client import LLMResponseError
from mesh_llm.embeddings import Embedder, belief_embed_text
from mesh_llm.protocol import LLMClient
from mesh_models.belief import Belief
from mesh_models.field import DEFAULT_FIELD_ID
from pydantic import BaseModel, Field

from mesh_agents.confidence import (
    BeliefSignals,
    ConfidenceWeights,
    compute_confidence,
)

MatchDecision = Literal["merge", "reject", "adjudicate"]

# Default thresholds (cosine similarity). Start TIGHTER than entity resolution:
# a false belief merge is costlier than a false entity merge, so only very
# confident pairs auto-merge and the reject floor is higher.
_DEFAULT_HIGH = 0.95
_DEFAULT_LOW = 0.85


@dataclass(frozen=True)
class BeliefMergeConfig:
    """Match thresholds (cosine similarity), tunable via env. Conservative
    defaults — raise ``high`` if a sweep surfaces a false merge."""

    high: float = _DEFAULT_HIGH
    low: float = _DEFAULT_LOW

    @classmethod
    def from_env(cls) -> BeliefMergeConfig:
        def _f(name: str, default: float) -> float:
            raw = (os.environ.get(name) or "").strip()
            try:
                return float(raw) if raw else default
            except ValueError:
                return default

        return cls(
            high=_f("MESH_BELIEF_MERGE_HIGH", _DEFAULT_HIGH),
            low=_f("MESH_BELIEF_MERGE_LOW", _DEFAULT_LOW),
        )


def band(similarity: float, config: BeliefMergeConfig | None = None) -> MatchDecision:
    """Map a cosine similarity to a band. ``>= high`` → auto-merge; ``<= low`` →
    auto-reject; otherwise → LLM adjudication. Mirrors the entity ``classify_pair``."""
    cfg = config or BeliefMergeConfig()
    if similarity >= cfg.high:
        return "merge"
    if similarity <= cfg.low:
        return "reject"
    return "adjudicate"


class BeliefMatchDecision(BaseModel):
    """LLM adjudication output. ``same_proposition`` defaults False — the
    conservative fallback used on any parse failure."""

    same_proposition: bool = Field(
        default=False,
        description=(
            "True ONLY if the two beliefs assert the SAME proposition about the "
            "field (one is a re-phrasing / near-duplicate of the other)."
        ),
    )
    reason: str = Field(default="", description="One-sentence justification.")


@dataclass(frozen=True)
class BeliefForMatch:
    """Minimal belief view handed to the adjudicator."""

    topic: str
    statement: str


def belief_for_match(belief: Belief) -> BeliefForMatch:
    return BeliefForMatch(topic=belief.topic, statement=belief.statement)


ADJUDICATION_SYSTEM = (
    "You decide whether two belief records from a research knowledge base assert "
    "the SAME proposition about the field. Re-phrasings, paraphrases, and "
    "near-duplicate statements of the same underlying claim count as the SAME "
    "proposition. Beliefs that merely share a topic, an entity, or a theme but "
    "make DIFFERENT assertions (different values, different subjects, different "
    "directions) are DIFFERENT propositions. "
    "Be conservative: if the evidence is ambiguous or insufficient, answer "
    "same_proposition=false. Merging two different beliefs corrupts the knowledge "
    "base and is costly to unwind; leaving a duplicate is cheap."
)


def _belief_block(label: str, b: BeliefForMatch) -> str:
    return f"{label}:\n  topic: {b.topic}\n  statement: {b.statement}"


def build_belief_adjudication_prompt(
    a: BeliefForMatch, b: BeliefForMatch
) -> tuple[str, str]:
    """(system, user) prompt for one same-proposition adjudication. Shared by the
    synchronous path and the Batch-API path so both reason identically."""
    user = (
        "Do these two beliefs assert the same proposition about the field?\n\n"
        f"{_belief_block('Belief A', a)}\n\n"
        f"{_belief_block('Belief B', b)}"
    )
    return ADJUDICATION_SYSTEM, user


def adjudicate_beliefs(llm: LLMClient, a: BeliefForMatch, b: BeliefForMatch) -> bool:
    """Single synchronous adjudication. Returns ``False`` (not-same) on any parse
    failure (conservative). Provider-not-ready errors propagate."""
    system, user = build_belief_adjudication_prompt(a, b)
    try:
        result, _ = llm.complete_with_latency(
            name="adjudicate_belief_match",
            system=system,
            user=user,
            response_model=BeliefMatchDecision,
        )
    except LLMResponseError:
        return False
    assert isinstance(result, BeliefMatchDecision)
    return result.same_proposition


def build_belief_adjudication_batch_items(
    pairs: list[tuple[str, BeliefForMatch, BeliefForMatch]],
) -> list[BatchRequestItem]:
    """Build Batch-API requests for ``(custom_id, a, b)`` pairs. The sweep submits
    these as one batch (50% cheaper)."""
    items: list[BatchRequestItem] = []
    for custom_id, a, b in pairs:
        system, user = build_belief_adjudication_prompt(a, b)
        items.append(BatchRequestItem(custom_id=custom_id, system=system, user=user))
    return items


def make_confidence_fn(weights: ConfidenceWeights | None = None) -> ConfidenceFn:
    """Build the ``ConfidenceFn`` ``merge_beliefs`` takes — wiring
    ``get_belief_signals`` (mesh_db) → ``compute_confidence`` (mesh_agents). Kept
    here so ``mesh_db`` stays free of the confidence dependency (one-way flow)."""
    w = weights or ConfidenceWeights.from_env()

    def _fn(conn: MeshConnection, belief_id: str) -> float:
        signals = BeliefSignals.from_row(get_belief_signals(conn, belief_id))
        return compute_confidence(signals, w)

    return _fn


# ---------------------------------------------------------------------------
# Match layer — block → band → (adjudicate middle) → decisions.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MergeDecision:
    """One block→band outcome for a (query belief, candidate) pair. ``band`` is
    the raw similarity classification; ``confirmed`` is the final verdict after
    adjudication (high → True, low → excluded, middle → LLM result). The sweep
    applies pairs whose ``confirmed`` is True; the CLI duplicates view shows all."""

    belief_id: str
    candidate_id: str
    candidate_topic: str
    candidate_statement: str
    similarity: float
    band: MatchDecision
    confirmed: bool


def resolve_belief_duplicates(
    conn: MeshConnection,
    belief: Belief,
    *,
    embedder: Embedder,
    llm: LLMClient | None,
    config: BeliefMergeConfig | None = None,
    k: int = 10,
    field_id: str = DEFAULT_FIELD_ID,
) -> list[MergeDecision]:
    """Resolve one belief's duplicate candidates. Write-free.

    Embeds ``(topic, statement)``, blocks against currently-held same-field,
    same-family beliefs, bands each candidate, and adjudicates the middle band
    with the LLM (defaulting to not-same when ``llm`` is None or it can't decide).
    Returns the non-reject candidates (band ∈ {merge, adjudicate}); the caller
    keeps the ``confirmed`` ones. The blocking, like the merge it feeds, never
    crosses fields (Phase 17)."""
    cfg = config or BeliefMergeConfig.from_env()
    vec = embedder.embed([belief_embed_text(belief.topic, belief.statement)])[0]
    family = belief_family(belief.topic)
    candidates = find_candidate_duplicate_beliefs(
        conn, vec, k=k, exclude_id=belief.id, field_id=field_id, family=family
    )
    decisions: list[MergeDecision] = []
    for cand_id, cand_topic, cand_statement, distance in candidates:
        similarity = 1.0 - distance
        decision = band(similarity, cfg)
        if decision == "reject":
            continue
        confirmed = decision == "merge"
        if decision == "adjudicate" and llm is not None:
            confirmed = adjudicate_beliefs(
                llm,
                belief_for_match(belief),
                BeliefForMatch(topic=cand_topic, statement=cand_statement),
            )
        decisions.append(
            MergeDecision(
                belief_id=belief.id,
                candidate_id=cand_id,
                candidate_topic=cand_topic,
                candidate_statement=cand_statement,
                similarity=similarity,
                band=decision,
                confirmed=confirmed,
            )
        )
    return decisions


# Re-exported so callers can build a write closure without importing internals.
__all__ = [
    "ADJUDICATION_SYSTEM",
    "BeliefForMatch",
    "BeliefMatchDecision",
    "BeliefMergeConfig",
    "MatchDecision",
    "MergeDecision",
    "adjudicate_beliefs",
    "band",
    "belief_for_match",
    "build_belief_adjudication_batch_items",
    "build_belief_adjudication_prompt",
    "make_confidence_fn",
    "resolve_belief_duplicates",
]
