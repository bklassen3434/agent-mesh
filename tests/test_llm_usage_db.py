"""Phase 11a: llm_usage ledger DB layer."""
from __future__ import annotations

import duckdb
from mesh_db.llm_usage import (
    LLMUsageRecord,
    aggregate_usage_by_skill,
    create_llm_usage,
    list_llm_usage,
)


def _rec(
    run_id: str,
    skill: str,
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    estimated_cost_usd: float = 0.0,
) -> LLMUsageRecord:
    return LLMUsageRecord(
        run_id=run_id,
        skill_id=skill,
        agent_name="claim_extractor",
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        estimated_cost_usd=estimated_cost_usd,
    )


def test_create_and_list_round_trip(tmp_db: duckdb.DuckDBPyConnection) -> None:
    rec = LLMUsageRecord(
        run_id="run-1",
        agent_name="claim_extractor",
        skill_id="extract_claims",
        model="claude-haiku-4-5",
        input_tokens=100,
        output_tokens=50,
        cache_read_tokens=10,
        cache_creation_tokens=5,
        estimated_cost_usd=0.0012,
    )
    create_llm_usage(tmp_db, rec)
    rows = list_llm_usage(tmp_db, "run-1")
    assert len(rows) == 1
    assert rows[0].input_tokens == 100
    assert rows[0].cache_read_tokens == 10
    assert rows[0].model == "claude-haiku-4-5"
    assert rows[0].estimated_cost_usd == 0.0012


def test_aggregate_sums_per_skill(tmp_db: duckdb.DuckDBPyConnection) -> None:
    create_llm_usage(
        tmp_db, _rec("run-1", "extract_claims", input_tokens=100, output_tokens=20,
                     estimated_cost_usd=0.001)
    )
    create_llm_usage(
        tmp_db, _rec("run-1", "extract_claims", input_tokens=200, output_tokens=40,
                     estimated_cost_usd=0.002)
    )
    create_llm_usage(
        tmp_db, _rec("run-1", "challenge_belief", input_tokens=50,
                     estimated_cost_usd=0.005)
    )

    totals = aggregate_usage_by_skill(tmp_db, "run-1")
    by_skill = {t.skill_id: t for t in totals}

    assert by_skill["extract_claims"].calls == 2
    assert by_skill["extract_claims"].input_tokens == 300
    assert by_skill["extract_claims"].output_tokens == 60
    assert by_skill["challenge_belief"].calls == 1
    # ordered by cost descending — challenge_belief (0.005) before extract (0.003)
    assert totals[0].skill_id == "challenge_belief"


def test_aggregate_scopes_to_run(tmp_db: duckdb.DuckDBPyConnection) -> None:
    create_llm_usage(tmp_db, _rec("run-1", "extract_claims", input_tokens=100))
    create_llm_usage(tmp_db, _rec("run-2", "extract_claims", input_tokens=999))
    totals = aggregate_usage_by_skill(tmp_db, "run-1")
    assert len(totals) == 1
    assert totals[0].input_tokens == 100


def test_empty_run_returns_no_rows(tmp_db: duckdb.DuckDBPyConnection) -> None:
    assert aggregate_usage_by_skill(tmp_db, "nope") == []
    assert list_llm_usage(tmp_db, "nope") == []
