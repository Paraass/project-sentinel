"""Persistent workflow models.

Minimum schema needed to represent a durable workflow run: the run itself,
the documents registered against it, and per-stage checkpoints proving what
has actually completed. No agent, finding, rule, watcher, or conflict tables
belong here — those are later batches.

Table creation uses `Base.metadata.create_all`, not a migration framework.
This is a deliberate choice for the current state of the repository: there
is exactly one schema revision (this one), no prior schema to migrate from,
and introducing Alembic now would add a tool with nothing yet to do. This
should be revisited the first time an existing table needs to change shape
after data exists in it — at that point a real migration tool earns its
place. Until then, `create_all`'s only actual behavior is "create the table
if it doesn't already exist," which is exactly what's needed.
"""
import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, Enum, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class WorkflowState(str, enum.Enum):
    """The workflow's overall run status.

    Deliberately just the four states this batch needs. Later Build Orders
    add the real movement-level states (CLASSIFYING, EXTRACTING,
    CONSOLIDATING, VALIDATING, AWAITING_HUMAN_REVIEW, etc.) from the
    approved behavioral architecture — this enum is the seed those will
    extend, not a placeholder that gets thrown away.
    """

    INTAKE_PENDING = "INTAKE_PENDING"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class CheckpointStatus(str, enum.Enum):
    """The status of a single stage's checkpoint row.

    Distinct from WorkflowState: WorkflowState is the run's overall status;
    CheckpointStatus is whether one specific stage's work is still in
    flight, durably finished, or durably failed. IN_PROGRESS with no
    matching COMPLETED/FAILED row is exactly the signal restart/resume
    logic looks for.
    """

    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class WorkflowRun(Base):
    """A single tracked run (the seed of what the behavioral architecture
    calls a "pile"). One row per run; current_state is the durable source of
    truth for where the run stands, never inferred from application memory.
    """

    __tablename__ = "workflow_runs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    current_state: Mapped[WorkflowState] = mapped_column(
        Enum(WorkflowState, name="workflow_state"),
        nullable=False,
        default=WorkflowState.INTAKE_PENDING,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )

    documents: Mapped[list["Document"]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )
    checkpoints: Mapped[list["StageCheckpoint"]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )


class Document(Base):
    """A document registered against a run at intake.

    Deliberately minimal: a filename/label is all this batch needs to prove
    a run can own documents. Classification, extraction, and content parsing
    belong to a later batch, not to this table.
    """

    __tablename__ = "documents"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workflow_runs.id", ondelete="CASCADE"), nullable=False
    )
    filename: Mapped[str] = mapped_column(String(512), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

    run: Mapped["WorkflowRun"] = relationship(back_populates="documents")


class StageCheckpoint(Base):
    """A durable record of one stage's lifecycle for one run.

    A row is created the moment a stage starts (status=IN_PROGRESS) and is
    updated in place when that stage finishes (status=COMPLETED or FAILED).
    This is what "completed stage outputs are persisted immediately" and
    "completed stages must not silently rerun" actually mean at the data
    layer: resume logic reads this table, nothing else, to decide what to
    do next.
    """

    __tablename__ = "stage_checkpoints"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workflow_runs.id", ondelete="CASCADE"), nullable=False
    )
    stage: Mapped[WorkflowState] = mapped_column(
        Enum(WorkflowState, name="workflow_state"), nullable=False
    )
    status: Mapped[CheckpointStatus] = mapped_column(
        Enum(CheckpointStatus, name="checkpoint_status"), nullable=False
    )
    output_data: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    run: Mapped["WorkflowRun"] = relationship(back_populates="checkpoints")


async def init_models(engine: AsyncEngine) -> None:
    """Create tables that don't already exist.

    Called once at application startup (see app/main.py). Safe to call
    repeatedly — `create_all` is a no-op for tables that already exist, so
    this does not disturb data across restarts.
    """
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
