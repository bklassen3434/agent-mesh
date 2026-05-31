from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any
from unittest.mock import patch

from mesh_agents.consolidator import CandidateHeuristic, ConsolidationResult
from mesh_db.claims import create_claim
from mesh_db.connection import get_connection
from mesh_db.entities import create_entity
from mesh_db.heuristics import list_heuristics
from mesh_db.pipeline_runs import list_pipeline_runs
from mesh_db.sources import create_source
from mesh_llm import LLMUsage
from mesh_models.claim import Claim
from mesh_models.entity import Entity, EntityType
from mesh_models.source import Source, SourceType


def _seed_extraction_history(db: str | None) -> None:
    """Seed a handful of claim_extractor extraction events so recall_history has
    something to distill."""
    conn = get_connection(db)
    entity = create_entity(conn, Entity(canonical_name="ForumModel", type=EntityType.model))
    for i in range(3):
        source = create_source(
            conn,
            Source(
                type=SourceType.reddit,
                url=f"https://reddit.com/r/ml/{i}",
                published_at=datetime.now(UTC),
                raw_content_hash=f"hash-{i}",
            ),
        )
        create_claim(
            conn,
            Claim(
                predicate="achieves_score",
                subject_entity_id=entity.id,
                object={"score": 90.0 + i, "benchmark": "MMLU"},
                source_id=source.id,
                extracted_by_agent="claim_extractor",
                raw_excerpt=f"forum claim {i}",
                confidence=0.7,
            ),
        )
    conn.close()


class _MockLLM:
    """Returns a canned ConsolidationResult. Not an AnthropicClient, so the
    consolidation graph routes through the synchronous distill path."""

    model = "mock-model"

    def complete_with_usage(
        self,
        name: str,
        system: str,
        user: str,
        response_model: type | None = None,
        options: object | None = None,
    ) -> tuple[object, int, LLMUsage]:
        result = ConsolidationResult(
            heuristics=[
                CandidateHeuristic(
                    skill="extract_claims",
                    source="reddit",
                    heuristic="Treat single-source forum score claims as low-confidence.",
                    rationale="forum extractions recur without corroboration",
                )
            ]
        )
        return result, 100, LLMUsage(input_tokens=200, output_tokens=40)


def _run(db: str | None) -> Any:
    from mesh_pipeline.consolidation import run_consolidation

    with patch(
        "mesh_pipeline.consolidation.make_llm_client", return_value=_MockLLM()
    ):
        return asyncio.run(run_consolidation(db_path=db))


def test_consolidation_writes_grounded_heuristic(tmp_db: Any) -> None:
    _seed_extraction_history(None)
    result = _run(None)

    assert result.heuristics_written >= 1
    assert result.targets_with_history == 1  # only claim_extractor has history

    written = list_heuristics(tmp_db, agent="claim_extractor", skill="extract_claims")
    assert len(written) == 1
    h = written[0]
    assert h.confidence == 0.3  # low starting confidence
    assert h.expires_at > h.created_at  # TTL set
    assert h.source == "reddit"
    assert h.provenance_claim_ids  # grounded in the claims it was distilled from

    runs = list_pipeline_runs(tmp_db, limit=10, run_type="consolidation")
    assert len(runs) == 1


def test_consolidation_dedupes_on_rerun(tmp_db: Any) -> None:
    _seed_extraction_history(None)
    _run(None)
    _run(None)  # identical candidate the second time
    written = list_heuristics(tmp_db, agent="claim_extractor", skill="extract_claims")
    assert len(written) == 1  # not duplicated


def test_consolidation_no_history_is_noop(tmp_db: Any) -> None:
    result = _run(None)
    assert result.heuristics_written == 0
    assert result.targets_with_history == 0
    assert list_heuristics(tmp_db) == []
