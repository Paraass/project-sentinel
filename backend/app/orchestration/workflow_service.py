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
    FindingType,
    Report,
    ReportChangelogEntry,
    ReviewDecision,
    ReviewItem,
    Rule,
    RuleSet,
    StageCheckpoint,
    ValidationFinding,
    WorkflowRun,
    WorkflowState,
)
from app.storage.document_storage import DocumentInput, save_document

# Movement 1 (Understands the Pile), Movement 2 (Examines), the shared
# review/commit lifecycle, and Movement 3 (Stays Alive)'s real stage
# sequence, replacing the Batch 6/7 placeholder
# (INTAKE_PENDING -> PROCESSING -> COMPLETED).
_ALLOWED_TRANSITIONS: dict[WorkflowState, set[WorkflowState]] = {
    WorkflowState.INTAKE_PENDING: {WorkflowState.CLASSIFYING},
    WorkflowState.CLASSIFYING: {WorkflowState.EXTRACTING, WorkflowState.FAILED},
    WorkflowState.EXTRACTING: {WorkflowState.CONSOLIDATING, WorkflowState.FAILED},
    WorkflowState.CONSOLIDATING: {WorkflowState.CONFLICT_SCAN, WorkflowState.FAILED},
    WorkflowState.CONFLICT_SCAN: {WorkflowState.COMPLETED, WorkflowState.FAILED},
    # COMPLETED is Movement 1's own terminal label (unchanged from Batch 9)
    # *and* now also a legitimate starting point for Movement 2, entered
    # only when a caller explicitly supplies a rule set — see graph.py's
    # route_entry. Nothing about Movement-1-only runs changes: with no rule
    # set requested, a run simply stays at COMPLETED, exactly as before.
    WorkflowState.COMPLETED: {WorkflowState.RULE_VALIDATION_PENDING},
    WorkflowState.RULE_VALIDATION_PENDING: {WorkflowState.VALIDATING},
    WorkflowState.VALIDATING: {WorkflowState.AWAITING_HUMAN_REVIEW, WorkflowState.FAILED},
    # AWAITING_HUMAN_REVIEW only advances on an EXPLICIT close request (see
    # graph.py's route_entry) — never automatically, per Batch 11's
    # "require an explicit close operation" requirement.
    WorkflowState.AWAITING_HUMAN_REVIEW: {WorkflowState.REVIEW_CLOSED},
    WorkflowState.REVIEW_CLOSED: {WorkflowState.COMMITTING},
    WorkflowState.COMMITTING: {WorkflowState.REPORT_COMMITTED, WorkflowState.FAILED},
    WorkflowState.REPORT_COMMITTED: {WorkflowState.WATCHING},
    # WATCHING, like COMPLETED and AWAITING_HUMAN_REVIEW, only advances when
    # a caller explicitly supplies a new document — see route_entry.
    WorkflowState.WATCHING: {WorkflowState.NEW_DOCUMENT_DETECTED},
    WorkflowState.NEW_DOCUMENT_DETECTED: {WorkflowState.IMPACT_ANALYSIS, WorkflowState.FAILED},
    WorkflowState.IMPACT_ANALYSIS: {WorkflowState.FOCUSED_UPDATE_DRAFTING, WorkflowState.FAILED},
    # FOCUSED_UPDATE_DRAFTING feeds back into the SAME shared review queue
    # Movement 2 uses — not a second approval mechanism.
    WorkflowState.FOCUSED_UPDATE_DRAFTING: {
        WorkflowState.AWAITING_HUMAN_REVIEW,
        WorkflowState.FAILED,
    },
    WorkflowState.FAILED: set(),
}

# Ordered so "the next stage after X" can be looked up positionally.
_STAGE_SEQUENCE: list[WorkflowState] = [
    WorkflowState.INTAKE_PENDING,
    WorkflowState.CLASSIFYING,
    WorkflowState.EXTRACTING,
    WorkflowState.CONSOLIDATING,
    WorkflowState.CONFLICT_SCAN,
    WorkflowState.COMPLETED,
    WorkflowState.RULE_VALIDATION_PENDING,
    WorkflowState.VALIDATING,
    WorkflowState.AWAITING_HUMAN_REVIEW,
    WorkflowState.REVIEW_CLOSED,
    WorkflowState.COMMITTING,
    WorkflowState.REPORT_COMMITTED,
    WorkflowState.WATCHING,
    WorkflowState.NEW_DOCUMENT_DETECTED,
    WorkflowState.IMPACT_ANALYSIS,
    WorkflowState.FOCUSED_UPDATE_DRAFTING,
]

# Explicit map of "completing this stage sets run.current_state to this
# terminal-for-now label" pairs. There are now three independent segments:
# Movement 1 ends at COMPLETED; Movement 2 (and Movement 3, feeding back
# into the same queue) ends at AWAITING_HUMAN_REVIEW; COMMITTING ends at
# REPORT_COMMITTED. Named explicitly per stage rather than inferred from
# list position.
_TERMINAL_TRANSITIONS: dict[WorkflowState, WorkflowState] = {
    WorkflowState.CONFLICT_SCAN: WorkflowState.COMPLETED,
    WorkflowState.VALIDATING: WorkflowState.AWAITING_HUMAN_REVIEW,
    WorkflowState.FOCUSED_UPDATE_DRAFTING: WorkflowState.AWAITING_HUMAN_REVIEW,
    WorkflowState.COMMITTING: WorkflowState.REPORT_COMMITTED,
}


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
    documents: list[DocumentInput],
    name: str | None = None,
) -> WorkflowRun:
    """Create a new run, durably store each document's actual content, and
    register the resulting rows, then durably complete the INTAKE_PENDING
    stage.

    Content is written to disk (via app.storage.document_storage) before
    the Document row referencing it is added, so a row is never persisted
    pointing at content that doesn't actually exist on disk. Intake's
    "work" happens atomically here, so it is recorded as an
    already-completed checkpoint rather than an IN_PROGRESS one — there is
    no partial-intake state this batch needs to model, unlike the analytical
    stages that follow, where a real interruption risk exists.
    """
    run = WorkflowRun(name=name, current_state=WorkflowState.INTAKE_PENDING)
    session.add(run)
    await session.flush()  # populate run.id before children reference it

    for document_input in documents:
        stored = save_document(document_input)
        session.add(
            Document(
                run_id=run.id,
                filename=document_input.filename,
                storage_key=stored.storage_key,
                content_hash=stored.content_hash,
                size_bytes=stored.size,
                content_type=stored.content_type,
            )
        )

    session.add(
        StageCheckpoint(
            run_id=run.id,
            stage=WorkflowState.INTAKE_PENDING,
            status=CheckpointStatus.COMPLETED,
        )
    )

    await session.flush()
    return run


async def add_document_to_run(
    session: AsyncSession, run_id: uuid.UUID, document_input: DocumentInput
) -> Document:
    """Register one additional document against an already-processed run —
    Movement 3's scoped intake.

    Unlike create_run, this never touches WorkflowRun.current_state or
    creates an INTAKE_PENDING checkpoint; the run already has one, and
    existing documents are never touched or reprocessed. Reuses the exact
    same content-addressed storage path as the original cold-start intake,
    so there is only ever one way a document's bytes get durably stored.
    """
    stored = save_document(document_input)
    document = Document(
        run_id=run_id,
        filename=document_input.filename,
        storage_key=stored.storage_key,
        content_hash=stored.content_hash,
        size_bytes=stored.size,
        content_type=stored.content_type,
    )
    session.add(document)
    await session.flush()
    return document


async def get_document(session: AsyncSession, document_id: uuid.UUID) -> Document:
    """Load a single document by id."""
    document = await session.get(Document, document_id)
    if document is None:
        raise WorkflowError(f"No document found for id {document_id}")
    return document


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
    session: AsyncSession,
    run_id: uuid.UUID,
    stage: WorkflowState,
    initial_output_data: dict | None = None,
) -> StageCheckpoint:
    """Transition a run into `stage` and persist an IN_PROGRESS checkpoint
    for it. Rejects the transition if it isn't allowed from the run's
    current state.

    `initial_output_data`, if given, is durably recorded on the checkpoint
    immediately — before any of the stage's real work runs — for the rare
    case where a node needs to remember something (e.g. which document it
    is processing) that must survive a crash occurring before that node's
    own work finishes. This is different from complete_stage's
    output_data, which is the stage's actual result; this is just enough
    context for a resumed attempt to pick up where a crashed one left off.
    """
    run = await get_run(session, run_id)

    if stage not in _ALLOWED_TRANSITIONS.get(run.current_state, set()):
        raise InvalidStateTransitionError(run.current_state, stage)

    checkpoint = StageCheckpoint(
        run_id=run.id,
        stage=stage,
        status=CheckpointStatus.IN_PROGRESS,
        output_data=initial_output_data,
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
    if stage in _TERMINAL_TRANSITIONS:
        # Completing certain stages sets a new terminal-for-now label on
        # the run — COMPLETED after CONFLICT_SCAN, AWAITING_HUMAN_REVIEW
        # after VALIDATING. Neither label has its own checkpoint; each is
        # the resulting status, not a stage with its own work.
        run.current_state = _TERMINAL_TRANSITIONS[stage]

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


async def find_in_progress_checkpoint(
    session: AsyncSession, run_id: uuid.UUID, stage: WorkflowState
) -> StageCheckpoint | None:
    """Return the IN_PROGRESS checkpoint for (run_id, stage), or None.

    A pure read — unlike `_in_progress_checkpoint` below, it never raises.
    This is what lets a caller (a LangGraph node, in Batch 7) tell the
    difference between "this stage was already started and needs
    resuming" and "this stage has never been started," without needing to
    catch an exception to find out. complete_stage/fail_stage still use the
    raising variant below, since for them a missing checkpoint genuinely is
    an error condition, not a branch to route on.
    """
    result = await session.execute(
        select(StageCheckpoint).where(
            StageCheckpoint.run_id == run_id,
            StageCheckpoint.stage == stage,
            StageCheckpoint.status == CheckpointStatus.IN_PROGRESS,
        )
    )
    return result.scalar_one_or_none()


async def get_completed_checkpoint(
    session: AsyncSession, run_id: uuid.UUID, stage: WorkflowState
) -> StageCheckpoint | None:
    """Return the COMPLETED checkpoint for (run_id, stage), or None.

    This is how a later stage reads an earlier stage's durable output —
    e.g. CONSOLIDATING reading what EXTRACTING produced. It reads only from
    Postgres, never from anything a caller might be holding in memory, which
    is what makes "restart/resume does not redo completed analytical
    stages" actually true rather than assumed: a fresh process calling this
    gets the exact same answer a long-running one would.
    """
    result = await session.execute(
        select(StageCheckpoint).where(
            StageCheckpoint.run_id == run_id,
            StageCheckpoint.stage == stage,
            StageCheckpoint.status == CheckpointStatus.COMPLETED,
        )
    )
    return result.scalar_one_or_none()


async def _in_progress_checkpoint(
    session: AsyncSession, run_id: uuid.UUID, stage: WorkflowState
) -> StageCheckpoint:
    checkpoint = await find_in_progress_checkpoint(session, run_id, stage)
    if checkpoint is None:
        raise StageNotInProgressError(run_id, stage)
    return checkpoint


async def get_resume_stage(session: AsyncSession, run_id: uuid.UUID) -> WorkflowState:
    """Determine what stage a caller should run next, reading only durable
    state — never any in-memory flag.

    - If the run is already COMPLETED, FAILED, or AWAITING_HUMAN_REVIEW,
      returns that state directly (nothing left to auto-resume — COMPLETED
      may still be a legitimate entry point into Movement 2, but that
      decision belongs to the caller, e.g. graph.py's route_entry, not to
      this generic lookup).
    - If the latest checkpoint is IN_PROGRESS, returns that same stage —
      it was interrupted before finishing and must be resumed, not
      advanced past.
    - If the latest checkpoint is COMPLETED, returns the next stage in
      sequence — the completed stage is never returned again.
    """
    run = await get_run(session, run_id)

    if run.current_state in (
        WorkflowState.COMPLETED,
        WorkflowState.FAILED,
        WorkflowState.AWAITING_HUMAN_REVIEW,
        WorkflowState.WATCHING,
    ):
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


async def create_rule_set(
    session: AsyncSession,
    name: str,
    version: str,
    rules: list[dict],
) -> RuleSet:
    """Create a RuleSet and its Rules in one call.

    Not tied to any run — rule sets are reusable across runs, unlike
    everything else this module creates. `rules` is plain data: each dict
    needs identifier, description, rule_type, parameters, and optionally
    order_index — this is exactly the "rules are data, not Python
    branching" requirement expressed at the call site too, not just inside
    the validator.
    """
    rule_set = RuleSet(name=name, version=version)
    session.add(rule_set)
    await session.flush()

    for index, rule_data in enumerate(rules):
        session.add(
            Rule(
                rule_set_id=rule_set.id,
                identifier=rule_data["identifier"],
                description=rule_data["description"],
                rule_type=rule_data["rule_type"],
                parameters=rule_data.get("parameters", {}),
                order_index=rule_data.get("order_index", index),
            )
        )

    await session.flush()
    return rule_set


async def get_rules_for_rule_set(session: AsyncSession, rule_set_id: uuid.UUID) -> list[Rule]:
    """Load a rule set's rules, in stable order."""
    result = await session.execute(
        select(Rule).where(Rule.rule_set_id == rule_set_id).order_by(Rule.order_index)
    )
    return list(result.scalars().all())


async def record_finding(
    session: AsyncSession,
    run_id: uuid.UUID,
    rule: Rule,
    rule_version: str,
    finding_type: FindingType,
    explanation: str,
    evidence: str,
    affected_claim_id: str | None = None,
) -> ValidationFinding:
    """Persist one validation finding as its own durable, independently
    identifiable row. Never called for a satisfied rule — a satisfied rule
    produces no row, which is what keeps "zero findings" a real, honest
    outcome rather than a count of hidden "passed" rows.
    """
    finding = ValidationFinding(
        run_id=run_id,
        rule_id=rule.id,
        rule_set_id=rule.rule_set_id,
        rule_version=rule_version,
        finding_type=finding_type,
        explanation=explanation,
        evidence=evidence,
        affected_claim_id=affected_claim_id,
    )
    session.add(finding)
    await session.flush()
    return finding


async def get_findings(session: AsyncSession, run_id: uuid.UUID) -> list[ValidationFinding]:
    """Load every finding recorded for a run."""
    result = await session.execute(
        select(ValidationFinding).where(ValidationFinding.run_id == run_id)
    )
    return list(result.scalars().all())


# --- Shared review/commit lifecycle helpers (Batch 11 Part A) --------------


async def create_review_item(
    session: AsyncSession,
    run_id: uuid.UUID,
    item_type: str,
    source_reference: str,
    content: dict,
) -> ReviewItem:
    """Add one independently decidable item to the review queue.

    Called by whichever stage produced something reviewable (VALIDATING
    for findings; FOCUSED_UPDATE_DRAFTING for proposed changes/conflicts)
    at the moment it completes, so every item awaiting AWAITING_HUMAN_REVIEW
    genuinely exists in the queue before a human — or a test — can decide
    on it.
    """
    item = ReviewItem(
        run_id=run_id,
        item_type=item_type,
        source_reference=source_reference,
        content=content,
    )
    session.add(item)
    await session.flush()
    return item


async def get_review_items(session: AsyncSession, run_id: uuid.UUID) -> list[ReviewItem]:
    """Load every review item for a run."""
    result = await session.execute(select(ReviewItem).where(ReviewItem.run_id == run_id))
    return list(result.scalars().all())


async def decide_review_item(
    session: AsyncSession,
    item_id: uuid.UUID,
    decision: ReviewDecision,
    decided_by: str,
    reason: str | None = None,
) -> ReviewItem:
    """Record an attributable decision on one review item.

    decided_by is required — a decision with no attribution isn't a
    decision this system can account for later. Deciding one item never
    touches any other row.
    """
    item = await session.get(ReviewItem, item_id)
    if item is None:
        raise WorkflowError(f"No review item found for id {item_id}")

    item.decision = decision
    item.decided_by = decided_by
    item.decision_reason = reason
    item.decided_at = _now()

    await session.flush()
    return item


async def get_current_report(session: AsyncSession, run_id: uuid.UUID) -> Report | None:
    """Load the current (is_current=True) committed report for a run, or
    None if nothing has been committed yet.
    """
    result = await session.execute(
        select(Report).where(Report.run_id == run_id, Report.is_current.is_(True))
    )
    return result.scalar_one_or_none()


async def get_report_version(
    session: AsyncSession, run_id: uuid.UUID, version: int
) -> Report | None:
    """Load a specific, possibly-superseded report version — proves older
    versions genuinely still exist rather than having been overwritten.
    """
    result = await session.execute(
        select(Report).where(Report.run_id == run_id, Report.version == version)
    )
    return result.scalar_one_or_none()


async def get_changelog(session: AsyncSession, run_id: uuid.UUID) -> list[ReportChangelogEntry]:
    """Load every changelog entry for a run, oldest first."""
    result = await session.execute(
        select(ReportChangelogEntry)
        .where(ReportChangelogEntry.run_id == run_id)
        .order_by(ReportChangelogEntry.report_version)
    )
    return list(result.scalars().all())
