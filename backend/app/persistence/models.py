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

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Integer, String, Text, text
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
    AWAITING_HUMAN_REVIEW are Movement 2 (Examines). REVIEW_CLOSED/
    COMMITTING/REPORT_COMMITTED/WATCHING are the shared review/commit
    lifecycle both movements feed into. NEW_DOCUMENT_DETECTED/
    IMPACT_ANALYSIS/FOCUSED_UPDATE_DRAFTING are Movement 3 (Stays Alive),
    all added in this batch.
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
    REVIEW_CLOSED = "REVIEW_CLOSED"
    COMMITTING = "COMMITTING"
    REPORT_COMMITTED = "REPORT_COMMITTED"
    WATCHING = "WATCHING"
    NEW_DOCUMENT_DETECTED = "NEW_DOCUMENT_DETECTED"
    IMPACT_ANALYSIS = "IMPACT_ANALYSIS"
    FOCUSED_UPDATE_DRAFTING = "FOCUSED_UPDATE_DRAFTING"
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


class ReviewDecision(str, enum.Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    DEFERRED = "DEFERRED"


class ReviewItem(Base):
    """One independently decidable item in the shared human-review queue.

    item_type is a small fixed vocabulary ("finding" from Movement 2,
    "proposed_change"/"conflict" from Movement 3) — deliberately not a
    generic pluggable framework. source_reference identifies which
    underlying object (a ValidationFinding's id, or a
    f"{baseline_claim_id}:{new_claim_id}" pair for a Movement 3 proposal)
    this item represents; content is a snapshot so a reviewer never needs
    to reconstruct context from elsewhere. Rejecting or deferring one item
    never touches any other row here — that independence is what makes
    "reject one, keep the rest" true at the data-shape level.
    """

    __tablename__ = "review_items"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workflow_runs.id", ondelete="CASCADE"), nullable=False
    )
    item_type: Mapped[str] = mapped_column(String(50), nullable=False)
    source_reference: Mapped[str] = mapped_column(String(255), nullable=False)
    content: Mapped[dict] = mapped_column(JSONB, nullable=False)
    decision: Mapped[ReviewDecision] = mapped_column(
        Enum(ReviewDecision, name="review_decision"),
        nullable=False,
        default=ReviewDecision.PENDING,
    )
    decision_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    decided_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


class Report(Base):
    """One committed, versioned snapshot of a run's grounded deliverable.

    is_current marks the latest version; older versions are never deleted
    or overwritten (immutable versions), only superseded. content carries
    each statement plus key/value where available, so a later Movement 3
    impact analysis has structured facts to compare against, not just
    prose text — a plain ConsolidatedStatement alone doesn't carry that.
    """

    __tablename__ = "reports"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workflow_runs.id", ondelete="CASCADE"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[dict] = mapped_column(JSONB, nullable=False)
    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


class ReportChangelogEntry(Base):
    """One durable record of what changed, when, and because of what,
    for one committed report version. This is the literal answer to
    "what changed / when / which source / which sections" required of
    every commit, Movement 1's initial one included.
    """

    __tablename__ = "report_changelog_entries"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workflow_runs.id", ondelete="CASCADE"), nullable=False
    )
    report_version: Mapped[int] = mapped_column(Integer, nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    source_document_ids: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    affected_claim_ids: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


# Every native Postgres enum type this schema declares, paired with its
# Python source of truth. `create_all` only creates a type if it doesn't
# exist yet — it never alters one that's already there, so a type created
# under an earlier version of a Python enum silently keeps its old values
# forever otherwise. This is the exact "first time an existing object
# needs to change shape" case flagged as the trigger for needing more than
# create_all — this reconciliation is that minimal next step, short of
# introducing a full migration framework.
_NATIVE_ENUMS: dict[str, type[enum.Enum]] = {
    "workflow_state": WorkflowState,
    "checkpoint_status": CheckpointStatus,
    "finding_type": FindingType,
    "review_decision": ReviewDecision,
}


async def _sync_enum_values(conn) -> None:
    """Add any enum value present in a Python enum but missing from its
    already-created Postgres type, without dropping, renaming, or
    reordering anything.

    Postgres supports `ALTER TYPE ... ADD VALUE` for exactly this add-only
    case (safe inside a transaction on PG12+). It does not support
    removing or renaming a value without recreating the type — that stays
    genuinely out of scope for this reconciliation, since it would risk
    existing rows referencing a value being removed. A leftover, unused
    stale value (like a value that no longer exists in the Python enum)
    is harmless and is deliberately left in place rather than dropped.
    """
    for pg_type_name, python_enum in _NATIVE_ENUMS.items():
        result = await conn.execute(
            text("SELECT enumlabel FROM pg_enum WHERE enumtypid = CAST(:type_name AS regtype)"),
            {"type_name": pg_type_name},
        )
        existing_values = {row[0] for row in result}

        for member in python_enum:
            if member.value not in existing_values:
                # Value names are interpolated, not bound as parameters —
                # ALTER TYPE is DDL and doesn't accept bind parameters for
                # the value literal in all drivers; safe here because both
                # pg_type_name and member.value come only from this
                # module's own hardcoded enum definitions, never from user
                # or request input.
                await conn.execute(
                    text(f"ALTER TYPE {pg_type_name} ADD VALUE IF NOT EXISTS '{member.value}'")
                )


async def init_models(engine: AsyncEngine) -> None:
    """Create tables/types that don't already exist, then reconcile any
    native enum type that already existed under an older set of values.

    Called once at application startup (see app/main.py). Safe to call
    repeatedly — both steps are no-ops once the schema already matches.
    """
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await _sync_enum_values(conn)
