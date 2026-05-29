"""Shared investigation helpers used by the coordinator + skeptic-sweep graphs.

Curator emits ``InvestigationSuggestion``s; both the sweep (when ranking
beliefs) and the coordinator's curate node persist them into ``Investigation``
rows. Factored here so there's a single implementation of the de-dup +
target-entity logic.
"""
from __future__ import annotations

from collections import Counter
from typing import Any

from mesh_agents.curator import InvestigationSuggestion
from mesh_db.beliefs import get_belief_by_id
from mesh_db.claims import get_claims_by_ids
from mesh_db.investigations import create_investigation, list_investigations
from mesh_models.belief import Belief
from mesh_models.investigation import Investigation, InvestigationStatus


def target_entity_for_belief(conn: Any, belief: Belief) -> str | None:
    """Best-effort target entity = most common subject across the belief's
    supporting + contradicting claims, so scouts know where to look. None when
    the belief has no claims yet (rare; usually only at bootstrap time)."""
    ids = list(belief.supporting_claim_ids) + list(belief.contradicting_claim_ids)
    if not ids:
        return None
    claims = get_claims_by_ids(conn, ids)
    if not claims:
        return None
    most_common, _ = Counter(c.subject_entity_id for c in claims).most_common(1)[0]
    return most_common


def persist_investigation_suggestions(
    conn: Any, suggestions: list[InvestigationSuggestion]
) -> int:
    """Translate Curator suggestions into Investigation rows.

    Skips beliefs that already have an open or in_progress investigation so the
    same stale belief doesn't spawn duplicates on every pass.
    """
    if not suggestions:
        return 0
    existing_belief_ids = {
        inv.opened_by_belief_id
        for inv in list_investigations(conn, limit=1000)
        if inv.status in (InvestigationStatus.open, InvestigationStatus.in_progress)
        and inv.opened_by_belief_id
    }
    n = 0
    for s in suggestions:
        if s.belief_id in existing_belief_ids:
            continue
        belief = get_belief_by_id(conn, s.belief_id)
        target = target_entity_for_belief(conn, belief) if belief is not None else None
        create_investigation(
            conn,
            Investigation(
                question=s.hypothesis,
                hypothesis=s.hypothesis,
                target_entity_id=target,
                suggested_source_types=s.suggested_source_types,
                opened_by_belief_id=s.belief_id,
                related_entity_ids=[target] if target else [],
            ),
        )
        n += 1
    return n
