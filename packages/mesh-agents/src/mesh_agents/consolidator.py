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

from typing import Any

from mesh_models.heuristic import DEFAULT_CONFIDENCE, DEFAULT_TTL_DAYS
from pydantic import BaseModel, Field


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
