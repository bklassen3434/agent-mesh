"""HTTP control surface for the scheduler (Phase 9).

A tiny Starlette app the API proxies to. Three control routes plus a
health check:

* ``GET  /scheduler/status``      — per-job next/last run + state
* ``POST /scheduler/reload``      — re-read Postgres config now (signal)
* ``POST /scheduler/run/{job_id}``— start an immediate run (409 if busy)

Starlette (not FastAPI) keeps the dependency surface minimal — it's a
transitive dep already, and this service has no schema/validation needs
beyond hand-shaping three JSON responses.
"""
from __future__ import annotations

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from mesh_scheduler.scheduler import SchedulerManager


def build_app(manager: SchedulerManager) -> Starlette:
    async def healthz(_request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    async def status(_request: Request) -> JSONResponse:
        return JSONResponse([s.model_dump(mode="json") for s in manager.status()])

    async def reload(_request: Request) -> JSONResponse:
        try:
            manager.reconcile()
        except Exception as exc:  # Postgres unreachable / mid-migration
            return JSONResponse({"detail": str(exc)}, status_code=503)
        return JSONResponse({"reloaded": True})

    async def run_job(request: Request) -> JSONResponse:
        job_id = request.path_params["job_id"]
        try:
            result = manager.trigger(job_id)
        except KeyError:
            return JSONResponse({"detail": f"unknown job {job_id}"}, status_code=404)
        if result is None:
            return JSONResponse({"detail": "run already in progress"}, status_code=409)
        run_id, triggered_at = result
        return JSONResponse({"run_id": run_id, "triggered_at": triggered_at.isoformat()})

    return Starlette(
        routes=[
            Route("/healthz", healthz),
            Route("/scheduler/status", status),
            Route("/scheduler/reload", reload, methods=["POST"]),
            Route("/scheduler/run/{job_id}", run_job, methods=["POST"]),
        ]
    )
