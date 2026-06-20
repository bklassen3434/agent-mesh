"""Controller skill: ``scout-source`` — acquire new sources from a connector.

Resolves an ``unscouted_connector`` tension by polling one enabled connector
in-process (``mesh_agents.connector_dispatch``) and emitting a
``CreateSourceEffect`` for each *new* source — the controller's source-acquisition
path, the analog of the coordinator's scout node. It is the only skill that adds
raw material to the board; everything else (extract → resolve → synthesize) works
what is already there.

The skill **never writes**: it reads existing source hashes through ``conn`` and
returns effects; the write gateway persists them. Dedup mirrors the coordinator's
ingest node — drop any scouted paper whose ``raw_content_hash`` already exists in
the field (and within the batch). The scouted **payload** (title/abstract) is
carried on the ``Source`` so it is persisted (migration 016) and extract-source
can recover the content a round later.
"""
from __future__ import annotations

import os
from typing import Any

from mesh_db.sources import list_sources
from mesh_models.effect import CreateSourceEffect, Effect
from mesh_models.tension import Tension, TensionKind

from mesh_agents.arxiv_scout import ScoutedPaper
from mesh_agents.connector_dispatch import scout_connector
from mesh_agents.skill import register_skill

# Per-connector fetch cap for one controller scout (mirrors MESH_PIPELINE_MAX_PAPERS).
_DEFAULT_MAX = 20


def _max_results() -> int:
    return int(os.environ.get("MESH_MARKET_SCOUT_MAX", str(_DEFAULT_MAX)))


def _existing_hashes(conn: Any, field_id: str) -> set[str]:
    return {
        s.raw_content_hash for s in list_sources(conn, limit=10000, field_id=field_id)
    }


@register_skill
class ScoutSourceSkill:
    """Handle ``unscouted_connector`` tensions; poll the connector and return one
    ``CreateSourceEffect`` per new source."""

    skill_id = "scout-source"
    handles = (TensionKind.unscouted_connector,)

    async def run(
        self, conn: Any, tension: Tension, *, budget_usd: float
    ) -> list[Effect]:
        connector_id = tension.target_ref.get("connector_id")
        if not connector_id:
            return []
        config = tension.signals.get("config") or {}
        papers = await scout_connector(
            connector_id, config=config, max_results=_max_results()
        )
        if not papers:
            return []

        field_id = tension.field_id
        existing = _existing_hashes(conn, field_id)
        seen: set[str] = set()
        effects: list[Effect] = []
        for paper in papers:
            h = paper.source.raw_content_hash
            if h in existing or h in seen:
                continue  # already on the board (or a dup within this batch)
            seen.add(h)
            effects.append(
                CreateSourceEffect(
                    field_id=field_id, source=_with_payload(paper)
                )
            )
        return effects


def _with_payload(paper: ScoutedPaper) -> Any:
    """Persist the scouted title/abstract on the source so extract-source can
    recover the paper text a round later (the row otherwise drops it)."""
    source = paper.source
    source.payload = {"title": paper.title, "abstract": paper.abstract}
    return source
