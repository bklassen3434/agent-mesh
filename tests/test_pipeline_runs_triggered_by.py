"""Phase 6a.2 unit test: pipeline_runs.triggered_by round-trips through
the DB layer and defaults sensibly for legacy callers.
"""
from __future__ import annotations

from mesh_db.connection import MeshConnection
from mesh_db.pipeline_runs import PipelineRun, create_pipeline_run, list_pipeline_runs


def test_triggered_by_round_trip(tmp_db: MeshConnection) -> None:
    run = PipelineRun(triggered_by="scheduled")
    create_pipeline_run(tmp_db, run)
    fetched = list_pipeline_runs(tmp_db, limit=1)
    assert fetched[0].triggered_by == "scheduled"


def test_triggered_by_defaults_to_manual(tmp_db: MeshConnection) -> None:
    # Construct without specifying triggered_by — should fall back to manual.
    run = PipelineRun()
    create_pipeline_run(tmp_db, run)
    fetched = list_pipeline_runs(tmp_db, limit=1)
    assert fetched[0].triggered_by == "manual"


def test_pipeline_run_exists(tmp_db: MeshConnection) -> None:
    from mesh_db.pipeline_runs import pipeline_run_exists

    run = PipelineRun()
    assert not pipeline_run_exists(tmp_db, run.id)
    create_pipeline_run(tmp_db, run)
    assert pipeline_run_exists(tmp_db, run.id)
    assert not pipeline_run_exists(tmp_db, "nonexistent-id")
