"""Tests for the LangGraph workflow skeleton, now running Movement 1's real
analytical stages.

Run against real Postgres via the same fixtures as test_workflow_service.py
— the graph's nodes call the real workflow_service and real app.agents
functions against a real database, nothing here is mocked.
"""
import uuid

import pytest
from sqlalchemy import select

from app.orchestration.graph import build_graph, run_workflow
from app.orchestration.workflow_service import create_run, start_stage
from app.persistence.models import CheckpointStatus, StageCheckpoint, WorkflowState
from app.storage.document_storage import DocumentInput

_PRD_TEXT = b"""Product Requirements Document
As a user, I want to export my data.
Acceptance Criteria: export completes within 5 seconds.
"""

_SRS_TEXT = b"""Software Requirements Specification
The system shall support concurrent editing.
Functional Requirement: conflict detection must run automatically.
"""


def _doc(filename: str, content: bytes = b"generic line one\ngeneric line two\n") -> DocumentInput:
    return DocumentInput(filename=filename, content=content)


ALL_STAGES = [
    WorkflowState.CLASSIFYING,
    WorkflowState.EXTRACTING,
    WorkflowState.CONSOLIDATING,
    WorkflowState.CONFLICT_SCAN,
]


async def _checkpoints_for(db_session, run_id, stage):
    result = await db_session.execute(
        select(StageCheckpoint).where(
            StageCheckpoint.run_id == run_id, StageCheckpoint.stage == stage
        )
    )
    return result.scalars().all()


# --- Graph can be constructed/compiled ------------------------------------


@pytest.mark.asyncio
async def test_graph_builds_and_compiles(db_session):
    graph = build_graph(db_session)
    # A compiled LangGraph exposes get_graph(); calling it is itself proof
    # the graph is valid and fully wired (nodes/edges resolve), not just
    # that the Python object exists.
    graph.get_graph()


# --- Cold-start path exists and runs end to end, through all four real stages ---


@pytest.mark.asyncio
async def test_cold_start_path_runs_full_movement_1_pipeline(db_session):
    result = await run_workflow(
        db_session, documents=[_doc("prd.txt", _PRD_TEXT), _doc("srs.txt", _SRS_TEXT)]
    )

    assert "run_id" in result
    assert result.get("error") is None
    run_id = uuid.UUID(result["run_id"])

    checkpoints = (
        await db_session.execute(
            select(StageCheckpoint)
            .where(StageCheckpoint.run_id == run_id)
            .order_by(StageCheckpoint.started_at)
        )
    ).scalars().all()

    stages_completed = {(c.stage, c.status) for c in checkpoints}
    assert (WorkflowState.INTAKE_PENDING, CheckpointStatus.COMPLETED) in stages_completed
    for stage in ALL_STAGES:
        assert (stage, CheckpointStatus.COMPLETED) in stages_completed

    classify_cp = next(c for c in checkpoints if c.stage == WorkflowState.CLASSIFYING)
    assert len(classify_cp.output_data["classifications"]) == 2
    types_seen = {c["document_type"] for c in classify_cp.output_data["classifications"]}
    assert "PRD" in types_seen
    assert "SRS" in types_seen

    extract_cp = next(c for c in checkpoints if c.stage == WorkflowState.EXTRACTING)
    assert len(extract_cp.output_data["claims"]) > 0

    consolidate_cp = next(c for c in checkpoints if c.stage == WorkflowState.CONSOLIDATING)
    assert len(consolidate_cp.output_data["statements"]) == len(extract_cp.output_data["claims"])

    conflict_cp = next(c for c in checkpoints if c.stage == WorkflowState.CONFLICT_SCAN)
    assert conflict_cp.output_data["conflicts"] == []  # no key/value overlap between these docs

    from app.orchestration.workflow_service import get_run

    run = await get_run(db_session, run_id)
    assert run.current_state == WorkflowState.COMPLETED


# --- Incremental/resume path: interrupted CLASSIFYING resumes and the chain continues ---


@pytest.mark.asyncio
async def test_resume_path_skips_intake_and_completes_remaining_stages(db_session):
    # Simulate a run that crashed after CLASSIFYING started but before it
    # completed — the Batch 6 crash scenario, now against the real
    # multi-stage pipeline, driven through the graph.
    run = await create_run(db_session, documents=[_doc("c.txt", _PRD_TEXT)])
    await db_session.commit()
    await start_stage(db_session, run.id, WorkflowState.CLASSIFYING)
    await db_session.commit()

    intake_before = await _checkpoints_for(db_session, run.id, WorkflowState.INTAKE_PENDING)
    assert len(intake_before) == 1

    result = await run_workflow(db_session, run_id=run.id)
    assert result.get("error") is None

    # Intake must not have run again.
    intake_after = await _checkpoints_for(db_session, run.id, WorkflowState.INTAKE_PENDING)
    assert len(intake_after) == 1

    # CLASSIFYING's previously-interrupted checkpoint is now completed —
    # exactly one row, not a second one alongside it.
    classify_checkpoints = await _checkpoints_for(db_session, run.id, WorkflowState.CLASSIFYING)
    assert len(classify_checkpoints) == 1
    assert classify_checkpoints[0].status == CheckpointStatus.COMPLETED

    # And the chain continued past the resumed stage — every later stage
    # also completed, proving resume isn't just "fix the interrupted stage
    # and stop."
    for stage in (WorkflowState.EXTRACTING, WorkflowState.CONSOLIDATING, WorkflowState.CONFLICT_SCAN):
        checkpoints = await _checkpoints_for(db_session, run.id, stage)
        assert len(checkpoints) == 1
        assert checkpoints[0].status == CheckpointStatus.COMPLETED


# --- Conditional routing: a completed run ends immediately, is never rerun ---


@pytest.mark.asyncio
async def test_completed_run_routes_straight_to_end(db_session):
    result = await run_workflow(db_session, documents=[_doc("d.txt")])
    run_id = uuid.UUID(result["run_id"])

    conflict_scan_before = await _checkpoints_for(db_session, run_id, WorkflowState.CONFLICT_SCAN)
    assert len(conflict_scan_before) == 1

    # Invoke again with the same, now-completed run_id.
    second_result = await run_workflow(db_session, run_id=run_id)
    assert second_result.get("run_id") == str(run_id)

    # No second checkpoint of any analytical stage was created — routing
    # sent this straight to END without touching any of them.
    for stage in ALL_STAGES:
        checkpoints = await _checkpoints_for(db_session, run_id, stage)
        assert len(checkpoints) == 1
