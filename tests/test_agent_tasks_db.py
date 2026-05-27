"""Phase 6b unit tests for the agent_tasks + agent_task_events DAL."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import duckdb
from mesh_db.agent_tasks import (
    EVENT_COMPLETED,
    EVENT_CREATED,
    EVENT_FAILED,
    EVENT_HEARTBEAT,
    EVENT_STARTED,
    ORPHANED_REASON,
    TASK_STATUS_COMPLETED,
    TASK_STATUS_FAILED,
    TASK_STATUS_PENDING,
    count_tasks_by_status,
    create_task,
    get_task,
    list_recent_failures,
    mark_completed,
    mark_failed,
    mark_heartbeat,
    mark_running,
    sweep_orphaned_tasks,
)


def _events(conn: duckdb.DuckDBPyConnection, task_id: str) -> list[str]:
    rows = conn.execute(
        "SELECT event_type FROM agent_task_events WHERE task_id = ? ORDER BY timestamp",
        [task_id],
    ).fetchall()
    return [r[0] for r in rows]


def test_create_task_writes_pending_row_and_event(
    tmp_db: duckdb.DuckDBPyConnection,
) -> None:
    create_task(
        tmp_db,
        task_id="t1",
        skill_id="extract_claims",
        agent_url="http://x:8002",
        input_payload={"a": 1},
        dispatched_by_run_id="run-7",
    )
    task = get_task(tmp_db, "t1")
    assert task is not None
    assert task.status == TASK_STATUS_PENDING
    assert task.dispatched_by_run_id == "run-7"
    assert task.input == {"a": 1}
    assert _events(tmp_db, "t1") == [EVENT_CREATED]


def test_lifecycle_writes_full_event_trail(
    tmp_db: duckdb.DuckDBPyConnection,
) -> None:
    create_task(
        tmp_db, task_id="t2", skill_id="x", agent_url="u", input_payload={}
    )
    mark_running(tmp_db, "t2")
    mark_heartbeat(tmp_db, "t2")
    mark_heartbeat(tmp_db, "t2")
    mark_completed(tmp_db, "t2", {"ok": True})
    assert _events(tmp_db, "t2") == [
        EVENT_CREATED,
        EVENT_STARTED,
        EVENT_HEARTBEAT,
        EVENT_HEARTBEAT,
        EVENT_COMPLETED,
    ]
    task = get_task(tmp_db, "t2")
    assert task is not None
    assert task.status == TASK_STATUS_COMPLETED
    assert task.output == {"ok": True}


def test_mark_failed_records_error_and_event(
    tmp_db: duckdb.DuckDBPyConnection,
) -> None:
    create_task(
        tmp_db, task_id="t3", skill_id="x", agent_url="u", input_payload={}
    )
    mark_failed(tmp_db, "t3", "boom")
    task = get_task(tmp_db, "t3")
    assert task is not None
    assert task.status == TASK_STATUS_FAILED
    assert task.error == "boom"
    assert _events(tmp_db, "t3")[-1] == EVENT_FAILED


def test_orphan_sweep_only_touches_stale_pending_or_running(
    tmp_db: duckdb.DuckDBPyConnection,
) -> None:
    # A fresh pending task should NOT be touched if threshold > 0.
    create_task(
        tmp_db, task_id="fresh", skill_id="x", agent_url="u", input_payload={}
    )
    # An already-completed task is not eligible for orphan sweep.
    create_task(
        tmp_db, task_id="done", skill_id="x", agent_url="u", input_payload={}
    )
    mark_completed(tmp_db, "done", {})

    # threshold_seconds=-1 means "any updated_at < now+1s qualifies",
    # which catches the still-pending row but skips the completed one
    # because the WHERE filter excludes status=completed.
    n = sweep_orphaned_tasks(tmp_db, threshold_seconds=-1)
    assert n == 1
    fresh = get_task(tmp_db, "fresh")
    assert fresh is not None
    assert fresh.status == TASK_STATUS_FAILED
    assert fresh.error == ORPHANED_REASON
    done = get_task(tmp_db, "done")
    assert done is not None
    assert done.status == TASK_STATUS_COMPLETED


def test_orphan_sweep_respects_threshold_window(
    tmp_db: duckdb.DuckDBPyConnection,
) -> None:
    create_task(
        tmp_db, task_id="recent", skill_id="x", agent_url="u", input_payload={}
    )
    # Threshold of 1h — the row was created seconds ago, so nothing to do.
    n = sweep_orphaned_tasks(tmp_db, threshold_seconds=3600)
    assert n == 0
    # Pass a fake `now` 2h in the future — same row is now over-threshold.
    future = datetime.now(UTC) + timedelta(hours=2)
    n = sweep_orphaned_tasks(tmp_db, threshold_seconds=3600, now=future)
    assert n == 1


def test_count_and_list_recent_failures(
    tmp_db: duckdb.DuckDBPyConnection,
) -> None:
    for i in range(3):
        tid = f"t{i}"
        create_task(
            tmp_db, task_id=tid, skill_id="x", agent_url="u", input_payload={}
        )
        if i == 0:
            mark_completed(tmp_db, tid, {})
        elif i == 1:
            mark_failed(tmp_db, tid, "uhoh")
    counts = count_tasks_by_status(tmp_db)
    assert counts == {
        TASK_STATUS_COMPLETED: 1,
        TASK_STATUS_FAILED: 1,
        TASK_STATUS_PENDING: 1,
    }
    failures = list_recent_failures(tmp_db, limit=5)
    assert [f.id for f in failures] == ["t1"]
    # Ordering: most recent failure first.
    mark_failed(tmp_db, "t2", "second")
    failures = list_recent_failures(tmp_db, limit=5)
    assert [f.id for f in failures] == ["t2", "t1"]


def test_running_status_qualifies_for_orphan_sweep(
    tmp_db: duckdb.DuckDBPyConnection,
) -> None:
    create_task(
        tmp_db, task_id="r1", skill_id="x", agent_url="u", input_payload={}
    )
    mark_running(tmp_db, "r1")
    n = sweep_orphaned_tasks(tmp_db, threshold_seconds=-1)
    assert n == 1
    task = get_task(tmp_db, "r1")
    assert task is not None
    assert task.status == TASK_STATUS_FAILED
