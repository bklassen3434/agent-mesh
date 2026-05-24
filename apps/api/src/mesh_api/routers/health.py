from __future__ import annotations

from fastapi import APIRouter

from mesh_api.deps import db_exists
from mesh_api.schemas import HealthResponse

router = APIRouter(tags=["health"])


@router.get("/healthz", response_model=HealthResponse, summary="Liveness")
def healthz() -> HealthResponse:
    return HealthResponse(status="ok", db_present=db_exists())
