from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from mesh_agents.claim_extractor import _build_handler
from mesh_agents.memory import build_memory_block
from mesh_db.connection import MeshConnection
from mesh_db.heuristics import create_heuristic
from mesh_llm import LLMUsage
from mesh_models.claim import Claim
from mesh_models.entity import Entity, EntityType
from mesh_models.heuristic import AgentHeuristic
from mesh_models.source import Source, SourceType


def _seed_heuristic(conn: MeshConnection, **kwargs: object) -> AgentHeuristic:
    defaults: dict[str, object] = {
        "agent": "claim_extractor",
        "skill": "extract_claims",
        "heuristic": "Forum score claims are self-reported; lower their confidence.",
    }
    defaults.update(kwargs)
    h = AgentHeuristic(**defaults)  # type: ignore[arg-type]
    create_heuristic(conn, h)
    return h


def test_build_memory_block_includes_active_excludes_expired(
    tmp_db: MeshConnection,
) -> None:
    now = datetime.now(UTC)
    _seed_heuristic(tmp_db, heuristic="ACTIVE-RULE")
    _seed_heuristic(
        tmp_db, heuristic="EXPIRED-RULE", expires_at=now - timedelta(days=1)
    )
    _seed_heuristic(
        tmp_db, heuristic="RETIRED-RULE", is_currently_active=False
    )
    block = build_memory_block("claim_extractor", "extract_claims")
    assert "ACTIVE-RULE" in block
    assert "EXPIRED-RULE" not in block
    assert "RETIRED-RULE" not in block
    assert "LEARNED HEURISTICS" in block


def test_wrong_skill_scope_excluded(tmp_db: MeshConnection) -> None:
    _seed_heuristic(tmp_db, heuristic="EXTRACT-RULE")
    block = build_memory_block("claim_extractor", "challenge_belief")
    assert "EXTRACT-RULE" not in block


class _CapturingLLM:
    """Captures the user prompt and returns a canned empty extraction."""

    model = "mock-model"

    def __init__(self) -> None:
        self.last_user: str | None = None
        self.last_system: str | None = None

    def complete_with_usage(
        self,
        name: str,
        system: str,
        user: str,
        response_model: type | None = None,
        options: object | None = None,
    ) -> tuple[object, int, LLMUsage]:
        self.last_system = system
        self.last_user = user
        assert response_model is not None
        return response_model(claims=[]), 50, LLMUsage(input_tokens=10, output_tokens=5)


def test_extract_skill_folds_memory_after_cache_prefix(tmp_db: MeshConnection) -> None:
    # Seed a heuristic + an extraction event so both memory sources have content.
    _seed_heuristic(tmp_db, heuristic="UNIQUE-HEURISTIC-TOKEN")
    entity = Entity(canonical_name="X", type=EntityType.model)
    from mesh_db.claims import create_claim
    from mesh_db.entities import create_entity
    from mesh_db.sources import create_source

    create_entity(tmp_db, entity)
    src = create_source(
        tmp_db,
        Source(
            type=SourceType.arxiv,
            url="https://arxiv.org/abs/1",
            published_at=datetime.now(UTC),
            raw_content_hash="h1",
        ),
    )
    create_claim(
        tmp_db,
        Claim(
            predicate="achieves_score",
            subject_entity_id=entity.id,
            object={"score": 9, "benchmark": "B"},
            source_id=src.id,
            extracted_by_agent="claim_extractor",
            raw_excerpt="x",
            confidence=0.9,
        ),
    )

    llm = _CapturingLLM()
    handler = _build_handler(llm, "claim_extractor")  # type: ignore[arg-type]
    payload = {
        "paper": {
            "source": src.model_dump(mode="json"),
            "title": "A Paper",
            "abstract": "Some abstract text.",
            "arxiv_id": "1",
        }
    }
    asyncio.run(handler(payload))

    assert llm.last_user is not None
    # The heuristic is in the USER message (after the cached system prefix).
    assert "UNIQUE-HEURISTIC-TOKEN" in llm.last_user
    assert "UNIQUE-HEURISTIC-TOKEN" not in (llm.last_system or "")
    # The task content (the paper) is still present.
    assert "Some abstract text." in llm.last_user
    # Cache prefix (system) is the static extraction prompt, unchanged.
    assert (llm.last_system or "").startswith("You are a claim extractor")
