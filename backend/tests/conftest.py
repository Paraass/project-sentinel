"""Shared fixtures for workflow persistence/orchestration tests.

These tests run against a real PostgreSQL database — not a mock — per the
Batch 6 requirement to prove persistence and restart/resume genuinely work.
TEST_DATABASE_URL defaults to the docker-compose test database naming
convention; override it via environment variable when running outside
Docker.
"""
import os

import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.persistence.models import init_models

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://postgres:postgres@localhost:5432/sentinel_test",
)


@pytest_asyncio.fixture
async def db_engine():
    """A fresh engine per test.

    Deliberately function-scoped, not session-scoped: pytest-asyncio gives
    each test function its own event loop by default, and asyncpg
    connections are bound to the loop that created them. A session-scoped
    engine's pooled connections would outlive that loop and break on the
    next test. `init_models` is idempotent (`create_all` no-ops on tables
    that already exist), so calling it per test costs nothing meaningful.
    """
    engine = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    await init_models(engine)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def db_session(db_engine):  # noqa: F811 - depends on the (now function-scoped) fixture above
    """A fresh session per test, backed by the real engine above.

    Restart/resume tests need genuinely committed data (a real process
    restart wouldn't have an open transaction to roll back), so isolation
    here is a post-test TRUNCATE rather than a rolled-back transaction.
    """
    session_factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    async with session_factory() as session:
        yield session

    async with db_engine.begin() as conn:
        await conn.execute(
            text("TRUNCATE TABLE stage_checkpoints, documents, workflow_runs CASCADE")
        )
