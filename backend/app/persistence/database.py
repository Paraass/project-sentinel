"""Database connection configuration and initialization.

Deliberately limited to connection setup. No models, no schemas, no tables —
those belong to a future Build Order. This module exists so future
persistence code has a single, already-reviewed place to obtain a session
from, rather than each future module configuring its own engine.
"""
from contextlib import asynccontextmanager
from typing import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.core.config import get_settings

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    """Return the module-level async engine, creating it on first use."""
    global _engine
    if _engine is None:
        settings = get_settings()
        _engine = create_async_engine(
    settings.database_url,
    pool_pre_ping=True,
    poolclass=NullPool,
)
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """Return the module-level async session factory, creating it on first use."""
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(bind=get_engine(), expire_on_commit=False)
    return _session_factory


@asynccontextmanager
async def get_session() -> AsyncIterator[AsyncSession]:
    """Yield a database session.

    Used directly by workflow_service/graph callers that manage their own
    commit boundaries, and wrapped by get_db_session() below for FastAPI
    route handlers.
    """
    session_factory = get_session_factory()
    async with session_factory() as session:
        yield session


async def dispose_engine() -> None:
    """Dispose the engine's connection pool and clear the cached session
    factory. Called on application shutdown.

    Both `_engine` and `_session_factory` must be cleared together:
    `_session_factory` is a closure bound to whatever engine existed when
    it was first created, and `get_session_factory()` only builds a new
    one when the cache is empty. Clearing `_engine` alone leaves the next
    `get_session()` call handing out sessions from a factory still bound
    to the disposed engine's pool — connections tied to whatever event
    loop that pool was created on, which is exactly wrong the moment a new
    engine gets created on a different loop (e.g. a fresh TestClient per
    test). Found while reconstructing this file, not re-verified against
    a live failure, since the environment that could run it was lost —
    flagging that plainly rather than claiming it's confirmed.
    """
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
        _engine = None
    _session_factory = None


async def get_db_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency wrapper around get_session().

    Route handlers already commit at each meaningful step via
    workflow_service/graph functions (proven durable since Batch 6) — this
    final commit is a safety net for anything a route reads/writes
    directly, and the rollback-on-exception ensures a failed request never
    leaves a half-applied change for the next request to trip over.
    """
    async with get_session() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
