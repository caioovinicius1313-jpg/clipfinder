import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.router import api_router
from app.core.config import settings
from app.services.storage_service import StorageOperationError, StorageService

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI):
    if settings.bucket_enabled:
        try:
            StorageService().configure_browser_cors(settings.allowed_origins)
        except StorageOperationError:
            logger.exception("Railway Bucket CORS configuration failed")
    yield

app = FastAPI(
    title="ClipFinder API",
    version="0.1.0",
    description="Base API for ClipFinder projects and source video uploads.",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router)


@app.get("/health", tags=["health"])
def health_check() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/health/storage", tags=["health"])
def storage_health_check() -> dict[str, str]:
    return {"storage": "bucket" if settings.bucket_enabled else "local"}
