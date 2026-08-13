"""LangGraph workflow skeleton over the durable Batch 6 backbone, now
running Movement 1's real analytical stages.

LangGraph here is purely a sequencing layer. It decides which node runs
next; workflow_service remains the only thing that validates transitions
and persists them — this module never touches WorkflowRun/StageCheckpoint
rows directly, and never encodes its own notion of "what comes after what"
beyond calling get_resume_stage. The actual analysis (classification,
extraction, consolidation, conflict scanning) lives in app.agents and is
deterministic, no LLM involved yet.

Deliberately no LangGraph checkpointer is configured. Durability is already
handled by Postgres via workflow_service (proved in Batch 6); adding a
second, LangGraph-native persistence mechanism on top would duplicate
exactly the logic that already exists. Every node re-derives what to do
from persisted state on each invocation — including nodes reading a prior
stage's output via get_completed_checkpoint rather than anything carried in
LangGraph's own state dict, since that dict does not survive a restart and
must never be relied on as if it did.

Five real nodes now exist: intake, classifying, extracting, consolidating,
conflict_scan. This mirrors WorkflowState exactly. Movement 2's states
(VALIDATING, AWAITING_HUMAN_REVIEW, ...) do not exist in workflow_service
yet, so no nodes for them exist here either.
"""
import uuid
from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.classifier import classify_document
from app.agents.conflict_scanner import scan_conflicts
from app.agents.consolidator import consolidate
from app.agents.extractor import ExtractedClaim, extract_claims
from app.agents.rule_validator import RuleInput, validate
from app.orchestration.workflow_service import (
    complete_stage,
    create_run,
    fail_stage,
    find_in_progress_checkpoint,
    get_completed_checkpoint,
    get_resume_stage,
    get_rules_for_rule_set,
    record_finding,
    start_stage,
)
from app.persistence.models import Document, FindingType, RuleSet, WorkflowState
from app.storage.document_storage import DocumentInput, read_document


class SentinelGraphState(TypedDict, total=False):
    """Data carried between nodes for a single graph invocation.

    Only `run_id` matters across a restart — a resumed invocation is called
    with the same run_id it was given before, and everything else here is
    scratch space for that one invocation, not itself durable. `documents`
    is only meaningful when starting a brand new run (no run_id yet).
    `rule_set_id` is only meaningful the one time a COMPLETED run is handed
    off into Movement 2 (see route_entry) — once RULE_VALIDATION_PENDING
    actually starts, which rule set was chosen is read back durably from
    its own checkpoint, not from this dict, on every subsequent resume. No
    analytical results (claims, classifications, findings...) are carried
    here — every node that needs a prior stage's output reads it durably
    via get_completed_checkpoint instead.
    """

    run_id: str
    documents: list[DocumentInput]
    rule_set_id: str
    error: str | None


def _serialize(dataclass_obj) -> dict:
    """Plain-dict form of a frozen dataclass, enum values reduced to their
    string value, for storage in a JSONB checkpoint output_data column."""
    from dataclasses import asdict

    result = asdict(dataclass_obj)
    for key, value in result.items():
        if hasattr(value, "value") and not isinstance(value, (str, int, float, bool, type(None))):
            result[key] = value.value
    return result


async def _run_documents(session: AsyncSession, run_id: uuid.UUID) -> list[Document]:
    result = await session.execute(select(Document).where(Document.run_id == run_id))
    return list(result.scalars().all())


def _load_claims_from_checkpoint(checkpoint) -> list[ExtractedClaim]:
    return [ExtractedClaim(**claim_dict) for claim_dict in checkpoint.output_data["claims"]]


async def _intake_node(state: SentinelGraphState, *, session: AsyncSession) -> SentinelGraphState:
    """Create the run and register its documents.

    Only ever reached for a brand new run (see `route_entry` below) — a
    resumed invocation always already has a run_id, so this node is skipped
    by routing, not by a check inside the node itself.
    """
    run = await create_run(session, documents=state.get("documents", []))
    await session.commit()
    return {**state, "run_id": str(run.id)}


async def _classifying_node(
    state: SentinelGraphState, *, session: AsyncSession
) -> SentinelGraphState:
    """Classify every document registered against this run."""
    run_id = uuid.UUID(state["run_id"])
    stage = WorkflowState.CLASSIFYING

    try:
        if await find_in_progress_checkpoint(session, run_id, stage) is None:
            await start_stage(session, run_id, stage)
            await session.commit()

        documents = await _run_documents(session, run_id)
        results = []
        for document in documents:
            content_bytes = read_document(document.storage_key)
            text = content_bytes.decode("utf-8", errors="replace")
            results.append(classify_document(str(document.id), text))

        output = {"classifications": [_serialize(r) for r in results]}
        await complete_stage(session, run_id, stage, output_data=output)
        await session.commit()
    except Exception as exc:  # noqa: BLE001 - must be durably recorded, not swallowed
        await fail_stage(session, run_id, stage, error_message=str(exc))
        await session.commit()
        return {**state, "error": str(exc)}

    return state


async def _extracting_node(
    state: SentinelGraphState, *, session: AsyncSession
) -> SentinelGraphState:
    """Extract claims from every document registered against this run.

    Independent of classification's result — this deterministic extractor
    doesn't branch on document type, so it doesn't need to wait on or read
    CLASSIFYING's output at all.
    """
    run_id = uuid.UUID(state["run_id"])
    stage = WorkflowState.EXTRACTING

    try:
        if await find_in_progress_checkpoint(session, run_id, stage) is None:
            await start_stage(session, run_id, stage)
            await session.commit()

        documents = await _run_documents(session, run_id)
        all_claims: list[ExtractedClaim] = []
        for document in documents:
            content_bytes = read_document(document.storage_key)
            text = content_bytes.decode("utf-8", errors="replace")
            all_claims.extend(extract_claims(str(document.id), text))

        output = {"claims": [_serialize(c) for c in all_claims]}
        await complete_stage(session, run_id, stage, output_data=output)
        await session.commit()
    except Exception as exc:  # noqa: BLE001
        await fail_stage(session, run_id, stage, error_message=str(exc))
        await session.commit()
        return {**state, "error": str(exc)}

    return state


async def _consolidating_node(
    state: SentinelGraphState, *, session: AsyncSession
) -> SentinelGraphState:
    """Build the grounded deliverable from EXTRACTING's durable output.

    Reads EXTRACTING's checkpoint directly rather than anything carried in
    graph state — this is the mechanism, not just an intention, behind
    "restart/resume does not redo completed analytical stages": a fresh
    process resuming here has no in-memory claims at all, and still works,
    because the claims it needs were never anywhere but Postgres.
    """
    run_id = uuid.UUID(state["run_id"])
    stage = WorkflowState.CONSOLIDATING

    try:
        if await find_in_progress_checkpoint(session, run_id, stage) is None:
            await start_stage(session, run_id, stage)
            await session.commit()

        extracting_checkpoint = await get_completed_checkpoint(
            session, run_id, WorkflowState.EXTRACTING
        )
        claims = _load_claims_from_checkpoint(extracting_checkpoint)
        statements = consolidate(claims)

        output = {"statements": [_serialize(s) for s in statements]}
        await complete_stage(session, run_id, stage, output_data=output)
        await session.commit()
    except Exception as exc:  # noqa: BLE001
        await fail_stage(session, run_id, stage, error_message=str(exc))
        await session.commit()
        return {**state, "error": str(exc)}

    return state


async def _conflict_scan_node(
    state: SentinelGraphState, *, session: AsyncSession
) -> SentinelGraphState:
    """Scan EXTRACTING's durable claims for cross-document disagreements.

    Same durable-read pattern as consolidating: reads EXTRACTING's
    checkpoint directly, not anything held in graph state.
    """
    run_id = uuid.UUID(state["run_id"])
    stage = WorkflowState.CONFLICT_SCAN

    try:
        if await find_in_progress_checkpoint(session, run_id, stage) is None:
            await start_stage(session, run_id, stage)
            await session.commit()

        extracting_checkpoint = await get_completed_checkpoint(
            session, run_id, WorkflowState.EXTRACTING
        )
        claims = _load_claims_from_checkpoint(extracting_checkpoint)
        conflicts = scan_conflicts(claims)

        output = {"conflicts": [_serialize(c) for c in conflicts]}
        # Completing CONFLICT_SCAN is what moves the run to COMPLETED
        # (see workflow_service.complete_stage) — nothing extra needed here.
        await complete_stage(session, run_id, stage, output_data=output)
        await session.commit()
    except Exception as exc:  # noqa: BLE001
        await fail_stage(session, run_id, stage, error_message=str(exc))
        await session.commit()
        return {**state, "error": str(exc)}

    return state


async def _rule_validation_pending_node(
    state: SentinelGraphState, *, session: AsyncSession
) -> SentinelGraphState:
    """Durably record which rule set this run will be validated against.

    Mirrors intake's role for Movement 1: the "work" here is registering a
    choice, not analysis, so it completes atomically rather than having a
    meaningful IN_PROGRESS window. The chosen rule_set_id is stored in this
    checkpoint's own output_data so `_validating_node` (including on a
    resumed invocation, which never has `rule_set_id` in its state dict)
    can read it back durably rather than depending on graph state memory.
    """
    run_id = uuid.UUID(state["run_id"])
    stage = WorkflowState.RULE_VALIDATION_PENDING

    try:
        if await find_in_progress_checkpoint(session, run_id, stage) is None:
            await start_stage(session, run_id, stage)
            await session.commit()

        rule_set_id = state.get("rule_set_id")
        output = {"rule_set_id": rule_set_id}
        await complete_stage(session, run_id, stage, output_data=output)
        await session.commit()
    except Exception as exc:  # noqa: BLE001
        await fail_stage(session, run_id, stage, error_message=str(exc))
        await session.commit()
        return {**state, "error": str(exc)}

    return state


async def _validating_node(
    state: SentinelGraphState, *, session: AsyncSession
) -> SentinelGraphState:
    """Validate the grounded deliverable and source claims against the
    chosen rule set, persisting each finding as its own durable row.

    Reads RULE_VALIDATION_PENDING's checkpoint (for which rule set) and
    both CONSOLIDATING's and EXTRACTING's checkpoints (for the deliverable
    statements and the structured key/value claims a required_key_value
    rule needs) — all durably, none from graph state. A satisfied rule
    contributes nothing; only VIOLATION and CANNOT_EVALUATE findings are
    ever written, so "zero findings" stays a genuine, inspectable outcome.
    """
    run_id = uuid.UUID(state["run_id"])
    stage = WorkflowState.VALIDATING

    try:
        if await find_in_progress_checkpoint(session, run_id, stage) is None:
            await start_stage(session, run_id, stage)
            await session.commit()

        rvp_checkpoint = await get_completed_checkpoint(
            session, run_id, WorkflowState.RULE_VALIDATION_PENDING
        )
        rule_set_id = uuid.UUID(rvp_checkpoint.output_data["rule_set_id"])

        consolidating_checkpoint = await get_completed_checkpoint(
            session, run_id, WorkflowState.CONSOLIDATING
        )
        statements = consolidating_checkpoint.output_data["statements"]

        extracting_checkpoint = await get_completed_checkpoint(
            session, run_id, WorkflowState.EXTRACTING
        )
        claims = extracting_checkpoint.output_data["claims"]

        rules = await get_rules_for_rule_set(session, rule_set_id)
        rule_set = await session.get(RuleSet, rule_set_id)
        rule_inputs = [
            RuleInput(
                rule_id=str(rule.id),
                identifier=rule.identifier,
                rule_type=rule.rule_type,
                parameters=rule.parameters,
            )
            for rule in rules
        ]
        rules_by_id = {str(rule.id): rule for rule in rules}

        findings = validate(rule_inputs, statements, claims)

        for finding in findings:
            rule = rules_by_id[finding.rule_id]
            await record_finding(
                session,
                run_id,
                rule,
                rule_version=rule_set.version,
                finding_type=finding.finding_type,
                explanation=finding.explanation,
                evidence=finding.evidence,
                affected_claim_id=finding.affected_claim_id,
            )

        output = {
            "rule_set_id": str(rule_set_id),
            "finding_count": len(findings),
            "violation_count": sum(1 for f in findings if f.finding_type == FindingType.VIOLATION),
            "cannot_evaluate_count": sum(
                1 for f in findings if f.finding_type == FindingType.CANNOT_EVALUATE
            ),
        }
        # Completing VALIDATING is what moves the run to
        # AWAITING_HUMAN_REVIEW (see workflow_service.complete_stage).
        await complete_stage(session, run_id, stage, output_data=output)
        await session.commit()
    except Exception as exc:  # noqa: BLE001
        await fail_stage(session, run_id, stage, error_message=str(exc))
        await session.commit()
        return {**state, "error": str(exc)}

    return state


_STAGE_TO_NODE_NAME: dict[WorkflowState, str] = {
    WorkflowState.CLASSIFYING: "classifying",
    WorkflowState.EXTRACTING: "extracting",
    WorkflowState.CONSOLIDATING: "consolidating",
    WorkflowState.CONFLICT_SCAN: "conflict_scan",
    WorkflowState.RULE_VALIDATION_PENDING: "rule_validation_pending",
    WorkflowState.VALIDATING: "validating",
}


def build_graph(session: AsyncSession) -> CompiledStateGraph:
    """Build and compile the graph, bound to one caller-owned session.

    The session is captured via closure rather than threaded through
    LangGraph's config/state machinery — keeps node signatures the standard
    `(state) -> state` shape.
    """

    async def intake(state: SentinelGraphState) -> SentinelGraphState:
        return await _intake_node(state, session=session)

    async def classifying(state: SentinelGraphState) -> SentinelGraphState:
        return await _classifying_node(state, session=session)

    async def extracting(state: SentinelGraphState) -> SentinelGraphState:
        return await _extracting_node(state, session=session)

    async def consolidating(state: SentinelGraphState) -> SentinelGraphState:
        return await _consolidating_node(state, session=session)

    async def conflict_scan(state: SentinelGraphState) -> SentinelGraphState:
        return await _conflict_scan_node(state, session=session)

    async def rule_validation_pending(state: SentinelGraphState) -> SentinelGraphState:
        return await _rule_validation_pending_node(state, session=session)

    async def validating(state: SentinelGraphState) -> SentinelGraphState:
        return await _validating_node(state, session=session)

    async def route_entry(state: SentinelGraphState) -> str:
        """Decide where a graph invocation should actually start.

        - No run_id yet -> brand new run -> intake.
        - run_id given, resume_stage is COMPLETED -> Movement 1 already
          finished. If this invocation supplies a rule_set_id, hand off
          into Movement 2 (rule_validation_pending); otherwise end exactly
          as every pre-Batch-10 call already did.
        - run_id given, resume_stage is one of the analytical/validation
          stages -> skip everything already durably completed, go straight
          to that stage, whether that means starting it fresh or finishing
          an interrupted attempt (each node handles that distinction itself
          via find_in_progress_checkpoint).
        - run_id given, resume_stage is FAILED/AWAITING_HUMAN_REVIEW ->
          nothing to do, end immediately. Neither is auto-advanced.
        """
        if not state.get("run_id"):
            return "intake"

        run_id = uuid.UUID(state["run_id"])
        resume_stage = await get_resume_stage(session, run_id)

        if resume_stage == WorkflowState.COMPLETED:
            if state.get("rule_set_id"):
                return "rule_validation_pending"
            return "end"
        if resume_stage in (WorkflowState.FAILED, WorkflowState.AWAITING_HUMAN_REVIEW):
            return "end"
        return _STAGE_TO_NODE_NAME[resume_stage]

    graph = StateGraph(SentinelGraphState)
    graph.add_node("intake", intake)
    graph.add_node("classifying", classifying)
    graph.add_node("extracting", extracting)
    graph.add_node("consolidating", consolidating)
    graph.add_node("conflict_scan", conflict_scan)
    graph.add_node("rule_validation_pending", rule_validation_pending)
    graph.add_node("validating", validating)

    graph.add_conditional_edges(
        START,
        route_entry,
        {
            "intake": "intake",
            "classifying": "classifying",
            "extracting": "extracting",
            "consolidating": "consolidating",
            "conflict_scan": "conflict_scan",
            "rule_validation_pending": "rule_validation_pending",
            "validating": "validating",
            "end": END,
        },
    )
    graph.add_edge("intake", "classifying")
    graph.add_edge("classifying", "extracting")
    graph.add_edge("extracting", "consolidating")
    graph.add_edge("consolidating", "conflict_scan")
    graph.add_edge("conflict_scan", END)
    graph.add_edge("rule_validation_pending", "validating")
    graph.add_edge("validating", END)

    return graph.compile()


async def run_workflow(
    session: AsyncSession,
    *,
    run_id: uuid.UUID | None = None,
    documents: list[DocumentInput] | None = None,
    rule_set_id: uuid.UUID | None = None,
) -> SentinelGraphState:
    """Convenience entry point: build the graph and invoke it once.

    Pass `run_id` to resume an existing run (documents are ignored in that
    case, since intake is skipped). Pass `documents` with no `run_id` to
    start a brand new one. Pass `rule_set_id` together with `run_id` (for
    an already-COMPLETED run) to hand it off into Movement 2 validation.
    """
    graph = build_graph(session)
    initial_state: SentinelGraphState = {}
    if run_id is not None:
        initial_state["run_id"] = str(run_id)
    if documents is not None:
        initial_state["documents"] = documents
    if rule_set_id is not None:
        initial_state["rule_set_id"] = str(rule_set_id)

    return await graph.ainvoke(initial_state)
