"""Durable workflow orchestration service.

This is the state backbone described in the behavioral architecture's
Orchestration Module — minus LangGraph, minus agents, minus everything that
actually does analysis work. Its only job is: create runs, transition state,
persist every transition immediately, and tell a caller (a human, a test, or
eventually LangGraph) what stage to run next after a restart.

Every function here takes an already-open AsyncSession rather than opening
its own — callers control transaction/commit boundaries, this module never
does. This keeps the service layer testable against a real session without
hidden connection management, and matches "Orchestration Module -> Persistence
Module" as the only allowed dependency direction: this module reads/writes
through the ORM models directly (there being no separate repository layer
yet), but never manages engine/session lifecycle itself — that stays owned by
app.persistence.database.
"""
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.models import (
    CheckpointStatus,
    Document,
    StageCheckpoint,
    WorkflowRun,
    WorkflowState,
)

# The only stage sequence this batch knows about. Later batches extend this
# as real movement-level states (CLASSIFYING, EXTRACTING, ...) replace
# PROCESSING as a single opaque step — this map is deliberately the seed,
# not a final state machine.
_ALLOWED_TRANSITIONS: dict[WorkflowState, set[WorkflowState]] = {
    WorkflowState.INTAKE_PENDING: {WorkflowState.PROCESSING},
    WorkflowState.PROCESSING: {WorkflowState.COMPLETED, WorkflowState.FAILED},
    WorkflowState.COMPLETED: set(),
    WorkflowState.FAILED: set(),
}

# Ordered so "the next stage after X" can be looked up positionally.
_STAGE_SEQUENCE: list[WorkflowState] = [
    WorkflowState.INTAKE_PENDING,
    WorkflowState.PROCESSING,
    WorkflowState.COMPLETED,
]


class WorkflowError(Exception):
    """Base class for workflow backbone errors."""


class InvalidStateTransitionError(WorkflowError):
    """Raised when a requested transition isn't allowed from the current state."""

    def __init__(self, from_state: WorkflowState, to_state: WorkflowState) -> None:
        super().__init__(f"Cannot transition from {from_state.value} to {to_state.value}")
        self.from_state = from_state
        self.to_state = to_state


class RunNotFoundError(WorkflowError):
    """Raised when a run_id doesn't correspond to any persisted run."""

    def __init__(self, run_id: uuid.UUID) -> None:
        super().__init__(f"No workflow run found for id {run_id}")
        self.run_id = run_id


class StageNotInProgressError(WorkflowError):
    """Raised when completing/failing a stage that has no IN_PROGRESS checkpoint."""

    def __init__(self, run_id: uuid.UUID, stage: WorkflowState) -> None:
        super().__init__(f"No in-progress checkpoint for run {run_id}, stage {stage.value}")
        self.run_id = run_id
        self.stage = stage


async def get_run(session: AsyncSession, run_id: uuid.UUID) -> WorkflowRun:
    """Load a run by id. Raises RunNotFoundError if it doesn't exist."""
    run = await session.get(WorkflowRun, run_id)
    if run is None:
        raise RunNotFoundError(run_id)
    return run


async def create_run(
    session: AsyncSession,
    filenames: list[str],
    name: str | None = None,
) -> WorkflowRun:
    """Create a new run, register its documents, and durably complete the
    INTAKE_PENDING stage.

    Intake's "work" (registering documents) happens atomically here, so it
    is recorded as an already-completed checkpoint rather than an
    IN_PROGRESS one — there is no partial-intake state this batch needs to
    model, unlike PROCESSING, which is where a real interruption risk
    exists.
    """
    run = WorkflowRun(name=name, current_state=WorkflowState.INTAKE_PENDING)
    session.add(run)
    await session.flush()  # populate run.id before children reference it

    for filename in filenames:
        session.add(Document(run_id=run.id, filename=filename))

    session.add(
        StageCheckpoint(
            run_id=run.id,
            stage=WorkflowState.INTAKE_PENDING,
            status=CheckpointStatus.COMPLETED,
        )
    )

    await session.flush()
    return run


async def _latest_checkpoint(
    session: AsyncSession, run_id: uuid.UUID
) -> StageCheckpoint | None:
    result = await session.execute(
        select(StageCheckpoint)
        .where(StageCheckpoint.run_id == run_id)
        .order_by(StageCheckpoint.started_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def start_stage(
    session: AsyncSession, run_id: uuid.UUID, stage: WorkflowState
) -> StageCheckpoint:
    """Transition a run into `stage` and persist an IN_PROGRESS checkpoint
    for it. Rejects the transition if it isn't allowed from the run's
    current state.
    """
    run = await get_run(session, run_id)

    if stage not in _ALLOWED_TRANSITIONS.get(run.current_state, set()):
        raise InvalidStateTransitionError(run.current_state, stage)

    checkpoint = StageCheckpoint(
        run_id=run.id,
        stage=stage,
        status=CheckpointStatus.IN_PROGRESS,
    )
    session.add(checkpoint)
    run.current_state = stage

    await session.flush()
    return checkpoint


async def complete_stage(
    session: AsyncSession,
    run_id: uuid.UUID,
    stage: WorkflowState,
    output_data: dict | None = None,
) -> StageCheckpoint:
    """Mark a stage's checkpoint durably completed and persist its output.

    Requires an existing IN_PROGRESS checkpoint for (run_id, stage) — there
    is no path that lets a stage be marked complete without having been
    started first, which is what keeps "never bluffs" true at this layer.
    """
    checkpoint = await _in_progress_checkpoint(session, run_id, stage)

    checkpoint.status = CheckpointStatus.COMPLETED
    checkpoint.output_data = output_data
    checkpoint.completed_at = _now()

    run = await get_run(session, run_id)
    if stage == WorkflowState.PROCESSING:
        # PROCESSING completing is what makes the run COMPLETED — COMPLETED
        # itself has no separate work/checkpoint in this minimal sequence,
        # it is the resulting label, per the 3-state example in this batch.
        run.current_state = WorkflowState.COMPLETED

    await session.flush()
    return checkpoint


async def fail_stage(
    session: AsyncSession,
    run_id: uuid.UUID,
    stage: WorkflowState,
    error_message: str,
) -> StageCheckpoint:
    """Mark a stage's checkpoint durably failed and move the run to FAILED."""
    checkpoint = await _in_progress_checkpoint(session, run_id, stage)

    checkpoint.status = CheckpointStatus.FAILED
    checkpoint.error_message = error_message
    checkpoint.completed_at = _now()

    run = await get_run(session, run_id)
    run.current_state = WorkflowState.FAILED

    await session.flush()
    return checkpoint


async def _in_progress_checkpoint(
    session: AsyncSession, run_id: uuid.UUID, stage: WorkflowState
) -> StageCheckpoint:
    result = await session.execute(
        select(StageCheckpoint).where(
            StageCheckpoint.run_id == run_id,
            StageCheckpoint.stage == stage,
            StageCheckpoint.status == CheckpointStatus.IN_PROGRESS,
        )
    )
    checkpoint = result.scalar_one_or_none()
    if checkpoint is None:
        raise StageNotInProgressError(run_id, stage)
    return checkpoint


async def get_resume_stage(session: AsyncSession, run_id: uuid.UUID) -> WorkflowState:
    """Determine what stage a caller should run next, reading only durable
    state — never any in-memory flag.

    - If the run is already COMPLETED or FAILED, returns that terminal
      state (nothing left to resume).
    - If the latest checkpoint is IN_PROGRESS, returns that same stage —
      it was interrupted before finishing and must be resumed, not
      advanced past.
    - If the latest checkpoint is COMPLETED, returns the next stage in
      sequence — the completed stage is never returned again.
    """
    run = await get_run(session, run_id)

    if run.current_state in (WorkflowState.COMPLETED, WorkflowState.FAILED):
        return run.current_state

    latest = await _latest_checkpoint(session, run_id)
    if latest is None:
        return _STAGE_SEQUENCE[0]

    if latest.status == CheckpointStatus.IN_PROGRESS:
        return latest.stage

    # latest.status == COMPLETED: advance to the next stage in sequence.
    current_index = _STAGE_SEQUENCE.index(latest.stage)
    return _STAGE_SEQUENCE[current_index + 1]


def _now():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc)
