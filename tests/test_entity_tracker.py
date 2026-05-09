from __future__ import annotations

import asyncio

import duckdb
from mesh_agents.entity_tracker import EntityTrackerAgent, EntityTrackerInput
from mesh_db.entities import create_entity, get_entity_by_id
from mesh_models.entity import Entity, EntityType


class TestEntityTrackerAgent:
    def test_creates_new_entity_when_not_found(self, tmp_db: duckdb.DuckDBPyConnection) -> None:
        agent = EntityTrackerAgent(db_conn=tmp_db)
        output = asyncio.run(agent.run(EntityTrackerInput(names=["GPT-4"])))
        assert "GPT-4" in output.resolved
        assert output.created_count == 1

    def test_finds_existing_by_canonical_name(self, tmp_db: duckdb.DuckDBPyConnection) -> None:
        entity = Entity(canonical_name="BERT", type=EntityType.model)
        create_entity(tmp_db, entity)

        agent = EntityTrackerAgent(db_conn=tmp_db)
        output = asyncio.run(agent.run(EntityTrackerInput(names=["BERT"])))
        assert output.resolved["BERT"] == entity.id
        assert output.created_count == 0

    def test_finds_existing_by_alias(self, tmp_db: duckdb.DuckDBPyConnection) -> None:
        entity = Entity(
            canonical_name="BERT",
            type=EntityType.model,
            aliases=["bert-base", "bert-large"],
        )
        create_entity(tmp_db, entity)

        agent = EntityTrackerAgent(db_conn=tmp_db)
        output = asyncio.run(agent.run(EntityTrackerInput(names=["bert-base"])))
        assert output.resolved["bert-base"] == entity.id
        assert output.created_count == 0

    def test_case_insensitive_canonical_match(self, tmp_db: duckdb.DuckDBPyConnection) -> None:
        entity = Entity(canonical_name="GPT-4", type=EntityType.model)
        create_entity(tmp_db, entity)

        agent = EntityTrackerAgent(db_conn=tmp_db)
        output = asyncio.run(agent.run(EntityTrackerInput(names=["gpt-4"])))
        assert output.resolved["gpt-4"] == entity.id
        assert output.created_count == 0

    def test_case_insensitive_alias_match(self, tmp_db: duckdb.DuckDBPyConnection) -> None:
        entity = Entity(
            canonical_name="LLaMA",
            type=EntityType.model,
            aliases=["Llama", "llama-2"],
        )
        create_entity(tmp_db, entity)

        agent = EntityTrackerAgent(db_conn=tmp_db)
        output = asyncio.run(agent.run(EntityTrackerInput(names=["LLAMA"])))
        assert output.resolved["LLAMA"] == entity.id

    def test_type_hint_applied_to_new_entity(self, tmp_db: duckdb.DuckDBPyConnection) -> None:
        agent = EntityTrackerAgent(db_conn=tmp_db)
        output = asyncio.run(
            agent.run(
                EntityTrackerInput(
                    names=["MMLU"],
                    type_hints={"MMLU": EntityType.benchmark},
                )
            )
        )
        entity = get_entity_by_id(tmp_db, output.resolved["MMLU"])
        assert entity is not None
        assert entity.type == EntityType.benchmark

    def test_default_type_concept(self, tmp_db: duckdb.DuckDBPyConnection) -> None:
        agent = EntityTrackerAgent(db_conn=tmp_db)
        output = asyncio.run(agent.run(EntityTrackerInput(names=["SomeNewThing"])))
        entity = get_entity_by_id(tmp_db, output.resolved["SomeNewThing"])
        assert entity is not None
        assert entity.type == EntityType.concept

    def test_multiple_names_batch(self, tmp_db: duckdb.DuckDBPyConnection) -> None:
        agent = EntityTrackerAgent(db_conn=tmp_db)
        output = asyncio.run(agent.run(EntityTrackerInput(names=["Alpha", "Beta", "Gamma"])))
        assert len(output.resolved) == 3
        assert output.created_count == 3

    def test_no_duplicates_on_second_run(self, tmp_db: duckdb.DuckDBPyConnection) -> None:
        agent = EntityTrackerAgent(db_conn=tmp_db)
        out1 = asyncio.run(agent.run(EntityTrackerInput(names=["GPT-4"])))
        out2 = asyncio.run(agent.run(EntityTrackerInput(names=["GPT-4"])))
        assert out1.resolved["GPT-4"] == out2.resolved["GPT-4"]
        assert out2.created_count == 0
