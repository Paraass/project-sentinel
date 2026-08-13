"""Tests for the durable workflow persistence and orchestration layer.

Run against a real PostgreSQL database (see conftest.py) — no mocking of the
database or the service layer. These prove the six behaviors Batch 6 was
scoped around:

1. A workflow can be created and persisted.
2. State transitions are persisted.
3. A completed stage output survives a new session/process boundary.
4. A restart/resume operation identifies the correct unfinished stage.
5. A completed stage is not rerun after restart.
6. Invalid state transitions are rejected.

Plus the required error handling: completing a stage that was never started,
and looking up a run that doesn't exist.
"""
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.orchestration.workflow_service import (
    InvalidStateTransitionError,
    RunNotFoundError,
    StageNotInProgressError,
    complete_stage,
    create_run,
    get_resume_stage,
    get_run,
    start_stage,
)
from app.persistence.models import (
    CheckpointStatus,
    Document,
    StageCheckpoint,
    WorkflowState,
)
from app.storage.document_storage import DocumentInput, read_document
from tests.conftest import TEST_DATABASE_URL


def _doc(filename: str, content: bytes = b"test content") -> DocumentInput:
    """Small local factory to keep test call sites short — this batch
    requires create_run to receive real content, not just a filename."""
    return DocumentInput(filename=filename, content=content)


# --- 1. A workflow can be created and persisted --------------------------


@pytest.mark.asyncio
async def test_create_run_persists_run_and_documents(db_session):
    run = await create_run(db_session, documents=[_doc("a.pdf"), _doc("b.pdf")], name="test-pile")
    await db_session.commit()

    assert run.id is not None
    assert run.current_state == WorkflowState.INTAKE_PENDING

    result = await db_session.execute(select(Document).where(Document.run_id == run.id))
    documents = result.scalars().all()
    assert {d.filename for d in documents} == {"a.pdf", "b.pdf"}

    # Metadata is persisted, correctly, per document.
    import hashlib

    expected_hash = hashlib.sha256(b"test content").hexdigest()
    for doc in documents:
        assert doc.content_hash == expected_hash
        assert doc.storage_key == expected_hash  # content-addressed: key IS the hash
        assert doc.size_bytes == len(b"test content")
        assert doc.content_type == "application/pdf"  # guessed from the .pdf filename

    # And the stored content is genuinely retrievable, not just referenced.
    assert read_document(documents[0].storage_key) == b"test content"

    result = await db_session.execute(
        select(StageCheckpoint).where(StageCheckpoint.run_id == run.id)
    )
    checkpoints = result.scalars().all()
    assert len(checkpoints) == 1
    assert checkpoints[0].stage == WorkflowState.INTAKE_PENDING
    assert checkpoints[0].status == CheckpointStatus.COMPLETED


# --- 2. State transitions are persisted -----------------------------------


@pytest.mark.asyncio
async def test_start_stage_persists_transition(db_session):
    run = await create_run(db_session, documents=[_doc("doc.pdf")])
    await db_session.commit()

    checkpoint = await start_stage(db_session, run.id, WorkflowState.CLASSIFYING)
    await db_session.commit()

    assert checkpoint.status == CheckpointStatus.IN_PROGRESS
    assert checkpoint.stage == WorkflowState.CLASSIFYING

    reloaded = await get_run(db_session, run.id)
    assert reloaded.current_state == WorkflowState.CLASSIFYING


# --- 6. Invalid state transitions are rejected ----------------------------


@pytest.mark.asyncio
async def test_invalid_transition_is_rejected(db_session):
    run = await create_run(db_session, documents=[_doc("doc.pdf")])
    await db_session.commit()

    # INTAKE_PENDING -> COMPLETED is not an allowed transition; the four
    # analytical stages must happen first.
    with pytest.raises(InvalidStateTransitionError):
        await start_stage(db_session, run.id, WorkflowState.COMPLETED)

    # The rejected attempt must not have silently changed the run's state.
    await db_session.rollback()
    reloaded = await get_run(db_session, run.id)
    assert reloaded.current_state == WorkflowState.INTAKE_PENDING


# --- 3. A completed stage output survives a new session/process boundary -


@pytest.mark.asyncio
async def test_completed_output_survives_new_session(db_session):
    """Uses CONFLICT_SCAN (the new last real stage before COMPLETED) rather
    than the retired PROCESSING placeholder — CONFLICT_SCAN is now where
    "completing this stage moves the run to COMPLETED" is actually true.
    Walks through the full real sequence to get there legitimately, which
    doubles as a workflow_service-level regression check of the whole
    Movement 1 transition chain, independent of graph.py.
    """
    run = await create_run(db_session, documents=[_doc("doc.pdf")])
    await db_session.commit()

    for stage in (
        WorkflowState.CLASSIFYING,
        WorkflowState.EXTRACTING,
        WorkflowState.CONSOLIDATING,
    ):
        await start_stage(db_session, run.id, stage)
        await db_session.commit()
        await complete_stage(db_session, run.id, stage, output_data={})
        await db_session.commit()

    await start_stage(db_session, run.id, WorkflowState.CONFLICT_SCAN)
    await db_session.commit()
    await complete_stage(
        db_session, run.id, WorkflowState.CONFLICT_SCAN, output_data={"conflicts_found": 0}
    )
    await db_session.commit()

    # Simulate a genuinely new process: a fresh engine and session, not the
    # one this test already had open, reading only what's durably in
    # Postgres.
    fresh_engine = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    fresh_session_factory = async_sessionmaker(bind=fresh_engine, expire_on_commit=False)
    async with fresh_session_factory() as fresh_session:
        reloaded_run = await get_run(fresh_session, run.id)
        assert reloaded_run.current_state == WorkflowState.COMPLETED

        result = await fresh_session.execute(
            select(StageCheckpoint).where(
                StageCheckpoint.run_id == run.id,
                StageCheckpoint.stage == WorkflowState.CONFLICT_SCAN,
            )
        )
        checkpoint = result.scalar_one()
        assert checkpoint.status == CheckpointStatus.COMPLETED
        assert checkpoint.output_data == {"conflicts_found": 0}
    await fresh_engine.dispose()


# --- 4. Restart/resume identifies the correct unfinished stage -----------


@pytest.mark.asyncio
async def test_resume_after_interrupted_classifying_identifies_classifying(db_session):
    run = await create_run(db_session, documents=[_doc("doc.pdf")])
    await db_session.commit()

    # CLASSIFYING is started and committed, but never completed — this is
    # the "killed mid-stage" scenario. No in-memory flag is set anywhere;
    # the only evidence this happened is the committed IN_PROGRESS row.
    await start_stage(db_session, run.id, WorkflowState.CLASSIFYING)
    await db_session.commit()

    # A genuinely new engine/session, standing in for a restarted process
    # that has no memory of what the previous process was doing.
    fresh_engine = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    fresh_session_factory = async_sessionmaker(bind=fresh_engine, expire_on_commit=False)
    async with fresh_session_factory() as fresh_session:
        resume_stage = await get_resume_stage(fresh_session, run.id)
        assert resume_stage == WorkflowState.CLASSIFYING
    await fresh_engine.dispose()


# --- 5. A completed stage is not rerun after restart ----------------------


@pytest.mark.asyncio
async def test_resume_after_completed_conflict_scan_does_not_rerun(db_session):
    """Uses CONFLICT_SCAN — the new last real stage — since that is now
    where "completing this stage terminates the run" holds true.
    """
    run = await create_run(db_session, documents=[_doc("doc.pdf")])
    await db_session.commit()

    for stage in (
        WorkflowState.CLASSIFYING,
        WorkflowState.EXTRACTING,
        WorkflowState.CONSOLIDATING,
        WorkflowState.CONFLICT_SCAN,
    ):
        await start_stage(db_session, run.id, stage)
        await db_session.commit()
        await complete_stage(db_session, run.id, stage, output_data={"ok": True})
        await db_session.commit()

    fresh_engine = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    fresh_session_factory = async_sessionmaker(bind=fresh_engine, expire_on_commit=False)
    async with fresh_session_factory() as fresh_session:
        resume_stage = await get_resume_stage(fresh_session, run.id)
        # CONFLICT_SCAN already completed -> run is COMPLETED, a terminal
        # state, not CONFLICT_SCAN again.
        assert resume_stage == WorkflowState.COMPLETED

        # And attempting to start CONFLICT_SCAN again must be rejected, not
        # silently accepted.
        with pytest.raises(InvalidStateTransitionError):
            await start_stage(fresh_session, run.id, WorkflowState.CONFLICT_SCAN)
    await fresh_engine.dispose()


# --- Required error handling ----------------------------------------------


@pytest.mark.asyncio
async def test_complete_stage_without_in_progress_checkpoint_raises(db_session):
    run = await create_run(db_session, documents=[_doc("doc.pdf")])
    await db_session.commit()

    # CLASSIFYING was never started, so there is nothing to complete.
    with pytest.raises(StageNotInProgressError):
        await complete_stage(db_session, run.id, WorkflowState.CLASSIFYING, output_data={})


@pytest.mark.asyncio
async def test_get_run_raises_for_unknown_id(db_session):
    with pytest.raises(RunNotFoundError):
        await get_run(db_session, uuid.uuid4())
