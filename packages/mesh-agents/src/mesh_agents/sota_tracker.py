from __future__ import annotations

import re
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

from mesh_a2a.card_builder import build_agent_card
from mesh_a2a.task_server import build_task_app
from mesh_db.beliefs import list_beliefs
from mesh_db.connection import MeshConnection
from pydantic import BaseModel
from starlette.applications import Starlette

from mesh_agents.base import BaseAgent

# ---------------------------------------------------------------------------
# Shared types
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Phase 1 types (DB-backed, unchanged)
# ---------------------------------------------------------------------------


class SotaTrackerInput(BaseModel):
    claims_with_resolved_entities: list[ResolvedClaim]


class SotaTrackerOutput(BaseModel):
    belief_updates: list[BeliefUpdate]


# ---------------------------------------------------------------------------
# Phase 2 types (pure, no DB)
# ---------------------------------------------------------------------------


class BeliefSummary(BaseModel):
    """Lightweight view of an existing SOTA belief, passed in from the coordinator."""

    belief_id: str
    topic: str
    statement: str
    confidence: float


class SotaUpdateSkillInput(BaseModel):
    """Input for the update_sota A2A skill."""

    claims: list[ResolvedClaim]
    existing_sota_beliefs: list[BeliefSummary] = []


class SotaUpdateSkillOutput(BaseModel):
    """Output for the update_sota A2A skill."""

    belief_updates: list[BeliefUpdate]


# ---------------------------------------------------------------------------
# Shared logic helpers
# ---------------------------------------------------------------------------


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


def _compute_belief_updates(
    score_claims: list[ResolvedClaim],
    get_existing: Any,  # callable(topic: str) -> BeliefSummary | None
) -> list[BeliefUpdate]:
    by_benchmark: dict[str, list[ResolvedClaim]] = defaultdict(list)
    for claim in score_claims:
        benchmark = claim.object.get("benchmark")
        if benchmark:
            by_benchmark[str(benchmark)].append(claim)

    updates: list[BeliefUpdate] = []
    for benchmark, claims in by_benchmark.items():
        topic = f"sota:{benchmark}"
        best_claim = max(claims, key=lambda c: _score_from_object(c.object) or 0.0)
        best_score = _score_from_object(best_claim.object)
        if best_score is None:
            continue

        metric = best_claim.object.get("metric", "score")
        new_statement = (
            f"{best_claim.subject_entity_id} achieves {best_score} {metric} on {benchmark} "
            f"(as of {datetime.now(UTC).strftime('%Y-%m-%d')})"
        )

        existing = get_existing(topic)

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
                        existing_belief_id=existing.belief_id,
                    )
                )
    return updates


def update_sota_pure(
    claims: list[ResolvedClaim],
    existing_sota_beliefs: list[BeliefSummary],
) -> list[BeliefUpdate]:
    """Pure SOTA computation — no DB access.

    The coordinator pre-fetches existing SOTA beliefs and passes them here.
    """
    belief_map = {b.topic: b for b in existing_sota_beliefs}
    score_claims = [c for c in claims if c.predicate == "achieves_score"]
    return _compute_belief_updates(score_claims, lambda topic: belief_map.get(topic))


async def _handle_update_sota(payload: dict[str, Any]) -> dict[str, Any]:
    skill_input = SotaUpdateSkillInput.model_validate(payload)
    updates = update_sota_pure(skill_input.claims, skill_input.existing_sota_beliefs)
    return SotaUpdateSkillOutput(belief_updates=updates).model_dump(mode="json")


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------


class SotaTrackerAgent(BaseAgent):
    name = "sota_tracker"

    def __init__(self, llm: Any | None = None, db_conn: Any | None = None) -> None:
        super().__init__(llm=llm, db_conn=db_conn)

    async def run(self, input: BaseModel) -> SotaTrackerOutput:
        """Phase 1 path: optionally queries the DB for existing beliefs."""
        assert isinstance(input, SotaTrackerInput)
        score_claims = [
            c for c in input.claims_with_resolved_entities if c.predicate == "achieves_score"
        ]

        def _get_existing(topic: str) -> Any:
            if self.db_conn is None:
                return None
            return _get_sota_belief_as_summary(self.db_conn, topic)

        updates = _compute_belief_updates(score_claims, _get_existing)
        return SotaTrackerOutput(belief_updates=updates)

    async def run_skill(self, input: SotaUpdateSkillInput) -> SotaUpdateSkillOutput:
        """Phase 2 path: pure, no DB. Used by tests."""
        updates = update_sota_pure(input.claims, input.existing_sota_beliefs)
        return SotaUpdateSkillOutput(belief_updates=updates)

    def to_a2a_server(self, url: str) -> Starlette:
        card = build_agent_card(
            name="SOTA Tracker",
            description="Computes SOTA belief updates from resolved claims.",
            url=url,
            skill_id="update_sota",
            skill_name="Update SOTA",
            skill_description="Compare new achievement claims against existing SOTA beliefs.",
            skill_tags=["sota", "beliefs", "tracking"],
        )
        return build_task_app(
            agent_card=card,
            skill_handlers={"update_sota": _handle_update_sota},
            agent_name="sota_tracker",
        )


# ---------------------------------------------------------------------------
# DB helpers (Phase 1 only, not exported to A2A skill)
# ---------------------------------------------------------------------------


def _get_sota_belief(conn: MeshConnection, topic: str) -> Any:
    beliefs = list_beliefs(conn, topic=topic, currently_held=True, limit=1)
    for b in beliefs:
        if b.topic == topic:
            return b
    return None


def _get_sota_belief_as_summary(
    conn: MeshConnection, topic: str
) -> BeliefSummary | None:
    belief = _get_sota_belief(conn, topic)
    if belief is None:
        return None
    return BeliefSummary(
        belief_id=belief.id,
        topic=belief.topic,
        statement=belief.statement,
        confidence=belief.confidence,
    )
