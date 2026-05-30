"""Pre-aggregated knowledge-graph reads (Phase 9).

The wiki's /graph route needs node density (belief count) and edge weight
(claim count) computed server-side over the whole mesh, capped to a
bounded node set. Keeping the aggregation here (not in the API router or
the browser) means the SQL is one place, testable, and the payload the
browser receives is already small.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from mesh_db.connection import MeshConnection

# Hard cap on rendered nodes — top-N by belief count. Not configurable.
NODE_CAP = 200


def graph_nodes(
    conn: MeshConnection, limit: int = NODE_CAP
) -> list[dict[str, Any]]:
    """Top entities by belief count, with last-claim recency.

    belief_count = distinct currently-held beliefs that any claim about the
    entity supports or contradicts. last_claim_at = most recent claim with
    the entity as subject.
    """
    rows = conn.execute(
        """
        WITH belief_claims AS (
            SELECT id AS belief_id, UNNEST(supporting_claim_ids) AS claim_id
            FROM beliefs WHERE is_currently_held = TRUE
            UNION ALL
            SELECT id AS belief_id, UNNEST(contradicting_claim_ids) AS claim_id
            FROM beliefs WHERE is_currently_held = TRUE
        ),
        entity_beliefs AS (
            SELECT c.subject_entity_id AS entity_id,
                   COUNT(DISTINCT bc.belief_id) AS belief_count
            FROM belief_claims bc
            JOIN claims c ON c.id = bc.claim_id
            GROUP BY c.subject_entity_id
        ),
        entity_last_claim AS (
            SELECT subject_entity_id AS entity_id, MAX(extracted_at) AS last_claim_at
            FROM claims GROUP BY subject_entity_id
        )
        SELECT e.id, e.canonical_name, e.type,
               COALESCE(eb.belief_count, 0) AS belief_count,
               elc.last_claim_at
        FROM entities e
        LEFT JOIN entity_beliefs eb ON eb.entity_id = e.id
        LEFT JOIN entity_last_claim elc ON elc.entity_id = e.id
        ORDER BY belief_count DESC, e.created_at DESC
        LIMIT %s
        """,
        [limit],
    ).fetchall()
    return [
        {
            "id": str(r[0]),
            "label": str(r[1]),
            "type": str(r[2]),
            "belief_count": int(r[3]),
            "last_claim_at": r[4] if isinstance(r[4], datetime) else None,
        }
        for r in rows
    ]


def graph_edges(
    conn: MeshConnection, node_ids: set[str]
) -> list[dict[str, Any]]:
    """Relationships whose both endpoints are in the rendered node set.

    claim_count = number of evidence claims on the relationship, driving
    edge stroke width in the UI.
    """
    if not node_ids:
        return []
    rows = conn.execute(
        """
        SELECT from_entity_id, to_entity_id, type,
               COALESCE(cardinality(evidence_claim_ids), 0) AS claim_count
        FROM relationships
        """
    ).fetchall()
    return [
        {
            "source": str(r[0]),
            "target": str(r[1]),
            "relationship_type": str(r[2]),
            "claim_count": int(r[3]),
        }
        for r in rows
        if str(r[0]) in node_ids and str(r[1]) in node_ids
    ]
