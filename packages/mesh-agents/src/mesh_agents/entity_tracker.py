from __future__ import annotations

from typing import Any

import duckdb
from mesh_db.entities import create_entity
from mesh_models.entity import Entity, EntityType
from pydantic import BaseModel

from mesh_agents.base import BaseAgent


class EntityTrackerInput(BaseModel):
    names: list[str]
    type_hints: dict[str, EntityType] | None = None


class EntityTrackerOutput(BaseModel):
    resolved: dict[str, str]  # name → entity_id
    created_count: int = 0


def _find_entity_by_name(conn: duckdb.DuckDBPyConnection, name: str) -> str | None:
    """Return entity_id if found by canonical_name or alias (case-insensitive)."""
    row = conn.execute(
        """
        SELECT id FROM entities
        WHERE lower(canonical_name) = lower(?)
           OR len(list_filter(aliases, x -> lower(x) = lower(?))) > 0
        LIMIT 1
        """,
        [name, name],
    ).fetchone()
    return str(row[0]) if row else None


class EntityTrackerAgent(BaseAgent):
    name = "entity_tracker"

    def __init__(self, llm: Any | None = None, db_conn: Any | None = None) -> None:
        super().__init__(llm=llm, db_conn=db_conn)

    async def run(self, input: BaseModel) -> EntityTrackerOutput:
        assert isinstance(input, EntityTrackerInput)
        assert isinstance(self.db_conn, duckdb.DuckDBPyConnection)

        resolved: dict[str, str] = {}
        created_count = 0

        for name in input.names:
            existing_id = _find_entity_by_name(self.db_conn, name)
            if existing_id is not None:
                resolved[name] = existing_id
                continue

            entity_type = EntityType.concept
            if input.type_hints:
                entity_type = input.type_hints.get(name, EntityType.concept)

            entity = Entity(canonical_name=name, type=entity_type)
            create_entity(self.db_conn, entity)
            resolved[name] = entity.id
            created_count += 1

        return EntityTrackerOutput(resolved=resolved, created_count=created_count)
