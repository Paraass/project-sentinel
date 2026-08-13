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

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class WorkflowState(str, enum.Enum):
    """The workflow's overall run status.

    PROCESSING (the Batch 6/7 placeholder) was retired in Batch 9, replaced
    by the real Movement 1 stages it was always documented as a stand-in
    for. CLASSIFYING/EXTRACTING/CONSOLIDATING/CONFLICT_SCAN are Movement 1
    (Understands the Pile). RULE_VALIDATION_PENDING/VALIDATING/
    AWAITING_HUMAN_REVIEW are Movement 2 (Examines), added in this batch.
    AWAITING_HUMAN_REVIEW is deliberately NOT a success/terminal label the
    way COMPLETED is — it's "waiting," not "done." What happens after it
    (REVIEW_CLOSED, COMMITTING, ...) is a future batch's job to add.
    """

    INTAKE_PENDING = "INTAKE_PENDING"
    CLASSIFYING = "CLASSIFYING"
    EXTRACTING = "EXTRACTING"
    CONSOLIDATING = "CONSOLIDATING"
    CONFLICT_SCAN = "CONFLICT_SCAN"
    COMPLETED = "COMPLETED"
    RULE_VALIDATION_PENDING = "RULE_VALIDATION_PENDING"
    VALIDATING = "VALIDATING"
    AWAITING_HUMAN_REVIEW = "AWAITING_HUMAN_REVIEW"
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
    findings: Mapped[list["ValidationFinding"]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )


class Document(Base):
    """A document registered against a run at intake, with its actual
    content durably stored (see app/storage/document_storage.py) and
    enough metadata here to retrieve it reliably.

    storage_key and content_hash are kept as separate columns even though
    the current filesystem storage backend derives one from the other
    (storage_key IS the content hash) — the two are conceptually distinct
    (one is "where," the other is "what"), and a future storage backend
    (e.g. object storage with its own key scheme) could make that
    distinction real. Classification, extraction, and content parsing
    still belong to a later batch, not to this table.
    """

    __tablename__ = "documents"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workflow_runs.id", ondelete="CASCADE"), nullable=False
    )
    filename: Mapped[str] = mapped_column(String(512), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(255), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(nullable=False)
    content_type: Mapped[str] = mapped_column(String(255), nullable=False)
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


class RuleSet(Base):
    """A named, versioned collection of rules to validate a run's grounded
    deliverable against. Not tied to any one run — reusable across runs,
    unlike everything else in this file so far.
    """

    __tablename__ = "rule_sets"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    version: Mapped[str] = mapped_column(String(50), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

    rules: Mapped[list["Rule"]] = relationship(
        back_populates="rule_set", cascade="all, delete-orphan"
    )


class Rule(Base):
    """One rule within a RuleSet.

    rule_type + parameters is the rule as DATA — a small, fixed set of
    checkable rule_type "verbs" live in app.agents.rule_validator; which
    keyword/key/allowed-values to check for a given row is pure parameter
    data, never a new Python branch. description is human-readable only;
    it is never parsed or acted on by the validator.
    """

    __tablename__ = "rules"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    rule_set_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("rule_sets.id", ondelete="CASCADE"), nullable=False
    )
    identifier: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    rule_type: Mapped[str] = mapped_column(String(50), nullable=False)
    parameters: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    order_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

    rule_set: Mapped["RuleSet"] = relationship(back_populates="rules")


class FindingType(str, enum.Enum):
    VIOLATION = "VIOLATION"
    CANNOT_EVALUATE = "CANNOT_EVALUATE"


class ValidationFinding(Base):
    """One durable, independently identifiable validation result.

    Only VIOLATION and CANNOT_EVALUATE rows exist — a satisfied rule
    produces no row at all, which is what makes "zero findings" a genuine,
    inspectable outcome rather than a count of hidden "passed" rows.
    rule_version is denormalized from RuleSet.version at the moment
    validation ran, so a finding's meaning stays fixed even if the rule
    set is edited or re-versioned later — the finding always says which
    version actually produced it, not whatever the current version is.
    """

    __tablename__ = "validation_findings"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workflow_runs.id", ondelete="CASCADE"), nullable=False
    )
    rule_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("rules.id"), nullable=False
    )
    rule_set_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("rule_sets.id"), nullable=False
    )
    rule_version: Mapped[str] = mapped_column(String(50), nullable=False)
    finding_type: Mapped[FindingType] = mapped_column(
        Enum(FindingType, name="finding_type"), nullable=False
    )
    explanation: Mapped[str] = mapped_column(Text, nullable=False)
    evidence: Mapped[str] = mapped_column(Text, nullable=False)
    affected_claim_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

    run: Mapped["WorkflowRun"] = relationship(back_populates="findings")


async def init_models(engine: AsyncEngine) -> None:
    """Create tables that don't already exist.

    Called once at application startup (see app/main.py). Safe to call
    repeatedly — `create_all` is a no-op for tables that already exist, so
    this does not disturb data across restarts.
    """
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
