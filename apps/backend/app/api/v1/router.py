from fastapi import APIRouter

from app.api.v1 import (
    auth,
    build_runs,
    preview,
    project_messages,
    projects,
    requirements,
    workspace,
)

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(auth.router)
api_router.include_router(projects.router)
api_router.include_router(project_messages.router)
api_router.include_router(build_runs.router)
api_router.include_router(requirements.router)
api_router.include_router(workspace.router)
api_router.include_router(preview.router)
