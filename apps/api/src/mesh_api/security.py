"""Privileged-write guard for the otherwise-public read API.

The API is fronted by the Next.js wiki, which is the auth boundary: it
authenticates the admin (shared password → signed cookie) and proxies every
privileged call to the API server-side, attaching a shared internal token and
the caller's role. This dependency is what makes that boundary real on the API
side — without it, anyone who can reach the API port could create a field or
flip a connector by skipping the wiki.

Enforcement is opt-in by configuration: when ``MESH_INTERNAL_TOKEN`` is unset
(local dev, tests), the guard no-ops so nothing has to thread a token through.
Set the token in production (same value in the wiki + API) to require it.
"""
from __future__ import annotations

import os

from fastapi import Header, HTTPException


def _expected_token() -> str | None:
    token = os.environ.get("MESH_INTERNAL_TOKEN", "").strip()
    return token or None


def require_internal_caller(
    x_mesh_internal_token: str | None = Header(default=None),
) -> None:
    """Reject calls that didn't come from the wiki server.

    Used by the rate-limited ``/ask`` endpoint so a beta visitor can't skip the
    wiki (and its per-browser quota) by hitting the API directly. No role check —
    both admins and betas legitimately ask; the quota logic distinguishes them.
    No-op when no internal token is configured (dev/tests).
    """
    expected = _expected_token()
    if expected is None:
        return
    if x_mesh_internal_token != expected:
        raise HTTPException(status_code=401, detail="missing or invalid internal token")


def require_internal_admin(
    x_mesh_internal_token: str | None = Header(default=None),
    x_mesh_role: str | None = Header(default=None),
) -> None:
    """Reject privileged writes that didn't come from the wiki as an admin.

    No-op when no internal token is configured (dev/tests). Otherwise the call
    must carry the shared token *and* an admin role header — both set by the
    wiki's server-side proxy only after it has verified the admin cookie.
    """
    expected = _expected_token()
    if expected is None:
        return
    if x_mesh_internal_token != expected:
        raise HTTPException(status_code=401, detail="missing or invalid internal token")
    if (x_mesh_role or "").strip().lower() != "admin":
        raise HTTPException(status_code=403, detail="admin role required")
