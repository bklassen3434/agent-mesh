"""Thin async HTTP client for the mesh read API.

Only the two endpoints the bridge needs. Everything is read-only; the bot never
writes to the mesh.
"""
from __future__ import annotations

import contextlib
from typing import Any

import httpx
import structlog

logger = structlog.get_logger(__name__)


class MeshApiError(Exception):
    """Raised when the API is unreachable or returns an unexpected status."""


class BriefingUnavailable(MeshApiError):
    """The briefing couldn't be produced (e.g. no profile configured: 404)."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class MeshApiClient:
    def __init__(self, base_url: str, *, ask_timeout: float = 130.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._ask_timeout = ask_timeout
        self._client = httpx.AsyncClient(timeout=30.0)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def ask(self, question: str, field_slug: str) -> dict[str, Any]:
        """POST /api/v1/ask — grounded Q&A. Returns the Answer payload."""
        try:
            resp = await self._client.post(
                f"{self._base_url}/api/v1/ask",
                params={"field": field_slug},
                json={"question": question},
                timeout=self._ask_timeout,
            )
        except httpx.HTTPError as exc:
            raise MeshApiError(f"could not reach the mesh API: {exc}") from exc
        if resp.status_code != 200:
            raise MeshApiError(f"ask failed ({resp.status_code})")
        data: dict[str, Any] = resp.json()
        return data

    async def briefing(
        self, field_slug: str, target_date: str | None = None
    ) -> dict[str, Any]:
        """GET /api/v1/briefing — the personalized daily digest."""
        params = {"field": field_slug}
        if target_date:
            params["date"] = target_date
        try:
            resp = await self._client.get(
                f"{self._base_url}/api/v1/briefing", params=params, timeout=120.0
            )
        except httpx.HTTPError as exc:
            raise MeshApiError(f"could not reach the mesh API: {exc}") from exc
        if resp.status_code == 404:
            detail = "no briefing available"
            with contextlib.suppress(Exception):  # body may not be JSON
                detail = resp.json().get("detail", detail)
            raise BriefingUnavailable(detail)
        if resp.status_code != 200:
            raise MeshApiError(f"briefing failed ({resp.status_code})")
        data: dict[str, Any] = resp.json()
        return data
