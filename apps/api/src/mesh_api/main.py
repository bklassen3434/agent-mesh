from __future__ import annotations

import os

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from mesh_db.connection import get_connection
from mesh_db.migrations import apply_migrations

from mesh_api.routers import health


def _ensure_schema() -> None:
    """Apply migrations at startup against a brief read-write connection.

    The API operates read-only for all request handling. This one-shot write
    open ensures a freshly mounted volume has the schema before requests
    arrive. Migrations are idempotent — re-running is safe.
    """
    conn = get_connection(read_only=False)
    try:
        apply_migrations(conn)
    finally:
        conn.close()


def create_app() -> FastAPI:
    _ensure_schema()

    app = FastAPI(
        title="Agent Mesh Read API",
        description=(
            "Read-only HTTP service in front of the mesh DuckDB. Powers the "
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
        allow_methods=["GET"],
        allow_headers=["*"],
    )

    app.include_router(health.router)
    return app


def main() -> None:
    host = os.environ.get("API_HOST", "0.0.0.0")
    port = int(os.environ.get("API_PORT", "8000"))
    uvicorn.run("mesh_api.main:create_app", host=host, port=port, factory=True)
