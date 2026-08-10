"""Tests for the LangGraph workflow skeleton.

Run against real Postgres via the same fixtures as test_workflow_service.py
— the graph's nodes call the real workflow_service against a real database,
nothing here is mocked.
"""
import uuid

import pytest
from sqlalchemy import select

from app.orchestration.graph import build_graph, run_workflow
from app.orchestration.workflow_service import create_run, start_stage
from app.persistence.models import CheckpointStatus, StageCheckpoint, WorkflowState
from app.storage.document_storage import DocumentInput


def _doc(filename: str, content: bytes = b"test content") -> DocumentInput:
    return DocumentInput(filename=filename, content=content)


# --- Graph can be constructed/compiled ------------------------------------


@pytest.mark.asyncio
async def test_graph_builds_and_compiles(db_session):
    graph = build_graph(db_session)
    # A compiled LangGraph exposes get_graph(); calling it is itself proof
    # the graph is valid and fully wired (nodes/edges resolve), not just
    # that the Python object exists.
    graph.get_graph()


# --- Cold-start path exists and runs end to end ---------------------------


@pytest.mark.asyncio
async def test_cold_start_path_creates_run_and_completes_it(db_session):
    result = await run_workflow(db_session, documents=[_doc("a.pdf"), _doc("b.pdf")])

    assert "run_id" in result
    assert "error" not in result or result["error"] is None

    run_id = uuid.UUID(result["run_id"])

    # Both stages actually ran: INTAKE_PENDING (from create_run) and
    # PROCESSING, both durably completed.
    checkpoints = (
        await db_session.execute(
            select(StageCheckpoint)
            .where(StageCheckpoint.run_id == run_id)
            .order_by(StageCheckpoint.started_at)
        )
    ).scalars().all()

    stages_completed = [(c.stage, c.status) for c in checkpoints]
    assert (WorkflowState.INTAKE_PENDING, CheckpointStatus.COMPLETED) in stages_completed
    assert (WorkflowState.PROCESSING, CheckpointStatus.COMPLETED) in stages_completed

    processing_checkpoint = next(c for c in checkpoints if c.stage == WorkflowState.PROCESSING)
    assert processing_checkpoint.output_data == {"documents_seen": 2}


# --- Incremental/resume path: interrupted PROCESSING is resumed, not restarted ---


@pytest.mark.asyncio
async def test_resume_path_skips_intake_and_finishes_interrupted_processing(db_session):
    # Simulate a run that crashed after PROCESSING started but before it
    # completed — exactly the Batch 6 crash scenario, now driven through
    # the graph instead of calling workflow_service directly.
    run = await create_run(db_session, documents=[_doc("c.pdf")])
    await db_session.commit()
    await start_stage(db_session, run.id, WorkflowState.PROCESSING)
    await db_session.commit()

    intake_checkpoints_before = (
        await db_session.execute(
            select(StageCheckpoint).where(
                StageCheckpoint.run_id == run.id,
                StageCheckpoint.stage == WorkflowState.INTAKE_PENDING,
            )
        )
    ).scalars().all()
    assert len(intake_checkpoints_before) == 1

    result = await run_workflow(db_session, run_id=run.id)

    assert result.get("error") is None

    # Intake must not have run again — still exactly one INTAKE_PENDING
    # checkpoint, proving the conditional entry point genuinely skipped it
    # rather than re-registering documents.
    intake_checkpoints_after = (
        await db_session.execute(
            select(StageCheckpoint).where(
                StageCheckpoint.run_id == run.id,
                StageCheckpoint.stage == WorkflowState.INTAKE_PENDING,
            )
        )
    ).scalars().all()
    assert len(intake_checkpoints_after) == 1

    # The previously-interrupted PROCESSING checkpoint is now completed —
    # not a second PROCESSING checkpoint alongside it.
    processing_checkpoints = (
        await db_session.execute(
            select(StageCheckpoint).where(
                StageCheckpoint.run_id == run.id,
                StageCheckpoint.stage == WorkflowState.PROCESSING,
            )
        )
    ).scalars().all()
    assert len(processing_checkpoints) == 1
    assert processing_checkpoints[0].status == CheckpointStatus.COMPLETED


# --- Conditional routing: a completed run ends immediately, is never rerun ---


@pytest.mark.asyncio
async def test_completed_run_routes_straight_to_end(db_session):
    result = await run_workflow(db_session, documents=[_doc("d.pdf")])
    run_id = uuid.UUID(result["run_id"])

    processing_checkpoints_before = (
        await db_session.execute(
            select(StageCheckpoint).where(
                StageCheckpoint.run_id == run_id,
                StageCheckpoint.stage == WorkflowState.PROCESSING,
            )
        )
    ).scalars().all()
    assert len(processing_checkpoints_before) == 1

    # Invoke again with the same, now-completed run_id.
    second_result = await run_workflow(db_session, run_id=run_id)
    assert second_result.get("run_id") == str(run_id)

    # No second PROCESSING checkpoint was created — routing sent this
    # straight to END without touching processing at all.
    processing_checkpoints_after = (
        await db_session.execute(
            select(StageCheckpoint).where(
                StageCheckpoint.run_id == run_id,
                StageCheckpoint.stage == WorkflowState.PROCESSING,
            )
        )
    ).scalars().all()
    assert len(processing_checkpoints_after) == 1
