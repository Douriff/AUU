import os

from fastapi import APIRouter

from app.routes.envelope import ok

router = APIRouter(prefix="/api/v1", tags=["health"])


@router.get("/health")
def health():
    return ok(
        {
            "status": "up",
            "provider": os.getenv("DATA_PROVIDER", "mock"),
            "mode": "paper",
        }
    )
