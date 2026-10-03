"""GET /api/v1/status — login-free, coarse system status (P1-6). See app.status."""
from __future__ import annotations

from fastapi import APIRouter

from app.routes.envelope import ok
from app.status import public_status

router = APIRouter(prefix="/api/v1", tags=["status"])


@router.get("/status")
def status():
    return ok(public_status())
