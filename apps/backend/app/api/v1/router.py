from fastapi import APIRouter

from app.api.v1 import auth, build_runs, project_messages, projects

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(auth.router)
api_router.include_router(projects.router)
api_router.include_router(project_messages.router)
api_router.include_router(build_runs.router)
