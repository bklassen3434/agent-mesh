from __future__ import annotations

import contextlib
import os

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from mesh_db.pg_migrations import init_pg

from mesh_api.routers import (
    agents,
    ask,
    beliefs,
    briefing,
    claims,
    connectors,
    entities,
    fields,
    graph,
    health,
    overview,
    pipeline_runs,
    skeptic,
    sources,
    stats,
)
from mesh_api.routers import (
    status as status_router,
)


def _ensure_schema() -> None:
    """Best-effort knowledge-schema provisioning at startup.

    The API handles requests read-only (mesh_reader). init_pg needs owner
    privileges (CREATE EXTENSION/ROLE), so this is a convenience that succeeds
    only when the API is given an owner DSN (MESH_PG_URL); otherwise the
    coordinator/operator has already applied the schema and this no-ops.
    Idempotent and never blocks startup.
    """
    with contextlib.suppress(Exception):
        init_pg()


def create_app() -> FastAPI:
    _ensure_schema()

    app = FastAPI(
        title="Agent Mesh Read API",
        description=(
            "Read-only HTTP service in front of the mesh Postgres store. Powers the "
            "Next.js wiki and is reusable as a generic JSON contract over the "
            "knowledge base. All endpoints are GET; the API never writes."
        ),
        version="0.1.0",
    )

    allowed = os.environ.get("API_CORS_ORIGINS", "http://localhost:3000").split(",")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[o.strip() for o in allowed if o.strip()],
        allow_credentials=False,
        # The wiki issues a few non-GET writes from the browser: the Ask page
        # POSTs questions, and the Fields/Connectors pages PATCH/PUT per-field
        # config, so the wiki origin needs more than GET.
        allow_methods=["GET", "POST", "PATCH", "PUT"],
        allow_headers=["*"],
    )

    app.include_router(health.router)
    app.include_router(stats.router)
    app.include_router(pipeline_runs.router)
    app.include_router(entities.router)
    app.include_router(claims.router)
    app.include_router(sources.router)
    app.include_router(beliefs.router)
    app.include_router(skeptic.router)
    app.include_router(briefing.router)
    app.include_router(ask.router)
    app.include_router(status_router.router)
    app.include_router(graph.router)
    app.include_router(fields.router)
    app.include_router(overview.router)
    app.include_router(connectors.router)
    app.include_router(agents.router)
    return app


def main() -> None:
    # Phase 6b: MESH_BIND_INTERFACE wins over API_HOST when set. For
    # Tailscale-only deployment, set it to the host's tailnet IP
    # (100.x.x.x). For local dev, leave it unset and the existing
    # 0.0.0.0 default keeps the API reachable from localhost.
    host = (
        os.environ.get("MESH_BIND_INTERFACE")
        or os.environ.get("API_HOST")
        or "0.0.0.0"
    )
    port = int(os.environ.get("API_PORT", "8000"))
    uvicorn.run("mesh_api.main:create_app", host=host, port=port, factory=True)
