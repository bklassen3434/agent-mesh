"""Phase 22d: the discovery sweep graph (plan → dispatch → finalize).

Exercised against the seeded test container with a fake A2A client (so no scout
HTTP) and a mock draft LLM (so no API key). Verifies the sweep opens capped
``discovery``-origin investigations, records a pipeline run, and cleanly no-ops
on a field with no gaps.
"""
from __future__ import annotations

import asyncio
from typing import Any

from mesh_a2a.checkpoint import open_checkpointer, thread_config
from mesh_agents.discovery import DiscoveryProposal, DiscoveryProposals
from mesh_agents.entity_resolution import ResolutionConfig
from mesh_db.connection import MeshConnection
from mesh_db.entities import create_entity
from mesh_db.investigations import list_investigations
from mesh_db.pipeline_runs import pipeline_run_exists
from mesh_models.entity import Entity, EntityType
from mesh_models.investigation import InvestigationOrigin
from mesh_pipeline.discovery import DiscoverState, build_discovery_graph


class _FakeA2AClient:
    """In-memory MeshA2AClient stand-in. No investigate skills are advertised,
    so dispatch gathers nothing — exercising the full graph without scouts."""

    async def __aenter__(self) -> _FakeA2AClient:
        return self

    async def __aexit__(self, *a: Any) -> None:
        pass

    async def discover(self, base_urls: list[str]) -> dict[str, str]:
        return {}

    def skill_map(self) -> dict[str, Any]:
        return {}


class _MockLLM:
    model = "mock-model"

    def __init__(self, result: Any) -> None:
        self._result = result

    def health_check(self) -> None:  # pragma: no cover
        return None

    def complete_with_latency(self, *a: Any, **kw: Any) -> Any:  # pragma: no cover
        raise NotImplementedError

    def complete_with_usage(self, *a: Any, **kw: Any) -> Any:
        from mesh_llm.usage import LLMUsage

        return self._result, 5, LLMUsage(input_tokens=10, output_tokens=5)


def _run_sweep(conn: MeshConnection, draft_llm: Any, run_id: str) -> DiscoverState:
    async def _go() -> DiscoverState:
        client: Any = _FakeA2AClient()
        graph = build_discovery_graph(
            client,
            conn,
            draft_llm=draft_llm,
            embedder=None,
            resolution_llm=None,
            semaphore=asyncio.Semaphore(3),
            resolution_config=ResolutionConfig.from_env(),
        )
        initial: DiscoverState = {
            "run_id": run_id,
            "field_id": "ai-robotics",
            "field_slug": "ai-robotics",
            "triggered_by": "test",
            "traceparent": "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01",
            "started_at": "2026-06-13T00:00:00+00:00",
            "gaps_found": 0,
            "hypotheses_drafted": 0,
            "opened_investigation_ids": [],
            "investigations_opened": 0,
            "draft_usage": None,
            "draft_model": "",
            "dispatch": {},
            "errors": [],
            "finalized": False,
        }
        async with open_checkpointer() as saver:
            app = graph.compile(checkpointer=saver)
            final = await app.ainvoke(initial, config=thread_config(run_id))
        return final  # type: ignore[return-value]

    return asyncio.run(_go())


def test_sweep_opens_capped_discovery_investigation(tmp_db: MeshConnection) -> None:
    # An under-evidenced entity is a gap; the mock LLM drafts a hypothesis for it.
    ent = create_entity(tmp_db, Entity(canonical_name="ObscureNet", type=EntityType.model))
    gap_id = f"under_evidenced_entity:{ent.id}"
    result = DiscoveryProposals(
        proposals=[
            DiscoveryProposal(
                gap_id=gap_id,
                hypothesis="Search arxiv for ObscureNet evaluations",
                suggested_source_types=["arxiv"],
                rationale="closes the under-evidenced gap",
            )
        ]
    )
    final = _run_sweep(tmp_db, _MockLLM(result), "run-discovery-1")

    assert final["finalized"] is True
    assert final["investigations_opened"] == 1
    opened = list_investigations(tmp_db, origin=InvestigationOrigin.discovery)
    assert len(opened) == 1
    assert opened[0].target_entity_id == ent.id
    assert opened[0].suggested_source_types == ["arxiv"]
    # finalize recorded the run (idempotency guard key).
    assert pipeline_run_exists(tmp_db, "run-discovery-1")


def test_sweep_noops_on_field_without_gaps(tmp_db: MeshConnection) -> None:
    # Empty field → no gaps → no investigations, but the run still finalizes.
    final = _run_sweep(tmp_db, _MockLLM(DiscoveryProposals()), "run-discovery-empty")
    assert final["investigations_opened"] == 0
    assert list_investigations(tmp_db, origin=InvestigationOrigin.discovery) == []
    assert pipeline_run_exists(tmp_db, "run-discovery-empty")
