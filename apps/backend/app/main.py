from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1.router import api_router
from app.core.exceptions import register_exception_handlers
from app.core.logging import configure_logging
from app.core.settings import settings

configure_logging()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    if settings.workflow_embedded_worker:
        from app.worker import start_embedded_worker, stop_embedded_worker

        start_embedded_worker()
        try:
            yield
        finally:
            stop_embedded_worker()
        return
    yield


app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
    debug=settings.debug,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

register_exception_handlers(app)
app.include_router(api_router)
