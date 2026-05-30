"""Offline tests for the DuckDB->Postgres migration's pure helpers (Phase 12c).

The live migration (truncate/reload/verify) needs both a DuckDB file and a
Postgres server, so it is exercised by the 12c verification harness, not the
offline suite. Here we pin the SQL generation + value transforms that decide
correctness of every row.
"""
from __future__ import annotations

from mesh_db.duckdb_to_pg import (
    TABLES,
    _insert_sql,
    _select_sql,
    _transform,
    _vec,
)


def _cols(name: str) -> list[str]:
    return next(cols for t, cols in TABLES if t == name)


def test_select_casts_json_columns_to_varchar() -> None:
    sql = _select_sql("claims", _cols("claims"))
    assert "object::VARCHAR AS object" in sql
    assert "FROM claims" in sql
    # non-json columns are selected plainly
    assert "predicate" in sql and "predicate::VARCHAR" not in sql


def test_insert_casts_json_and_vector_columns() -> None:
    sql = _insert_sql("entities", _cols("entities"))
    assert "INTO knowledge.entities" in sql
    # one placeholder per column
    assert sql.count("%s") == len(_cols("entities"))
    # attributes (json) -> ::jsonb, name_embedding (vector) -> ::vector
    assert "%s::jsonb" in sql
    assert "%s::vector" in sql


def test_claims_insert_excludes_self_referential_fk() -> None:
    # superseded_by_claim_id is filled in a second pass, never in the insert
    assert "superseded_by_claim_id" not in _cols("claims")


def test_vec_formats_and_passes_through_none() -> None:
    assert _vec(None) is None
    assert _vec([1, 2.5, 3]) == "[1.0,2.5,3.0]"


def test_transform_only_touches_vector_columns() -> None:
    cols = _cols("entities")
    row = tuple(f"v{i}" if c != "name_embedding" else [0.5, 0.5]
                for i, c in enumerate(cols))
    out = _transform(cols, row)
    emb_idx = cols.index("name_embedding")
    assert out[emb_idx] == "[0.5,0.5]"
    # every other value untouched
    for i, c in enumerate(cols):
        if c != "name_embedding":
            assert out[i] == row[i]


def test_table_order_is_fk_safe() -> None:
    order = [t for t, _ in TABLES]
    # referenced tables come before referencing ones
    assert order.index("entities") < order.index("claims")
    assert order.index("sources") < order.index("claims")
    assert order.index("beliefs") < order.index("belief_revisions")
    assert order.index("beliefs") < order.index("investigations")
    assert order.index("entities") < order.index("relationships")
