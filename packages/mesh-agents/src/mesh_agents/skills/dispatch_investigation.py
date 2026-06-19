"""Market skill: ``dispatch-investigation`` — work an open investigation.

investigate-gap *opens* investigations; this skill *works* them — the market
analog of the coordinator's ``dispatch_open_investigations``. For an open (or
in-progress) investigation it runs hypothesis-directed search across the field's
enabled connectors (in-process, no A2A), acquires the gathered sources tagged with
the investigation's lineage, and advances the lifecycle.

It is deliberately **LLM-free and self-limiting**: it only *acquires* evidence
sources (each carrying ``investigation_id`` in its payload). Those sources are
read by ``extract-source`` in a later round, which attaches the resulting claims
back to the investigation (``AttachClaimToInvestigationEffect``). On the next run
this skill sees the attached claims and resolves the investigation once it clears
the claim threshold, or abandons it once it exhausts its run budget — the same
thresholds the coordinator uses (``MESH_INVESTIGATION_*``). The skill never writes;
it returns effects for the gateway.
"""
from __future__ import annotations

import os
from typing import Any

from mesh_db.connectors import list_field_connectors
from mesh_db.investigations import get_investigation_by_id
from mesh_db.sources import list_sources
from mesh_models.effect import (
    CreateSourceEffect,
    Effect,
    UpdateInvestigationEffect,
)
from mesh_models.investigation import InvestigationStatus
from mesh_models.tension import Tension, TensionKind

from mesh_agents.arxiv_scout import ScoutedPaper
from mesh_agents.connector import investigate_source_name
from mesh_agents.connector_dispatch import has_investigate, investigate_connector
from mesh_agents.skill import Bid, register_skill

_EST_COST_USD = 0.05


def _claims_threshold() -> int:
    return int(os.environ.get("MESH_INVESTIGATION_CLAIMS_THRESHOLD", "3"))


def _max_runs() -> int:
    return int(os.environ.get("MESH_INVESTIGATION_MAX_RUNS", "5"))


def _max_fetch() -> int:
    return int(os.environ.get("MESH_MARKET_INVESTIGATE_MAX", "10"))


@register_skill
class DispatchInvestigationSkill:
    """Bid on ``open_investigation`` tensions; gather evidence and advance the
    investigation's lifecycle (resolve / abandon / keep gathering)."""

    skill_id = "dispatch-investigation"
    handles = (TensionKind.open_investigation,)

    def bid(self, conn: Any, tension: Tension) -> Bid | None:
        if not tension.target_ref.get("investigation_id"):
            return None
        return Bid(value=tension.value, est_cost_usd=_EST_COST_USD)

    async def run(
        self, conn: Any, tension: Tension, *, budget_usd: float
    ) -> list[Effect]:
        inv_id = tension.target_ref.get("investigation_id")
        if not inv_id:
            return []
        inv = get_investigation_by_id(conn, inv_id)
        if inv is None or inv.status not in (
            InvestigationStatus.open,
            InvestigationStatus.in_progress,
        ):
            return []

        # Lifecycle short-circuit (the coordinator's _investigation_lifecycle):
        # resolve once enough evidence has been attached, abandon once the run
        # budget is spent — both terminal, no fetch needed.
        if len(inv.collected_claim_ids) >= _claims_threshold():
            return [_terminal(inv_id, InvestigationStatus.resolved)]
        if inv.pipeline_runs_attempted >= _max_runs():
            return [_terminal(inv_id, InvestigationStatus.abandoned)]

        # Otherwise gather more evidence from the enabled, investigable sources.
        field_id = tension.field_id
        enabled = {
            investigate_source_name(fc.connector_id)
            for fc in list_field_connectors(conn, field_id, enabled_only=True)
        }
        existing = {
            s.raw_content_hash for s in list_sources(conn, limit=10000, field_id=field_id)
        }
        seen: set[str] = set()
        effects: list[Effect] = []
        fetched = 0
        for source_type in inv.suggested_source_types:
            if fetched >= _max_fetch():
                break
            if source_type not in enabled or not has_investigate(source_type):
                continue
            papers = await investigate_connector(
                source_type,
                investigation_id=inv_id,
                hypothesis=inv.hypothesis or inv.question,
                target_entity_id=inv.target_entity_id,
                suggested_source_types=inv.suggested_source_types,
                max_results=_max_fetch() - fetched,
            )
            for paper in papers:
                h = paper.source.raw_content_hash
                if h in existing or h in seen:
                    continue
                seen.add(h)
                effects.append(
                    CreateSourceEffect(field_id=field_id, source=_tag(paper, inv_id))
                )
                fetched += 1

        # Move to in-progress and count the attempt (so it eventually abandons even
        # if no source is ever found). The effect is emitted regardless of fetches.
        effects.append(
            UpdateInvestigationEffect(
                investigation_id=inv_id,
                status=InvestigationStatus.in_progress.value,
                increment_attempts=True,
            )
        )
        return effects


def _terminal(inv_id: str, status: InvestigationStatus) -> UpdateInvestigationEffect:
    return UpdateInvestigationEffect(
        investigation_id=inv_id, status=status.value, set_resolved_at=True
    )


def _tag(paper: ScoutedPaper, investigation_id: str) -> Any:
    """Persist the scouted content + the investigation lineage on the source so
    extract-source can both read the paper and attach its claims back."""
    source = paper.source
    source.payload = {
        "title": paper.title,
        "abstract": paper.abstract,
        "investigation_id": investigation_id,
    }
    return source
