from __future__ import annotations

import asyncio

from mesh_agents.sota_tracker import ResolvedClaim, SotaTrackerAgent, SotaTrackerInput
from mesh_db.beliefs import create_belief
from mesh_db.connection import MeshConnection
from mesh_models.belief import Belief


def _rc(
    claim_id: str,
    entity_id: str,
    benchmark: str,
    score: float,
    predicate: str = "achieves_score",
) -> ResolvedClaim:
    return ResolvedClaim(
        claim_id=claim_id,
        subject_entity_id=entity_id,
        predicate=predicate,
        object={"score": score, "benchmark": benchmark, "metric": "accuracy"},
        source_id="src-1",
        raw_excerpt=f"achieves {score} on {benchmark}",
        confidence=0.9,
    )


class TestSotaTrackerAgent:
    def test_new_benchmark_creates_belief(self, tmp_db: MeshConnection) -> None:
        agent = SotaTrackerAgent(db_conn=tmp_db)
        output = asyncio.run(
            agent.run(SotaTrackerInput(claims_with_resolved_entities=[
                _rc("c1", "entity-1", "MMLU", 87.5)
            ]))
        )
        assert len(output.belief_updates) == 1
        assert output.belief_updates[0].is_new_belief is True
        assert output.belief_updates[0].topic == "sota:MMLU"

    def test_better_score_creates_revision(self, tmp_db: MeshConnection) -> None:
        # Pre-existing belief with score 80
        belief = Belief(
            topic="sota:MMLU",
            statement="OldModel achieves 80.0 accuracy on MMLU (as of 2024-01-01)",
            confidence=0.5,
        )
        create_belief(tmp_db, belief)

        agent = SotaTrackerAgent(db_conn=tmp_db)
        output = asyncio.run(
            agent.run(SotaTrackerInput(claims_with_resolved_entities=[
                _rc("c2", "entity-2", "MMLU", 92.0)
            ]))
        )
        assert len(output.belief_updates) == 1
        update = output.belief_updates[0]
        assert update.is_new_belief is False
        assert update.existing_belief_id == belief.id

    def test_worse_score_no_update(self, tmp_db: MeshConnection) -> None:
        belief = Belief(
            topic="sota:MMLU",
            statement="SomeModel achieves 95.0 accuracy on MMLU (as of 2024-01-01)",
            confidence=0.5,
        )
        create_belief(tmp_db, belief)

        agent = SotaTrackerAgent(db_conn=tmp_db)
        output = asyncio.run(
            agent.run(SotaTrackerInput(claims_with_resolved_entities=[
                _rc("c3", "entity-3", "MMLU", 80.0)
            ]))
        )
        assert output.belief_updates == []

    def test_non_score_claims_ignored(self, tmp_db: MeshConnection) -> None:
        agent = SotaTrackerAgent(db_conn=tmp_db)
        non_score = _rc("c4", "entity-4", "MMLU", 90.0, predicate="outperforms")
        output = asyncio.run(
            agent.run(SotaTrackerInput(claims_with_resolved_entities=[non_score]))
        )
        assert output.belief_updates == []

    def test_multiple_benchmarks_in_one_batch(self, tmp_db: MeshConnection) -> None:
        agent = SotaTrackerAgent(db_conn=tmp_db)
        output = asyncio.run(
            agent.run(SotaTrackerInput(claims_with_resolved_entities=[
                _rc("c5", "e1", "MMLU", 87.5),
                _rc("c6", "e2", "HumanEval", 72.0),
            ]))
        )
        topics = {u.topic for u in output.belief_updates}
        assert "sota:MMLU" in topics
        assert "sota:HumanEval" in topics

    def test_multiple_claims_same_benchmark_picks_best(
        self, tmp_db: MeshConnection
    ) -> None:
        agent = SotaTrackerAgent(db_conn=tmp_db)
        output = asyncio.run(
            agent.run(SotaTrackerInput(claims_with_resolved_entities=[
                _rc("c7", "e1", "MMLU", 80.0),
                _rc("c8", "e2", "MMLU", 92.5),
                _rc("c9", "e3", "MMLU", 85.0),
            ]))
        )
        assert len(output.belief_updates) == 1
        update = output.belief_updates[0]
        assert "92.5" in update.new_statement

    def test_claim_without_benchmark_skipped(self, tmp_db: MeshConnection) -> None:
        agent = SotaTrackerAgent(db_conn=tmp_db)
        claim = ResolvedClaim(
            claim_id="c10",
            subject_entity_id="e1",
            predicate="achieves_score",
            object={"score": 88.0},  # no benchmark key
            source_id="src-1",
            raw_excerpt="achieves 88.0",
            confidence=0.9,
        )
        output = asyncio.run(
            agent.run(SotaTrackerInput(claims_with_resolved_entities=[claim]))
        )
        assert output.belief_updates == []
