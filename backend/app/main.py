"""Application entry point.

Creates the FastAPI application and wires up logging, CORS, error
translation, and the routes available at this stage of the build. Routes
stay thin; this module only assembles them, it does not contain business
logic.
"""
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api import health, runs
from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger
from app.orchestration.workflow_service import (
    InvalidStateTransitionError,
    RunNotFoundError,
    StageNotInProgressError,
    WorkflowError,
)
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


# --- Domain error translation -----------------------------------------------
# Routes never construct these HTTP responses themselves for domain errors
# that escape a route's own try/except — this is the single place a
# workflow_service/graph exception becomes an HTTP response, so every route
# gets the same translation without repeating it. Registered from most to
# least specific; FastAPI dispatches to the most specific matching handler.


@app.exception_handler(RunNotFoundError)
async def run_not_found_handler(request: Request, exc: RunNotFoundError) -> JSONResponse:
    return JSONResponse(status_code=status.HTTP_404_NOT_FOUND, content={"detail": str(exc)})


@app.exception_handler(InvalidStateTransitionError)
async def invalid_transition_handler(
    request: Request, exc: InvalidStateTransitionError
) -> JSONResponse:
    return JSONResponse(status_code=status.HTTP_409_CONFLICT, content={"detail": str(exc)})


@app.exception_handler(StageNotInProgressError)
async def stage_not_in_progress_handler(
    request: Request, exc: StageNotInProgressError
) -> JSONResponse:
    return JSONResponse(status_code=status.HTTP_409_CONFLICT, content={"detail": str(exc)})


@app.exception_handler(WorkflowError)
async def workflow_error_handler(request: Request, exc: WorkflowError) -> JSONResponse:
    # Catch-all for any other domain error not translated more specifically
    # above. Never leaks internal detail beyond the exception's own
    # message, which every WorkflowError subclass in this codebase already
    # keeps free of raw stack traces or SQL.
    return JSONResponse(status_code=status.HTTP_400_BAD_REQUEST, content={"detail": str(exc)})


app.include_router(health.router)
app.include_router(runs.router)
