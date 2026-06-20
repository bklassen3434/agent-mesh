"""Skill: ``consolidate-memory`` — distil episodic history into procedural memory.

The controller analog of the Phase-16c consolidation job. A cooldown-gated
``consolidatable_memory`` tension (one per field with history, due on a timer like
scouting) routes here; the skill walks the consolidation targets, distils each
agent's recent episodic history into candidate heuristics (one synchronous LLM
call per target — the controller has no cross-round batch loop, so the Batch-API
path the standalone sweep used is traded for inline calls), binds them to
provenance, and emits one append-only ``WriteHeuristicEffect`` per fresh
heuristic. Nothing is revised or deleted — a stale heuristic simply expires.

Decision/write split: the skill builds the heuristic + genesis revision rows and
checks for duplicates, but the gateway performs the insert. Conservative — no
LLM (or an unready provider) yields no effects, and a re-distilled duplicate is
skipped, so a scheduled re-run never floods the store.
"""
from __future__ import annotations

from typing import Any

from mesh_db.episodic import recall_history
from mesh_llm import LLMProviderNotReadyError
from mesh_llm.protocol import LLMClient
from mesh_models.effect import WriteHeuristicEffect
from mesh_models.tension import Tension, TensionKind

from mesh_agents.consolidator import (
    candidate_to_proposal,
    consolidation_history_limit,
    consolidation_targets,
    consolidation_ttl_days,
    distill_pure,
    heuristic_already_present,
    proposal_to_heuristic,
    provenance_from_entries,
)
from mesh_agents.skill import register_skill

_AGENT = "consolidator"
# One synchronous distillation call per target (two by default). Rough estimate
# so the cost/agenda views don't under-weight a consolidation pass.
_EST_COST_USD = 0.04


@register_skill
class ConsolidateMemorySkill:
    """Distil each target agent's recent history → ``WriteHeuristicEffect``s."""

    skill_id = "consolidate-memory"
    handles = (TensionKind.consolidatable_memory,)

    def __init__(self, llm: LLMClient | None = None) -> None:
        self._llm = llm  # injectable for tests; built lazily in run otherwise

    async def run(
        self, conn: Any, tension: Tension, *, budget_usd: float
    ) -> list[Any]:
        field_id = tension.target_ref.get("field_id") or tension.field_id
        llm = self._resolve_llm()
        if llm is None:
            return []  # no adjudicator/provider → nothing to distil this pass

        limit = consolidation_history_limit()
        ttl = consolidation_ttl_days()
        effects: list[Any] = []
        for agent, skill in consolidation_targets():
            entries = recall_history(conn, agent, limit=limit, field_id=field_id)
            if not entries:
                continue
            try:
                result, _usage, _model = distill_pure(llm, agent, skill, entries)
            except LLMProviderNotReadyError:
                return effects  # provider died mid-pass — keep what we have
            run_ids, claim_ids = provenance_from_entries(entries)
            for candidate in result.heuristics:
                if heuristic_already_present(conn, agent, skill, candidate.heuristic, field_id):
                    continue
                proposal = candidate_to_proposal(
                    agent, candidate, run_ids=run_ids, claim_ids=claim_ids, ttl_days=ttl
                )
                if not proposal.has_provenance():
                    continue  # a heuristic with no justifying run/claim is rejected
                heuristic, genesis = proposal_to_heuristic(proposal, revised_by_agent=_AGENT)
                effects.append(
                    WriteHeuristicEffect(
                        field_id=field_id, heuristic=heuristic, genesis_revision=genesis
                    )
                )
        return effects

    def _resolve_llm(self) -> LLMClient | None:
        """The injected client if present, else a best-effort routed consolidation
        client. A missing/unready provider degrades to "no heuristics" rather than
        aborting — same posture as the standalone consolidation sweep."""
        if self._llm is not None:
            return self._llm
        try:
            from mesh_llm import make_routed_llm_client

            llm = make_routed_llm_client(agent_name=_AGENT)
            llm.health_check()
            return llm
        except Exception:
            return None
