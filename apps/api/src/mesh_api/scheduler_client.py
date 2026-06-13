"""Thin HTTP client to the scheduler service's control surface (Phase 9).

The API owns schedule *config* (Postgres) but not *execution*. Triggering
runs, reading live next/last-run state, and signalling a config reload all
go through the scheduler's small HTTP API. Postgres remains the source of
truth, so a missed reload signal self-heals on the scheduler's 30s poll —
these calls are allowed to fail without corrupting state.
"""
from __future__ import annotations

import os
from typing import Any

import httpx

_TIMEOUT = httpx.Timeout(5.0, connect=2.0)


def base_url() -> str:
    return os.environ.get("SCHEDULER_URL", "http://scheduler:9100").rstrip("/")


def fetch_status() -> list[dict[str, Any]]:
    """Per-job status from the scheduler. Raises httpx.HTTPError on failure."""
    with httpx.Client(timeout=_TIMEOUT) as client:
        resp = client.get(f"{base_url()}/scheduler/status")
        resp.raise_for_status()
        data = resp.json()
    return data if isinstance(data, list) else []


def signal_reload() -> bool:
    """Best-effort: ask the scheduler to re-read config now. Never raises."""
    try:
        with httpx.Client(timeout=_TIMEOUT) as client:
            resp = client.post(f"{base_url()}/scheduler/reload")
        return resp.status_code == 200
    except httpx.HTTPError:
        return False


def trigger_run(job_id: str, field: str = "ai-robotics") -> httpx.Response:
    """Ask the scheduler to start an immediate run. Raises on connection error;
    the caller inspects the status code (409 already-running, 404 unknown)."""
    with httpx.Client(timeout=_TIMEOUT) as client:
        return client.post(
            f"{base_url()}/scheduler/run/{job_id}", params={"field": field}
        )
