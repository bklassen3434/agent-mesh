"""Phase 6a.2 unit test: pipeline_runs.triggered_by round-trips through
the DB layer and defaults sensibly for legacy callers.
"""
from __future__ import annotations

import duckdb
from mesh_db.pipeline_runs import PipelineRun, create_pipeline_run, list_pipeline_runs


def test_triggered_by_round_trip(tmp_db: duckdb.DuckDBPyConnection) -> None:
    run = PipelineRun(triggered_by="scheduled")
    create_pipeline_run(tmp_db, run)
    fetched = list_pipeline_runs(tmp_db, limit=1)
    assert fetched[0].triggered_by == "scheduled"


def test_triggered_by_defaults_to_manual(tmp_db: duckdb.DuckDBPyConnection) -> None:
    # Construct without specifying triggered_by — should fall back to manual.
    run = PipelineRun()
    create_pipeline_run(tmp_db, run)
    fetched = list_pipeline_runs(tmp_db, limit=1)
    assert fetched[0].triggered_by == "manual"
