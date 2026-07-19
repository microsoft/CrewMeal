"""Liveness and readiness probes for Container Apps health checks."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import text

from crewmeal.search_enhancement.database import SearchEnhancementRepository
from crewmeal.search_enhancement.web.dependencies import get_repository

router = APIRouter(tags=["health"])


@router.get("/healthz", summary="Liveness probe")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz", summary="Readiness probe")
def readyz(
    repository: SearchEnhancementRepository = Depends(get_repository),
) -> dict[str, str]:
    try:
        with repository.engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:  # pragma: no cover - defensive
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database is not reachable.",
        ) from exc
    return {"status": "ready"}
