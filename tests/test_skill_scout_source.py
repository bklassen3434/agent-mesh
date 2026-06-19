"""Market skill: ``scout-source`` — source acquisition.

Polls an enabled connector in-process (the scout handler is stubbed so no
network), dedups against the board by content hash, and emits CreateSourceEffect
carrying the scouted payload. The agenda surfaces one ``unscouted_connector``
tension per enabled connector, and the gateway persists the source + payload so
extract-source can recover the title/abstract a round later (migration 016).
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

import pytest
from mesh_agents.agenda import scout_tensions
from mesh_agents.arxiv_scout import ScoutedPaper
from mesh_agents.skill import get_skill, load_builtin_skills
from mesh_db.connection import MeshConnection
from mesh_db.connectors import enable_connector
from mesh_db.effects import apply_effects
from mesh_db.sources import create_source, get_source_payload, list_sources
from mesh_models.effect import CreateSourceEffect
from mesh_models.field import DEFAULT_FIELD_ID
from mesh_models.source import Source, SourceType
from mesh_models.tension import Tension, TensionKind


def _paper(arxiv_id: str, h: str) -> ScoutedPaper:
    return ScoutedPaper(
        source=Source(
            type=SourceType.arxiv,
            url=f"https://arxiv.org/abs/{arxiv_id}",
            published_at=datetime(2024, 1, 1),
            raw_content_hash=h,
        ),
        title=f"Paper {arxiv_id}",
        abstract="We present a new model that achieves SOTA.",
        arxiv_id=arxiv_id,
    )


def _tension() -> Tension:
    return Tension(
        id=f"{TensionKind.unscouted_connector.value}:arxiv",
        field_id=DEFAULT_FIELD_ID,
        kind=TensionKind.unscouted_connector,
        subject="arxiv",
        rationale="poll arxiv",
        value=0.6,
        est_cost_usd=0.001,
        handler_skill="scout-source",
        target_ref={"connector_id": "arxiv"},
        signals={"config": {"categories": ["cs.LG"]}},
    )


def _run(skill: Any, conn: Any, tension: Tension) -> list[Any]:
    return asyncio.run(skill.run(conn, tension, budget_usd=0.001))


def test_emits_create_source_effects_and_dedups(
    tmp_db: MeshConnection, monkeypatch: pytest.MonkeyPatch
) -> None:
    # One source already on the board; the scout returns it again plus a new one.
    create_source(
        tmp_db,
        Source(
            type=SourceType.arxiv,
            url="https://arxiv.org/abs/old",
            published_at=datetime(2024, 1, 1),
            raw_content_hash="hash-old",
        ),
        field_id=DEFAULT_FIELD_ID,
    )

    async def fake_scout(connector_id: str, **kw: Any) -> list[ScoutedPaper]:
        assert connector_id == "arxiv"
        assert kw["config"] == {"categories": ["cs.LG"]}
        return [_paper("2401.1", "hash-old"), _paper("2401.2", "hash-new")]

    monkeypatch.setattr(
        "mesh_agents.skills.scout_source.scout_connector", fake_scout
    )
    load_builtin_skills()
    skill = get_skill("scout-source")
    assert skill is not None

    effects = _run(skill, tmp_db, _tension())
    # Only the unseen source becomes an effect (the existing hash is dropped).
    assert len(effects) == 1
    assert isinstance(effects[0], CreateSourceEffect)
    assert effects[0].source.raw_content_hash == "hash-new"
    assert effects[0].source.payload == {
        "title": "Paper 2401.2",
        "abstract": "We present a new model that achieves SOTA.",
    }

    # The skill wrote nothing; the gateway persists the source + its payload.
    report = apply_effects(tmp_db, effects)
    assert report.sources_created == 1
    new = [
        s
        for s in list_sources(tmp_db, limit=100, field_id=DEFAULT_FIELD_ID)
        if s.raw_content_hash == "hash-new"
    ]
    assert len(new) == 1
    assert get_source_payload(tmp_db, new[0].id) == {
        "title": "Paper 2401.2",
        "abstract": "We present a new model that achieves SOTA.",
    }


def test_scout_tensions_cover_each_enabled_connector(tmp_db: MeshConnection) -> None:
    enable_connector(tmp_db, DEFAULT_FIELD_ID, "arxiv", config={"categories": ["cs.LG"]})
    tensions = scout_tensions(tmp_db, DEFAULT_FIELD_ID)
    assert all(t.kind == TensionKind.unscouted_connector for t in tensions)
    arxiv = next(t for t in tensions if t.target_ref == {"connector_id": "arxiv"})
    assert arxiv.handler_skill == "scout-source"
    assert arxiv.signals["config"] == {"categories": ["cs.LG"]}
    # Every tension targets a connector that has an in-process handler.
    from mesh_agents.connector_dispatch import has_connector

    assert all(has_connector(t.target_ref["connector_id"]) for t in tensions)
