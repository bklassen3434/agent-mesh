"""Consolidator agent — distills episodic history into procedural heuristics.

The data contract lives here (Phase 16b); the offline LangGraph job that calls
the LLM to produce these proposals lives in ``apps/pipeline`` (Phase 16c).

Write split (coordinator-owned writes): an agent *proposes* a heuristic; only the
write gateway persists it (mesh_writer role). The controller's
``consolidate-memory`` skill builds the rows here (``proposal_to_heuristic``) and
emits a ``WriteHeuristicEffect``; the gateway inserts them. The proposal types are
also the wire contract a future agent-hosted proposer would use.
"""
from __future__ import annotations

import logging
import os
from datetime import UTC, datetime, timedelta
from typing import Any

from mesh_db.episodic import EpisodicEntry
from mesh_db.heuristics import list_heuristics
from mesh_llm import LLMClient, LLMProviderNotReadyError, LLMResponseError, LLMUsage
from mesh_llm.prompts import CONSOLIDATION_SYSTEM, format_consolidation_user
from mesh_models.heuristic import (
    DEFAULT_CONFIDENCE,
    DEFAULT_TTL_DAYS,
    AgentHeuristic,
    AgentHeuristicRevision,
)
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# (agent, skill) pairs to consolidate — the LLM skills that stamp episodic
# artifacts. Each agent id matches the identity recall_history keys on.
_DEFAULT_TARGETS: list[tuple[str, str]] = [
    ("claim_extractor", "extract_claims"),
    ("skeptic", "challenge_belief"),
]
# Cap on how many existing heuristics to scan for the dedup check.
_MAX_DEDUP = 200


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


# ── consolidation orchestration helpers (Phase 16c) ──────────────────────────
# Shared between the controller's ``consolidate-memory`` skill and (until it is
# retired) the standalone sweep. Pure reads + env config — no writes.


def consolidation_targets() -> list[tuple[str, str]]:
    """The (agent, skill) pairs to distil. ``MESH_CONSOLIDATION_TARGETS`` is a
    comma-separated ``agent:skill`` override; empty → the built-in defaults."""
    raw = os.environ.get("MESH_CONSOLIDATION_TARGETS", "")
    if not raw:
        return _DEFAULT_TARGETS
    out: list[tuple[str, str]] = []
    for pair in raw.split(","):
        agent, _, skill = pair.strip().partition(":")
        if agent and skill:
            out.append((agent, skill))
    return out or _DEFAULT_TARGETS


def consolidation_history_limit() -> int:
    return int(os.environ.get("MESH_CONSOLIDATION_HISTORY_LIMIT", "50"))


def consolidation_ttl_days() -> int:
    return int(os.environ.get("MESH_CONSOLIDATION_TTL_DAYS", "30"))


def provenance_from_entries(entries: list[EpisodicEntry]) -> tuple[list[str], list[str]]:
    """Collect the runs + claims an agent's history was drawn from — the
    provenance every distilled heuristic links back to."""
    run_ids: set[str] = set()
    claim_ids: set[str] = set()
    for e in entries:
        if e.run_id:
            run_ids.add(e.run_id)
        for cid in e.refs.get("claim_ids", []) or []:
            claim_ids.add(str(cid))
        for cid in e.refs.get("trigger_claim_ids", []) or []:
            claim_ids.add(str(cid))
    return sorted(run_ids), sorted(claim_ids)


def heuristic_already_present(
    conn: Any, agent: str, skill: str, text: str, field_id: str
) -> bool:
    """Skip a candidate whose exact text is already an active, unexpired heuristic
    for this scope — avoids re-distilled duplicates flooding the store across
    runs."""
    existing = list_heuristics(
        conn, agent=agent, skill=skill, active=True, include_expired=False,
        limit=_MAX_DEDUP, field_id=field_id,
    )
    return any(h.heuristic.strip() == text.strip() for h in existing)


def proposal_to_heuristic(
    proposal: HeuristicProposal,
    *,
    now: datetime | None = None,
    revised_by_agent: str = "consolidator",
) -> tuple[AgentHeuristic, AgentHeuristicRevision]:
    """Build the head row + genesis revision for a *new* heuristic, WITHOUT
    writing (the gateway inserts both via a ``WriteHeuristicEffect``). Mirrors the
    coordinator's former ``persist_heuristic`` build: ``ttl_days`` becomes an
    absolute ``expires_at``, and the genesis revision records creation from
    nothing so the log is complete from the first persist. Caller must have
    checked ``proposal.has_provenance()``."""
    now = now or datetime.now(UTC)
    heuristic = AgentHeuristic(
        agent=proposal.agent,
        skill=proposal.skill,
        source=proposal.source,
        entity_id=proposal.entity_id,
        heuristic=proposal.heuristic,
        confidence=proposal.confidence,
        provenance_run_ids=proposal.provenance_run_ids,
        provenance_claim_ids=proposal.provenance_claim_ids,
        created_at=now,
        last_revised_at=now,
        revision_count=0,
        expires_at=now + timedelta(days=proposal.ttl_days),
        is_currently_active=True,
    )
    genesis = AgentHeuristicRevision(
        heuristic_id=heuristic.id,
        previous_heuristic="",
        new_heuristic=heuristic.heuristic,
        previous_confidence=0.0,
        new_confidence=heuristic.confidence,
        provenance_run_ids=heuristic.provenance_run_ids,
        provenance_claim_ids=heuristic.provenance_claim_ids,
        revised_by_agent=revised_by_agent,
        revised_at=now,
        rationale=proposal.rationale or "distilled from recent episodic history",
    )
    return heuristic, genesis
