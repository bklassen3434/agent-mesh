"""Integration tests for the task-based mesh A2A protocol.

A fake single-skill agent is spun up in a subprocess. Tests submit tasks
through ``MeshA2AClient``, poll, and assert wire-shape + behavior of the
``submit_task`` / ``get_task`` / ``call_skill_blocking`` primitives.
"""
from __future__ import annotations

import asyncio
import socket
import subprocess
import sys
import textwrap
import time
from collections.abc import Generator
from pathlib import Path
from typing import Any

import httpx
import pytest
from a2a.utils.constants import AGENT_CARD_WELL_KNOWN_PATH
from mesh_a2a.client import (
    MeshA2AClient,
    SkillCallError,
    SkillNotFoundError,
    TaskTimeoutError,
)


def _wait_for_server(url: str, timeout: float = 10.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            httpx.get(url, timeout=1.0)
            return
        except Exception:
            time.sleep(0.2)
    raise TimeoutError(f"Server at {url} did not start within {timeout}s")


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port: int = s.getsockname()[1]
    s.close()
    return port


_FAKE_AGENT_SOURCE = textwrap.dedent(
    """
    import asyncio
    import os
    import sys

    import uvicorn
    from mesh_a2a.card_builder import build_agent_card
    from mesh_a2a.task_server import build_task_app


    async def _slow_echo(payload):
        delay = float(payload.get("delay", 0.0))
        if delay:
            await asyncio.sleep(delay)
        if payload.get("boom"):
            raise RuntimeError("intentional failure")
        return {"echo": payload.get("value", "")}


    def main() -> None:
        port = int(os.environ["AGENT_PORT"])
        url = os.environ["AGENT_PUBLIC_URL"]
        card = build_agent_card(
            name="Fake Echo",
            description="echoes payload after optional delay",
            url=url,
            skill_id="fake_echo",
            skill_name="Echo",
            skill_description="echo back",
        )
        app = build_task_app(
            agent_card=card,
            skill_handlers={"fake_echo": _slow_echo},
            agent_name="fake_echo",
        )
        uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


    if __name__ == "__main__":
        main()
    """
)


@pytest.fixture()
def fake_agent_server(tmp_path: Path) -> Generator[str, None, None]:
    """Spawn the fake echo agent on a free port; return its base URL."""
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    script = tmp_path / "fake_agent.py"
    script.write_text(_FAKE_AGENT_SOURCE)

    proc = subprocess.Popen(
        [sys.executable, str(script)],
        env={
            **__import__("os").environ,
            "AGENT_HOST": "127.0.0.1",
            "AGENT_PORT": str(port),
            "AGENT_PUBLIC_URL": base_url,
        },
    )
    try:
        _wait_for_server(f"{base_url}/healthz")
        yield base_url
    finally:
        proc.terminate()
        proc.wait(timeout=5)


def test_healthz_responds(fake_agent_server: str) -> None:
    resp = httpx.get(f"{fake_agent_server}/healthz")
    assert resp.status_code == 200
    assert resp.json()["agent"] == "fake_echo"


def test_agent_card_advertises_skill(fake_agent_server: str) -> None:
    resp = httpx.get(f"{fake_agent_server}{AGENT_CARD_WELL_KNOWN_PATH}")
    assert resp.status_code == 200
    assert any(s["id"] == "fake_echo" for s in resp.json()["skills"])


def test_submit_then_get_lifecycle(fake_agent_server: str) -> None:
    async def _run() -> dict[str, Any]:
        async with MeshA2AClient() as c:
            await c.discover([fake_agent_server])
            task_id, url = await c.submit_task("fake_echo", {"value": "hi", "delay": 0.1})
            statuses: list[str] = []
            for _ in range(40):
                rec = await c.get_task(task_id, url)
                statuses.append(rec["status"])
                if rec["status"] in ("completed", "failed"):
                    return {
                        "final": rec["status"],
                        "result": rec["result"],
                        "saw_pending_or_running": any(
                            s in ("pending", "running") for s in statuses
                        ),
                    }
                await asyncio.sleep(0.05)
            raise AssertionError(f"never completed: {statuses}")

    out = asyncio.run(_run())
    assert out["final"] == "completed"
    assert out["result"] == {"echo": "hi"}
    assert out["saw_pending_or_running"] is True


def test_call_skill_blocking_returns_result(fake_agent_server: str) -> None:
    async def _run() -> dict[str, Any]:
        async with MeshA2AClient() as c:
            await c.discover([fake_agent_server])
            return await c.call_skill_blocking(
                "fake_echo", {"value": "x"}, poll_interval=0.05, timeout=10.0
            )

    assert asyncio.run(_run()) == {"echo": "x"}


def test_call_skill_blocking_propagates_failure(fake_agent_server: str) -> None:
    async def _run() -> str:
        async with MeshA2AClient() as c:
            await c.discover([fake_agent_server])
            try:
                await c.call_skill_blocking(
                    "fake_echo", {"boom": True}, poll_interval=0.05, timeout=5.0
                )
            except SkillCallError as exc:
                return str(exc)
        return "no error raised"

    msg = asyncio.run(_run())
    assert "RuntimeError" in msg or "intentional failure" in msg


def test_call_skill_blocking_times_out_when_too_slow(fake_agent_server: str) -> None:
    async def _run() -> str:
        async with MeshA2AClient() as c:
            await c.discover([fake_agent_server])
            try:
                await c.call_skill_blocking(
                    "fake_echo",
                    {"delay": 5.0, "value": "late"},
                    poll_interval=0.05,
                    timeout=0.5,
                )
            except TaskTimeoutError as exc:
                return str(exc)
        return "no timeout raised"

    msg = asyncio.run(_run())
    assert "did not complete within" in msg


def test_agent_restart_surfaces_as_skill_call_error(fake_agent_server: str) -> None:
    """If the agent restarts mid-task, in-memory registry loses the record,
    so a poll returns 404 and the orchestrator sees a SkillCallError it can
    catch and record on the source. This is the documented Phase 5a
    behavior — durability is Phase 6.
    """

    async def _run() -> str:
        async with MeshA2AClient() as c:
            await c.discover([fake_agent_server])
            try:
                # Synthesizes "task vanished after agent restart": ask for a
                # task_id we never created. Wire-level, this is identical to
                # what happens after a real restart.
                await c.get_task("00000000-0000-0000-0000-000000000000", fake_agent_server)
            except SkillCallError as exc:
                return str(exc)
        return "no error raised"

    msg = asyncio.run(_run())
    assert "not found" in msg


def test_submit_unknown_skill_raises(fake_agent_server: str) -> None:
    async def _run() -> None:
        async with MeshA2AClient() as c:
            await c.discover([fake_agent_server])
            await c.submit_task("not_a_real_skill", {})

    with pytest.raises(SkillNotFoundError):
        asyncio.run(_run())
