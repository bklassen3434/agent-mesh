"""One-time DuckDB → Postgres knowledge-store data migration (Phase 12c).

Reads every row from the DuckDB file (``MESH_DB_PATH``) and writes it into the
Postgres ``knowledge`` schema (``MESH_PG_URL`` / ``LANGGRAPH_POSTGRES_URL``),
preserving primary keys and all foreign keys so provenance links survive.

Idempotent: truncate-and-reload. A failed run can simply be re-run. Inserts are
ordered to satisfy FK constraints; ``claims.superseded_by_claim_id`` (a
self-reference) is filled in a second pass so claim insert order is irrelevant.

Run after ``init-pg-db``:
    uv run python -m mesh_db.duckdb_to_pg
    uv run mesh.cli migrate-duckdb-to-pg

The schema (``knowledge.*``) must already exist (see ``mesh_db.pg_migrations``).
"""
from __future__ import annotations

import os

import duckdb
import psycopg

from mesh_db.pg_connection import get_pg_connection

# Tables in FK-safe insert order. `claims` is loaded with a NULL
# superseded_by_claim_id (filled in pass 2). Column lists are authoritative
# against migrations_pg/002,003.
TABLES: list[tuple[str, list[str]]] = [
    ("entities", [
        "id", "canonical_name", "aliases", "type", "attributes",
        "created_at", "last_seen_at", "name_embedding",
    ]),
    ("sources", [
        "id", "type", "url", "author", "published_at", "fetched_at",
        "raw_content_hash", "reliability_prior",
    ]),
    ("claims", [
        "id", "predicate", "subject_entity_id", "object", "source_id",
        "extracted_at", "extracted_by_agent", "raw_excerpt", "status",
        "confidence", "failure_mode",  # superseded_by_claim_id -> pass 2
    ]),
    ("beliefs", [
        "id", "topic", "statement", "supporting_claim_ids",
        "contradicting_claim_ids", "confidence", "last_revised_at",
        "revision_count", "is_currently_held",
    ]),
    ("belief_revisions", [
        "id", "belief_id", "previous_statement", "new_statement",
        "previous_confidence", "new_confidence", "trigger_claim_ids",
        "revised_by_agent", "revised_at", "rationale",
    ]),
    ("relationships", [
        "id", "from_entity_id", "to_entity_id", "type",
        "evidence_claim_ids", "confidence",
    ]),
    ("investigations", [
        "id", "question", "related_entity_ids", "status", "priority",
        "created_at", "resolved_at", "resolution_belief_id",
        "assigned_scout_agents", "target_entity_id", "hypothesis",
        "suggested_source_types", "opened_by_belief_id",
        "pipeline_runs_attempted", "collected_claim_ids",
    ]),
    ("pipeline_runs", [
        "id", "started_at", "finished_at", "papers_scouted",
        "sources_inserted", "claims_inserted", "entities_created",
        "beliefs_created", "beliefs_revised", "avg_extraction_latency_ms",
        "errors", "run_type", "triggered_by",
    ]),
    ("llm_usage", [
        "id", "run_id", "agent_name", "skill_id", "model", "input_tokens",
        "output_tokens", "cache_read_tokens", "cache_creation_tokens",
        "estimated_cost_usd", "created_at",
    ]),
    ("processed_items", [
        "source_type", "external_id", "content_hash",
        "first_seen_at", "last_seen_at",
    ]),
]

# Columns that are JSON in DuckDB (returned as str) and JSONB in Postgres.
JSON_COLS = {"attributes", "object", "errors"}
# pgvector column(s): DuckDB FLOAT[] (list|None) -> '[..]'::vector.
VECTOR_COLS = {"name_embedding"}

# All knowledge tables (for truncate), reverse-dep order not needed with CASCADE.
ALL_TABLES = [t for t, _ in TABLES]


def _select_sql(table: str, cols: list[str]) -> str:
    parts = [f"{c}::VARCHAR AS {c}" if c in JSON_COLS else c for c in cols]
    return f"SELECT {', '.join(parts)} FROM {table}"


def _insert_sql(table: str, cols: list[str]) -> str:
    ph = []
    for c in cols:
        if c in JSON_COLS:
            ph.append("%s::jsonb")
        elif c in VECTOR_COLS:
            ph.append("%s::vector")
        else:
            ph.append("%s")
    return (
        f"INSERT INTO knowledge.{table} ({', '.join(cols)}) "
        f"VALUES ({', '.join(ph)})"
    )


def _vec(value: object) -> str | None:
    if value is None:
        return None
    assert isinstance(value, (list, tuple))
    return "[" + ",".join(str(float(x)) for x in value) + "]"


def _transform(cols: list[str], row: tuple[object, ...]) -> list[object]:
    return [
        _vec(v) if cols[i] in VECTOR_COLS else v
        for i, v in enumerate(row)
    ]


def migrate(*, duck: duckdb.DuckDBPyConnection, pg: psycopg.Connection) -> dict[str, int]:
    """Truncate the knowledge tables and reload them from DuckDB. Returns the
    per-table row counts written."""
    counts: dict[str, int] = {}
    with pg.transaction():
        pg.execute(
            "TRUNCATE "
            + ", ".join(f"knowledge.{t}" for t in ALL_TABLES)
            + " CASCADE"
        )
        for table, cols in TABLES:
            rows = duck.execute(_select_sql(table, cols)).fetchall()
            if rows:
                pg.cursor().executemany(
                    _insert_sql(table, cols),
                    [_transform(cols, r) for r in rows],
                )
            counts[table] = len(rows)

        # pass 2: claims self-reference
        superseded = duck.execute(
            "SELECT id, superseded_by_claim_id FROM claims "
            "WHERE superseded_by_claim_id IS NOT NULL"
        ).fetchall()
        if superseded:
            pg.cursor().executemany(
                "UPDATE knowledge.claims SET superseded_by_claim_id = %s "
                "WHERE id = %s",
                [(sup, cid) for cid, sup in superseded],
            )
        counts["_superseded_links"] = len(superseded)
    return counts


def _scalar(row: tuple[object, ...] | None) -> object:
    """First column of a single-row result; asserts a row came back."""
    assert row is not None, "expected a row"
    return row[0]


def verify(*, duck: duckdb.DuckDBPyConnection, pg: psycopg.Connection) -> list[str]:
    """Post-migration checks. Returns a list of problems (empty == all good)."""
    problems: list[str] = []

    # 1. per-table row-count parity
    for table, _ in TABLES:
        d = _scalar(duck.execute(f"SELECT count(*) FROM {table}").fetchone())
        p = _scalar(pg.execute(f"SELECT count(*) FROM knowledge.{table}").fetchone())
        if d != p:
            problems.append(f"row count mismatch {table}: duckdb={d} pg={p}")

    # 2. provenance: no orphaned claims (FK guarantees, but assert anyway)
    orphan_claims = _scalar(pg.execute(
        "SELECT count(*) FROM knowledge.claims c "
        "LEFT JOIN knowledge.sources s ON s.id=c.source_id WHERE s.id IS NULL"
    ).fetchone())
    if orphan_claims:
        problems.append(f"{orphan_claims} claims with no source")

    # 3. provenance: belief/relationship claim-id arrays resolve to real claims
    dangling_belief = _scalar(pg.execute(
        "SELECT count(*) FROM ("
        "  SELECT unnest(supporting_claim_ids||contradicting_claim_ids) AS cid "
        "  FROM knowledge.beliefs) x "
        "LEFT JOIN knowledge.claims c ON c.id=x.cid WHERE c.id IS NULL"
    ).fetchone())
    if dangling_belief:
        problems.append(f"{dangling_belief} belief claim-id refs not in claims")
    dangling_rel = _scalar(pg.execute(
        "SELECT count(*) FROM ("
        "  SELECT unnest(evidence_claim_ids) AS cid FROM knowledge.relationships) x "
        "LEFT JOIN knowledge.claims c ON c.id=x.cid WHERE c.id IS NULL"
    ).fetchone())
    if dangling_rel:
        problems.append(f"{dangling_rel} relationship claim-id refs not in claims")

    # 4. synthetic Skeptic source rows preserved
    d_sk = _scalar(duck.execute(
        "SELECT count(*) FROM sources WHERE type='skeptic'"
    ).fetchone())
    p_sk = _scalar(pg.execute(
        "SELECT count(*) FROM knowledge.sources WHERE type='skeptic'"
    ).fetchone())
    if d_sk != p_sk:
        problems.append(f"skeptic source rows mismatch: duckdb={d_sk} pg={p_sk}")

    return problems


def run() -> None:
    # Read the *source* DuckDB file directly — get_connection now returns the
    # Postgres (destination) store, not DuckDB.
    duck_path = os.environ.get("MESH_DB_PATH", "./data/mesh.db")
    duck = duckdb.connect(duck_path, read_only=True)
    try:
        with get_pg_connection() as pg:
            counts = migrate(duck=duck, pg=pg)
            print("migrated rows:")
            for k, v in counts.items():
                print(f"  {k:24s} {v}")
            problems = verify(duck=duck, pg=pg)
            if problems:
                print("\nVERIFICATION FAILED:")
                for p in problems:
                    print(f"  - {p}")
                raise SystemExit(1)
            print("\nverification OK — row counts match, provenance intact.")
    finally:
        duck.close()


if __name__ == "__main__":
    run()
