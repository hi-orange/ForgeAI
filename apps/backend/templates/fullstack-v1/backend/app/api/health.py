from fastapi import APIRouter

from app.schemas.response import success

router = APIRouter(tags=["health"])


@router.get("/health")
def health() -> dict:
    return success({"status": "ok", "template_version": "fullstack-v1"})
