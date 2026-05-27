"""Phase 6b: verify MeshA2AClient drives the TaskRecorder lifecycle.

Doesn't spin up a real A2A server — patches the HTTP roundtrip so we can
script the agent's responses (queued -> running -> completed) and assert
the right recorder hooks fire in the right order.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from mesh_a2a.client import MeshA2AClient, SkillCallError, TaskTimeoutError
from mesh_a2a.task_recorder import NullTaskRecorder


class CapturingRecorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def record_pending(self, **kwargs: Any) -> None:
        self.calls.append(("pending", kwargs))

    def record_running(self, task_id: str) -> None:
        self.calls.append(("running", {"task_id": task_id}))

    def record_heartbeat(self, task_id: str) -> None:
        self.calls.append(("heartbeat", {"task_id": task_id}))

    def record_completed(self, task_id: str, output: dict[str, Any]) -> None:
        self.calls.append(("completed", {"task_id": task_id, "output": output}))

    def record_failed(self, task_id: str, error: str) -> None:
        self.calls.append(("failed", {"task_id": task_id, "error": error}))


def _patched_client(
    monkeypatch: pytest.MonkeyPatch,
    poll_responses: list[dict[str, Any]],
    *,
    recorder: Any | None = None,
    heartbeat_every_n: str | None = None,
) -> MeshA2AClient:
    if heartbeat_every_n is not None:
        monkeypatch.setenv("MESH_TASK_HEARTBEAT_EVERY_N", heartbeat_every_n)
    monkeypatch.setenv("MESH_TASK_POLL_INTERVAL_SECONDS", "0.001")
    client = MeshA2AClient(task_recorder=recorder)
    client._registry["test_skill"] = "http://agent"
    submit_mock = AsyncMock(return_value=("task-123", "http://agent"))
    get_mock = AsyncMock(side_effect=poll_responses)
    monkeypatch.setattr(client, "submit_task", submit_mock)
    monkeypatch.setattr(client, "get_task", get_mock)
    return client


@pytest.mark.asyncio
async def test_full_lifecycle_records_pending_running_completed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = CapturingRecorder()
    client = _patched_client(
        monkeypatch,
        poll_responses=[
            {"status": "queued"},
            {"status": "running"},
            {"status": "completed", "result": {"ok": True}},
        ],
        recorder=recorder,
        heartbeat_every_n="0",
    )
    result = await client.call_skill_blocking("test_skill", {"k": "v"})
    assert result == {"ok": True}
    event_types = [name for name, _ in recorder.calls]
    assert event_types == ["pending", "running", "completed"]
    pending_kwargs = recorder.calls[0][1]
    assert pending_kwargs["task_id"] == "task-123"
    assert pending_kwargs["skill_id"] == "test_skill"
    assert pending_kwargs["agent_url"] == "http://agent"
    assert pending_kwargs["payload"] == {"k": "v"}


@pytest.mark.asyncio
async def test_failed_task_records_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = CapturingRecorder()
    client = _patched_client(
        monkeypatch,
        poll_responses=[
            {"status": "running"},
            {"status": "failed", "error": "boom"},
        ],
        recorder=recorder,
        heartbeat_every_n="0",
    )
    with pytest.raises(SkillCallError):
        await client.call_skill_blocking("test_skill", {})
    assert [n for n, _ in recorder.calls] == ["pending", "running", "failed"]
    assert recorder.calls[-1][1]["error"] == "boom"


@pytest.mark.asyncio
async def test_timeout_records_failed_with_task_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = CapturingRecorder()
    client = _patched_client(
        monkeypatch,
        # Agent keeps reporting running — timeout=0 forces immediate timeout
        # after first poll.
        poll_responses=[{"status": "running"}] * 5,
        recorder=recorder,
        heartbeat_every_n="0",
    )
    with pytest.raises(TaskTimeoutError):
        await client.call_skill_blocking("test_skill", {}, timeout=0)
    assert [n for n, _ in recorder.calls] == ["pending", "running", "failed"]
    assert recorder.calls[-1][1]["error"] == "task_timeout"


@pytest.mark.asyncio
async def test_heartbeat_fires_every_n_polls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = CapturingRecorder()
    # heartbeat every 2 polls → 5 running polls = 2 heartbeats (polls 2 + 4)
    client = _patched_client(
        monkeypatch,
        poll_responses=[
            {"status": "running"},
            {"status": "running"},
            {"status": "running"},
            {"status": "running"},
            {"status": "completed", "result": {}},
        ],
        recorder=recorder,
        heartbeat_every_n="2",
    )
    await client.call_skill_blocking("test_skill", {})
    heartbeats = [n for n, _ in recorder.calls if n == "heartbeat"]
    assert len(heartbeats) == 2


@pytest.mark.asyncio
async def test_null_recorder_no_op_default(monkeypatch: pytest.MonkeyPatch) -> None:
    # The default recorder is a no-op; confirm an agent driver that doesn't
    # pass a recorder still works end-to-end.
    client = _patched_client(
        monkeypatch,
        poll_responses=[{"status": "completed", "result": {"ok": True}}],
        recorder=None,
        heartbeat_every_n="0",
    )
    assert isinstance(client._recorder, NullTaskRecorder)
    out = await client.call_skill_blocking("test_skill", {})
    assert out == {"ok": True}


@pytest.mark.asyncio
async def test_recorder_exceptions_dont_break_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class BoomRecorder:
        def record_pending(self, **_: Any) -> None:
            raise RuntimeError("disk full")

        def record_running(self, _: str) -> None: ...
        def record_heartbeat(self, _: str) -> None: ...
        def record_completed(self, _: str, __: dict[str, Any]) -> None: ...
        def record_failed(self, _: str, __: str) -> None: ...

    client = _patched_client(
        monkeypatch,
        poll_responses=[{"status": "completed", "result": {"ok": True}}],
        recorder=BoomRecorder(),
        heartbeat_every_n="0",
    )
    # The recorder.record_pending raises, but dispatch still returns.
    out = await client.call_skill_blocking("test_skill", {})
    assert out == {"ok": True}
