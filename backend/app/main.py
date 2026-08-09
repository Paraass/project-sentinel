"""Application entry point.

Creates the FastAPI application and wires up logging, CORS, and the routes
available at this stage of the build. Routes stay thin; this module only
assembles them, it does not contain business logic.
"""
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import health
from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger
from app.persistence.database import dispose_engine, get_engine
from app.persistence.models import init_models

settings = get_settings()
configure_logging(settings.log_level)
logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    logger.info("application_startup", extra={"environment": settings.environment})
    await init_models(get_engine())
    yield
    logger.info("application_shutdown")
    await dispose_engine()


app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health.router)
