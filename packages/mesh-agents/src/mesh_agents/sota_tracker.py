from __future__ import annotations

import re
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

import duckdb
from mesh_db.beliefs import list_beliefs
from pydantic import BaseModel

from mesh_agents.base import BaseAgent


class ResolvedClaim(BaseModel):
    claim_id: str
    subject_entity_id: str
    predicate: str
    object: dict[str, Any]
    source_id: str
    raw_excerpt: str
    confidence: float


class BeliefUpdate(BaseModel):
    topic: str
    new_statement: str
    new_confidence: float
    supporting_claim_ids: list[str]
    rationale: str
    is_new_belief: bool
    existing_belief_id: str | None = None


class SotaTrackerInput(BaseModel):
    claims_with_resolved_entities: list[ResolvedClaim]


class SotaTrackerOutput(BaseModel):
    belief_updates: list[BeliefUpdate]


def _parse_score(text: str) -> float | None:
    """Extract the first numeric score from a belief statement."""
    match = re.search(r"\b(\d+(?:\.\d+)?)\b", text)
    return float(match.group(1)) if match else None


def _score_from_object(obj: dict[str, Any]) -> float | None:
    for key in ("score", "value"):
        val = obj.get(key)
        if val is not None:
            try:
                return float(val)
            except (TypeError, ValueError):
                pass
    return None


class SotaTrackerAgent(BaseAgent):
    name = "sota_tracker"

    def __init__(self, llm: Any | None = None, db_conn: Any | None = None) -> None:
        super().__init__(llm=llm, db_conn=db_conn)

    async def run(self, input: BaseModel) -> SotaTrackerOutput:
        assert isinstance(input, SotaTrackerInput)

        score_claims = [
            c for c in input.claims_with_resolved_entities if c.predicate == "achieves_score"
        ]

        # Group by benchmark
        by_benchmark: dict[str, list[ResolvedClaim]] = defaultdict(list)
        for claim in score_claims:
            benchmark = claim.object.get("benchmark")
            if benchmark:
                by_benchmark[str(benchmark)].append(claim)

        updates: list[BeliefUpdate] = []

        for benchmark, claims in by_benchmark.items():
            topic = f"sota:{benchmark}"
            best_claim = max(
                claims,
                key=lambda c: _score_from_object(c.object) or 0.0,
            )
            best_score = _score_from_object(best_claim.object)
            if best_score is None:
                continue

            metric = best_claim.object.get("metric", "score")
            new_statement = (
                f"{best_claim.subject_entity_id} achieves {best_score} {metric} on {benchmark} "
                f"(as of {datetime.now(UTC).strftime('%Y-%m-%d')})"
            )

            existing = (
                _get_sota_belief(self.db_conn, topic) if self.db_conn is not None else None
            )

            if existing is None:
                updates.append(
                    BeliefUpdate(
                        topic=topic,
                        new_statement=new_statement,
                        new_confidence=0.5,
                        supporting_claim_ids=[best_claim.claim_id],
                        rationale=(
                            f"First recorded SOTA on {benchmark} from "
                            f"{best_claim.raw_excerpt[:100]}"
                        ),
                        is_new_belief=True,
                        existing_belief_id=None,
                    )
                )
            else:
                existing_score = _parse_score(existing.statement)
                if existing_score is None or best_score > existing_score:
                    updates.append(
                        BeliefUpdate(
                            topic=topic,
                            new_statement=new_statement,
                            new_confidence=0.5,
                            supporting_claim_ids=[best_claim.claim_id],
                            rationale=(
                                f"New SOTA on {benchmark}: {best_score} > {existing_score} "
                                f"from {best_claim.raw_excerpt[:100]}"
                            ),
                            is_new_belief=False,
                            existing_belief_id=existing.id,
                        )
                    )

        return SotaTrackerOutput(belief_updates=updates)


def _get_sota_belief(conn: duckdb.DuckDBPyConnection, topic: str) -> Any:
    beliefs = list_beliefs(conn, topic=topic, currently_held=True, limit=1)
    # list_beliefs uses ILIKE with wildcards, so filter exact match
    for b in beliefs:
        if b.topic == topic:
            return b
    return None
