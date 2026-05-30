from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

import duckdb
from pydantic import BaseModel, Field


class PipelineError(BaseModel):
    paper_id: str
    error_type: str
    error_message: str


class PipelineRun(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    finished_at: datetime | None = None
    run_type: str = "pipeline"  # "pipeline" | "skeptic_sweep" | future job types
    triggered_by: str = "manual"  # "manual" | "scheduled"
    papers_scouted: int = 0
    sources_inserted: int = 0
    claims_inserted: int = 0
    entities_created: int = 0
    beliefs_created: int = 0
    beliefs_revised: int = 0
    avg_extraction_latency_ms: int = 0
    errors: list[PipelineError] = Field(default_factory=list)


def create_pipeline_run(conn: duckdb.DuckDBPyConnection, model: PipelineRun) -> PipelineRun:
    conn.execute(
        """
        INSERT INTO pipeline_runs
            (id, started_at, finished_at, run_type, triggered_by, papers_scouted,
             sources_inserted, claims_inserted, entities_created, beliefs_created,
             beliefs_revised, avg_extraction_latency_ms, errors)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            model.id,
            model.started_at,
            model.finished_at,
            model.run_type,
            model.triggered_by,
            model.papers_scouted,
            model.sources_inserted,
            model.claims_inserted,
            model.entities_created,
            model.beliefs_created,
            model.beliefs_revised,
            model.avg_extraction_latency_ms,
            json.dumps([e.model_dump() for e in model.errors]),
        ],
    )
    return model


def pipeline_run_exists(conn: duckdb.DuckDBPyConnection, run_id: str) -> bool:
    """True if a pipeline_runs row with this id already exists.

    Used by the LangGraph finalize nodes to stay idempotent: a checkpointed
    graph can re-tick the final superstep, and the run-row write (plus the
    llm_usage ledger writes) must not be duplicated on replay.
    """
    row = conn.execute(
        "SELECT 1 FROM pipeline_runs WHERE id = ? LIMIT 1", [run_id]
    ).fetchone()
    return row is not None


def list_pipeline_runs(
    conn: duckdb.DuckDBPyConnection,
    limit: int = 10,
    run_type: str | None = None,
) -> list[PipelineRun]:
    where = ""
    params: list[Any] = []
    if run_type is not None:
        where = " WHERE run_type = ?"
        params.append(run_type)
    params.append(limit)
    rows = conn.execute(
        f"""
        SELECT id, started_at, finished_at, run_type, triggered_by, papers_scouted,
               sources_inserted, claims_inserted, entities_created, beliefs_created,
               beliefs_revised, avg_extraction_latency_ms, errors
        FROM pipeline_runs{where}
        ORDER BY started_at DESC LIMIT ?
        """,
        params,
    ).fetchall()
    return [_row_to_run(r) for r in rows]


def _row_to_run(row: tuple[Any, ...]) -> PipelineRun:
    (
        id_, started_at, finished_at, run_type, triggered_by, papers_scouted,
        sources_inserted, claims_inserted, entities_created, beliefs_created,
        beliefs_revised, avg_latency, errors_raw,
    ) = row[:13]

    errors_data: list[Any] = (
        json.loads(errors_raw) if isinstance(errors_raw, str) else (errors_raw or [])
    )
    errors = [PipelineError(**e) for e in errors_data]

    def _dt(val: Any) -> datetime:
        return val if isinstance(val, datetime) else datetime.fromisoformat(str(val))

    return PipelineRun(
        id=id_,
        started_at=_dt(started_at),
        finished_at=None if finished_at is None else _dt(finished_at),
        run_type=str(run_type) if run_type is not None else "pipeline",
        triggered_by=str(triggered_by) if triggered_by is not None else "manual",
        papers_scouted=int(papers_scouted),
        sources_inserted=int(sources_inserted),
        claims_inserted=int(claims_inserted),
        entities_created=int(entities_created),
        beliefs_created=int(beliefs_created),
        beliefs_revised=int(beliefs_revised),
        avg_extraction_latency_ms=int(avg_latency),
        errors=errors,
    )
