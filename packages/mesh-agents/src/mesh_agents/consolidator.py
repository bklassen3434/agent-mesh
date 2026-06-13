"""Consolidator agent — distills episodic history into procedural heuristics.

The data contract lives here (Phase 16b); the offline LangGraph job that calls
the LLM to produce these proposals lives in ``apps/pipeline`` (Phase 16c).

Write split (coordinator-owned writes): an agent *proposes* a heuristic via the
``propose_heuristic`` skill; only the coordinator persists it (mesh_writer
role). The consolidation job is itself coordinator-side, so it persists its
proposals directly via ``mesh_pipeline._heuristics.persist_heuristic`` — the
skill types here are the same wire contract a future agent-hosted proposer
would use.
"""
from __future__ import annotations

import logging
from typing import Any

from mesh_db.episodic import EpisodicEntry
from mesh_llm import LLMClient, LLMProviderNotReadyError, LLMResponseError, LLMUsage
from mesh_llm.prompts import CONSOLIDATION_SYSTEM, format_consolidation_user
from mesh_models.heuristic import DEFAULT_CONFIDENCE, DEFAULT_TTL_DAYS
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class HeuristicProposal(BaseModel):
    """A candidate heuristic an agent proposes. Provenance is mandatory — a
    proposal with neither a justifying run nor claim is rejected at persist
    time. ``ttl_days`` is converted to ``expires_at`` by the coordinator at
    persist time, so a proposal never carries an absolute clock the proposer
    can't control."""

    agent: str
    skill: str
    source: str | None = None
    entity_id: str | None = None
    heuristic: str
    confidence: float = Field(default=DEFAULT_CONFIDENCE, ge=0.0, le=1.0)
    provenance_run_ids: list[str] = Field(default_factory=list)
    provenance_claim_ids: list[str] = Field(default_factory=list)
    rationale: str = ""
    ttl_days: int = Field(default=DEFAULT_TTL_DAYS, ge=1)

    def has_provenance(self) -> bool:
        return bool(self.provenance_run_ids or self.provenance_claim_ids)


# ── propose_heuristic A2A skill types ────────────────────────────────────────
# Wire contract for the (coordinator-persisted) propose path. No live A2A
# server is stood up this phase — the consolidation job persists directly — but
# the types pin the contract so an agent-hosted proposer drops in later.


class ProposeHeuristicSkillInput(BaseModel):
    proposals: list[dict[str, Any]] = Field(default_factory=list)


class ProposeHeuristicSkillOutput(BaseModel):
    persisted: int = 0
    rejected: int = 0


def validate_proposals(payload: dict[str, Any]) -> list[HeuristicProposal]:
    """Parse + validate a propose_heuristic payload into typed proposals,
    dropping any that lack provenance (mandatory)."""
    skill_input = ProposeHeuristicSkillInput.model_validate(payload)
    out: list[HeuristicProposal] = []
    for raw in skill_input.proposals:
        proposal = HeuristicProposal.model_validate(raw)
        if proposal.has_provenance():
            out.append(proposal)
    return out


# ── distillation (Phase 16c) ─────────────────────────────────────────────────
# The LLM emits candidate heuristics from an agent's recent history + outcomes;
# the coordinator attaches provenance, a low starting confidence, and a TTL.


class CandidateHeuristic(BaseModel):
    skill: str
    source: str | None = None
    heuristic: str
    rationale: str = ""


class ConsolidationResult(BaseModel):
    heuristics: list[CandidateHeuristic] = Field(default_factory=list)


def format_history_for_distillation(entries: list[EpisodicEntry]) -> str:
    """Render episodic entries for the distillation prompt: one line per action
    with its outcome label and a couple of decisive facets (so the LLM can spot
    patterns like 'forum extractions get contradicted') — richer than the
    in-prompt recall block, but still bounded."""
    lines: list[str] = []
    for e in entries:
        o = e.outcome
        facets: list[str] = []
        if e.event_type == "extraction":
            facets.append(f"claims={o.claims_total}")
            if o.claims_supporting:
                facets.append(f"supporting={o.claims_supporting}")
            if o.claims_contradicting:
                facets.append(f"contradicting={o.claims_contradicting}")
            if o.claims_contested:
                facets.append(f"contested={o.claims_contested}")
            if o.failure_modes:
                facets.append(f"failure_modes={','.join(o.failure_modes)}")
        else:
            facets.append(f"held={o.belief_currently_held}")
            if o.later_revisions:
                facets.append(f"later_revisions={o.later_revisions}")
        suffix = f" ({'; '.join(facets)})" if facets else ""
        lines.append(f"- [{o.label}] {e.action_summary}{suffix}")
    return "\n".join(lines)


def build_consolidation_prompt(
    agent: str, skill: str, entries: list[EpisodicEntry]
) -> tuple[str, str]:
    """Return (system, user) for distilling ``agent``'s history into heuristics.
    The static system prompt is the cached prefix; the per-agent history goes in
    the USER message."""
    user = format_consolidation_user(
        agent=agent,
        skill=skill,
        history_block=format_history_for_distillation(entries),
        n_entries=len(entries),
    )
    return CONSOLIDATION_SYSTEM, user


def candidate_to_proposal(
    agent: str,
    candidate: CandidateHeuristic,
    *,
    run_ids: list[str],
    claim_ids: list[str],
    ttl_days: int = DEFAULT_TTL_DAYS,
) -> HeuristicProposal:
    """Bind an LLM candidate to a persistable proposal: code (not the LLM) sets
    the low starting confidence, the TTL, and the provenance from the runs/claims
    the history was drawn from."""
    return HeuristicProposal(
        agent=agent,
        skill=candidate.skill,
        source=candidate.source,
        heuristic=candidate.heuristic,
        confidence=DEFAULT_CONFIDENCE,
        provenance_run_ids=run_ids,
        provenance_claim_ids=claim_ids,
        rationale=candidate.rationale,
        ttl_days=ttl_days,
    )


def distill_pure(
    llm: LLMClient, agent: str, skill: str, entries: list[EpisodicEntry]
) -> tuple[ConsolidationResult, LLMUsage, str]:
    """Synchronous distillation entry point (the sync fallback to the batch
    path). Returns an empty result on a parse failure rather than raising, so a
    single bad distillation never aborts the consolidation run."""
    system, user = build_consolidation_prompt(agent, skill, entries)
    try:
        result, _, usage = llm.complete_with_usage(
            name="consolidate_heuristics",
            system=system,
            user=user,
            response_model=ConsolidationResult,
        )
    except LLMProviderNotReadyError:
        raise
    except LLMResponseError as exc:
        logger.warning(
            "consolidation_parse_failure", extra={"agent": agent, "error": str(exc)}
        )
        return ConsolidationResult(), LLMUsage(), getattr(llm, "model", "")
    assert isinstance(result, ConsolidationResult)
    # usage.model is the realized model (correct under cheap→strong routing
    # escalation); fall back to the client attribute if unset.
    return result, usage, usage.model or getattr(llm, "model", "")
