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
from app.agents.impact_analyzer import analyze_impact
from app.agents.rule_validator import RuleInput, validate
from app.agents.update_drafter import draft_update
from app.orchestration.workflow_service import (
    add_document_to_run,
    complete_stage,
    create_review_item,
    create_run,
    fail_stage,
    find_in_progress_checkpoint,
    get_completed_checkpoint,
    get_current_report,
    get_document,
    get_resume_stage,
    get_review_items,
    get_rules_for_rule_set,
    record_finding,
    start_stage,
)
from app.persistence.models import (
    Document,
    FindingType,
    Report,
    ReportChangelogEntry,
    ReviewDecision,
    RuleSet,
    WorkflowState,
)
from app.storage.document_storage import DocumentInput, read_document


class SentinelGraphState(TypedDict, total=False):
    """Data carried between nodes for a single graph invocation.

    Only `run_id` matters across a restart — a resumed invocation is called
    with the same run_id it was given before, and everything else here is
    scratch space for that one invocation, not itself durable.

    - `documents` only matters starting a brand new run (no run_id yet).
    - `rule_set_id` only matters the one time a COMPLETED run is handed
      off into Movement 2 — once RULE_VALIDATION_PENDING starts, which
      rule set was chosen is read back durably from its own checkpoint.
    - `close_review` only matters the one time an AWAITING_HUMAN_REVIEW run
      is explicitly closed — this is the "explicit close operation"
      Batch 11 requires; without it, a run simply stays put.
    - `new_document` only matters the one time a WATCHING run is handed a
      new document — once NEW_DOCUMENT_DETECTED starts, which document was
      registered is read back durably from WATCHING's own checkpoint.

    No analytical results (claims, classifications, findings, proposals...)
    are carried here — every node that needs a prior stage's output reads
    it durably via get_completed_checkpoint instead.
    """

    run_id: str
    documents: list[DocumentInput]
    rule_set_id: str
    close_review: bool
    new_document: DocumentInput
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

        for conflict in conflicts:
            await create_review_item(
                session,
                run_id,
                item_type="conflict",
                source_reference=conflict.conflict_id,
                content=_serialize(conflict),
            )

        # Completing CONFLICT_SCAN is what moves the run to COMPLETED
        # (see workflow_service.complete_stage) — nothing extra needed here.
        output = {"conflicts": [_serialize(c) for c in conflicts]}
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
            finding_row = await record_finding(
                session,
                run_id,
                rule,
                rule_version=rule_set.version,
                finding_type=finding.finding_type,
                explanation=finding.explanation,
                evidence=finding.evidence,
                affected_claim_id=finding.affected_claim_id,
            )
            # Batch 11: every finding becomes a reviewable item so the
            # shared review queue has something real to decide on before
            # REVIEW_CLOSED is ever reached.
            await create_review_item(
                session,
                run_id,
                item_type="finding",
                source_reference=str(finding_row.id),
                content=_serialize(finding),
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


# --- Shared review/commit lifecycle nodes (Batch 11 Part A) -----------------


async def _review_closed_node(
    state: SentinelGraphState, *, session: AsyncSession
) -> SentinelGraphState:
    """Durably record that review was explicitly closed.

    This is the required "explicit close operation" — nothing auto-advances
    a run out of AWAITING_HUMAN_REVIEW without a caller setting
    `close_review` (see route_entry). This node does not decide anything
    itself; it snapshots how the queue stood at close time.
    """
    run_id = uuid.UUID(state["run_id"])
    stage = WorkflowState.REVIEW_CLOSED

    try:
        if await find_in_progress_checkpoint(session, run_id, stage) is None:
            await start_stage(session, run_id, stage)
            await session.commit()

        items = await get_review_items(session, run_id)
        output = {
            "item_count": len(items),
            "approved_count": sum(1 for i in items if i.decision == ReviewDecision.APPROVED),
            "rejected_count": sum(1 for i in items if i.decision == ReviewDecision.REJECTED),
            "deferred_count": sum(1 for i in items if i.decision == ReviewDecision.DEFERRED),
            "pending_count": sum(1 for i in items if i.decision == ReviewDecision.PENDING),
        }
        await complete_stage(session, run_id, stage, output_data=output)
        await session.commit()
    except Exception as exc:  # noqa: BLE001
        await fail_stage(session, run_id, stage, error_message=str(exc))
        await session.commit()
        return {**state, "error": str(exc)}

    return state


async def _committing_node(
    state: SentinelGraphState, *, session: AsyncSession
) -> SentinelGraphState:
    """Atomically write the next report version.

    First commit for a run: content is Movement 1's grounded deliverable
    (CONSOLIDATING's statements), enriched with key/value pulled from
    EXTRACTING's claims so a later Movement 3 impact analysis has
    structured facts to compare against — a bare ConsolidatedStatement
    alone doesn't carry that.

    Later commits: only review items of type "proposed_change" with an
    APPROVED decision are applied, replacing their baseline statement;
    every other statement is carried over from the current report
    byte-for-byte, never regenerated. Conflicts are never auto-applied,
    approved or not — they carry no single resolved value to apply.

    Writing the Report row, superseding the previous is_current row, and
    writing the changelog entry all happen in this one node's session,
    committed once at the end — either the whole version lands or none of
    it does, matching the same atomic-commit guarantee proven in Batch 6.
    """
    run_id = uuid.UUID(state["run_id"])
    stage = WorkflowState.COMMITTING

    try:
        if await find_in_progress_checkpoint(session, run_id, stage) is None:
            await start_stage(session, run_id, stage)
            await session.commit()

        current_report = await get_current_report(session, run_id)

        if current_report is None:
            consolidating_checkpoint = await get_completed_checkpoint(
                session, run_id, WorkflowState.CONSOLIDATING
            )
            extracting_checkpoint = await get_completed_checkpoint(
                session, run_id, WorkflowState.EXTRACTING
            )
            claims_by_id = {
                c["claim_id"]: c for c in extracting_checkpoint.output_data["claims"]
            }

            statements = []
            for statement in consolidating_checkpoint.output_data["statements"]:
                claim = claims_by_id.get(statement["claim_id"], {})
                statements.append(
                    {
                        **statement,
                        "key": claim.get("key"),
                        "value": claim.get("value"),
                    }
                )

            new_version = 1
            source_document_ids = sorted({s["document_id"] for s in statements})
            affected_claim_ids = [s["claim_id"] for s in statements]
            summary = f"Initial report created from {len(source_document_ids)} document(s)."
        else:
            focused_checkpoint = await get_completed_checkpoint(
                session, run_id, WorkflowState.FOCUSED_UPDATE_DRAFTING
            )
            proposed_changes = focused_checkpoint.output_data["proposed_changes"]
            proposed_by_ref = {
                f"{pc['baseline_claim_id']}:{pc['new_claim_id']}": pc for pc in proposed_changes
            }

            items = await get_review_items(session, run_id)
            approved_refs = {
                i.source_reference
                for i in items
                if i.item_type == "proposed_change" and i.decision == ReviewDecision.APPROVED
            }
            approved_changes_by_claim_id = {
                proposed_by_ref[ref]["baseline_claim_id"]: proposed_by_ref[ref]
                for ref in approved_refs
                if ref in proposed_by_ref
            }

            new_statements = []
            affected_claim_ids = []
            source_document_ids_set = set()
            for statement in current_report.content["statements"]:
                change = approved_changes_by_claim_id.get(statement["claim_id"])
                if change is not None:
                    new_statements.append(
                        {
                            "claim_id": statement["claim_id"],
                            "document_id": change["new_document_id"],
                            "text": change["proposed_text"],
                            "source_location": change["source_citation"],
                            "claim_type": change["new_claim_type"],
                            "key": change.get("key"),
                            "value": change.get("value"),
                        }
                    )
                    affected_claim_ids.append(statement["claim_id"])
                    source_document_ids_set.add(change["new_document_id"])
                else:
                    new_statements.append(statement)  # byte-identical carryover

            statements = new_statements
            new_version = current_report.version + 1
            summary = f"Incremental update: {len(affected_claim_ids)} section(s) changed."
            source_document_ids = sorted(source_document_ids_set)

            current_report.is_current = False

        report = Report(
            run_id=run_id,
            version=new_version,
            content={"statements": statements},
            is_current=True,
        )
        session.add(report)
        await session.flush()

        session.add(
            ReportChangelogEntry(
                run_id=run_id,
                report_version=new_version,
                summary=summary,
                source_document_ids=source_document_ids,
                affected_claim_ids=affected_claim_ids,
            )
        )

        output = {"report_version": new_version}
        # Completing COMMITTING is what moves the run to REPORT_COMMITTED
        # (see workflow_service.complete_stage).
        await complete_stage(session, run_id, stage, output_data=output)
        await session.commit()
    except Exception as exc:  # noqa: BLE001
        await fail_stage(session, run_id, stage, error_message=str(exc))
        await session.commit()
        return {**state, "error": str(exc)}

    return state


async def _watching_node(
    state: SentinelGraphState, *, session: AsyncSession
) -> SentinelGraphState:
    """Enter the long-lived steady state after a successful commit.

    Atomic, like INTAKE_PENDING/RULE_VALIDATION_PENDING — there is no real
    "work" here, just a durable label meaning "this run's report is
    committed and stable; a new document can now trigger Movement 3."
    """
    run_id = uuid.UUID(state["run_id"])
    stage = WorkflowState.WATCHING

    try:
        if await find_in_progress_checkpoint(session, run_id, stage) is None:
            await start_stage(session, run_id, stage)
            await session.commit()
        await complete_stage(session, run_id, stage, output_data={})
        await session.commit()
    except Exception as exc:  # noqa: BLE001
        await fail_stage(session, run_id, stage, error_message=str(exc))
        await session.commit()
        return {**state, "error": str(exc)}

    return state


# --- Movement 3 nodes (Batch 11 Part B) -------------------------------------


async def _new_document_detected_node(
    state: SentinelGraphState, *, session: AsyncSession
) -> SentinelGraphState:
    """Scoped intake, classification, and extraction for the ONE new
    document only.

    Registers the new document (add_document_to_run) directly from
    `state["new_document"]`, mirroring how RULE_VALIDATION_PENDING reads
    `rule_set_id` from state — this is the one invocation where that data
    is actually supplied. Existing documents are never touched, never
    re-read, never reclassified: this node's only document is the one it
    just registered.
    """
    run_id = uuid.UUID(state["run_id"])
    stage = WorkflowState.NEW_DOCUMENT_DETECTED

    try:
        if await find_in_progress_checkpoint(session, run_id, stage) is None:
            document = await add_document_to_run(session, run_id, state["new_document"])
            await session.commit()
            await start_stage(
                session, run_id, stage, initial_output_data={"document_id": str(document.id)}
            )
            await session.commit()
        else:
            # Resuming an interrupted attempt: the document was already
            # registered before the crash. Read it back from
            # NEW_DOCUMENT_DETECTED's own IN_PROGRESS checkpoint output —
            # written immediately below, before any further work, so a
            # crash after registration but before classify/extract still
            # leaves the document id durably recoverable.
            in_progress = await find_in_progress_checkpoint(session, run_id, stage)
            document_id = uuid.UUID(in_progress.output_data["document_id"])
            document = await get_document(session, document_id)

        content_bytes = read_document(document.storage_key)
        text = content_bytes.decode("utf-8", errors="replace")
        classification = classify_document(str(document.id), text)
        new_claims = extract_claims(str(document.id), text)

        output = {
            "document_id": str(document.id),
            "classification": _serialize(classification),
            "new_claims": [_serialize(c) for c in new_claims],
        }
        await complete_stage(session, run_id, stage, output_data=output)
        await session.commit()
    except Exception as exc:  # noqa: BLE001
        await fail_stage(session, run_id, stage, error_message=str(exc))
        await session.commit()
        return {**state, "error": str(exc)}

    return state


async def _impact_analysis_node(
    state: SentinelGraphState, *, session: AsyncSession
) -> SentinelGraphState:
    """Compare the new document's claims against the CURRENT COMMITTED
    REPORT — not against Movement 1's raw claims, and never against
    anything unapproved. Ambiguous relevance defaults to affected inside
    analyze_impact itself; this node just supplies durable inputs to it.
    """
    run_id = uuid.UUID(state["run_id"])
    stage = WorkflowState.IMPACT_ANALYSIS

    try:
        if await find_in_progress_checkpoint(session, run_id, stage) is None:
            await start_stage(session, run_id, stage)
            await session.commit()

        ndd_checkpoint = await get_completed_checkpoint(
            session, run_id, WorkflowState.NEW_DOCUMENT_DETECTED
        )
        new_claims = ndd_checkpoint.output_data["new_claims"]

        current_report = await get_current_report(session, run_id)
        baseline_statements = current_report.content["statements"]

        result = analyze_impact(new_claims, baseline_statements)

        output = {
            "affected_claim_ids": result.affected_claim_ids,
            "unaffected_claim_ids": result.unaffected_claim_ids,
            "triggers": result.triggers,
        }
        await complete_stage(session, run_id, stage, output_data=output)
        await session.commit()
    except Exception as exc:  # noqa: BLE001
        await fail_stage(session, run_id, stage, error_message=str(exc))
        await session.commit()
        return {**state, "error": str(exc)}

    return state


async def _focused_update_drafting_node(
    state: SentinelGraphState, *, session: AsyncSession
) -> SentinelGraphState:
    """Produce a diff-shaped proposal scoped only to affected sections, and
    file every proposed change / conflict as a reviewable item in the SAME
    shared queue Movement 2 uses — not a second approval mechanism.
    """
    run_id = uuid.UUID(state["run_id"])
    stage = WorkflowState.FOCUSED_UPDATE_DRAFTING

    try:
        if await find_in_progress_checkpoint(session, run_id, stage) is None:
            await start_stage(session, run_id, stage)
            await session.commit()

        impact_checkpoint = await get_completed_checkpoint(
            session, run_id, WorkflowState.IMPACT_ANALYSIS
        )
        affected_ids = impact_checkpoint.output_data["affected_claim_ids"]
        triggers = impact_checkpoint.output_data["triggers"]

        current_report = await get_current_report(session, run_id)
        baseline_by_id = {s["claim_id"]: s for s in current_report.content["statements"]}

        ndd_checkpoint = await get_completed_checkpoint(
            session, run_id, WorkflowState.NEW_DOCUMENT_DETECTED
        )
        new_claims_by_id = {
            c["claim_id"]: c for c in ndd_checkpoint.output_data["new_claims"]
        }

        result = draft_update(affected_ids, triggers, baseline_by_id, new_claims_by_id)

        for change in result.proposed_changes:
            await create_review_item(
                session,
                run_id,
                item_type="proposed_change",
                source_reference=f"{change.baseline_claim_id}:{change.new_claim_id}",
                content=_serialize(change),
            )
        for conflict in result.conflicts:
            await create_review_item(
                session,
                run_id,
                item_type="conflict",
                source_reference=f"conflict:{conflict.key}:{conflict.baseline_claim_id}:{conflict.new_claim_id}",
                content=_serialize(conflict),
            )

        output = {
            "proposed_changes": [_serialize(c) for c in result.proposed_changes],
            "conflicts": [_serialize(c) for c in result.conflicts],
        }
        # Completing FOCUSED_UPDATE_DRAFTING is what moves the run back to
        # AWAITING_HUMAN_REVIEW (see workflow_service.complete_stage) — the
        # same state Movement 2 uses, not a second one.
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
    WorkflowState.REVIEW_CLOSED: "review_closed",
    WorkflowState.COMMITTING: "committing",
    WorkflowState.NEW_DOCUMENT_DETECTED: "new_document_detected",
    WorkflowState.IMPACT_ANALYSIS: "impact_analysis",
    WorkflowState.FOCUSED_UPDATE_DRAFTING: "focused_update_drafting",
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

    async def review_closed(state: SentinelGraphState) -> SentinelGraphState:
        return await _review_closed_node(state, session=session)

    async def committing(state: SentinelGraphState) -> SentinelGraphState:
        return await _committing_node(state, session=session)

    async def watching(state: SentinelGraphState) -> SentinelGraphState:
        return await _watching_node(state, session=session)

    async def new_document_detected(state: SentinelGraphState) -> SentinelGraphState:
        return await _new_document_detected_node(state, session=session)

    async def impact_analysis(state: SentinelGraphState) -> SentinelGraphState:
        return await _impact_analysis_node(state, session=session)

    async def focused_update_drafting(state: SentinelGraphState) -> SentinelGraphState:
        return await _focused_update_drafting_node(state, session=session)

    async def route_entry(state: SentinelGraphState) -> str:
        """Decide where a graph invocation should actually start.

        - No run_id yet -> brand new run -> intake.
        - resume_stage COMPLETED -> Movement 1 done. rule_set_id given ->
          hand off into Movement 2; otherwise end, exactly as pre-Batch-10.
        - resume_stage AWAITING_HUMAN_REVIEW -> waiting on a human.
          close_review given -> the required explicit close -> proceed into
          the shared review/commit lifecycle; otherwise end. Never
          auto-advanced.
        - resume_stage WATCHING -> steady state after a commit.
          new_document given -> Movement 3's scoped incremental path;
          otherwise end. Never auto-advanced.
        - resume_stage is any other analytical/validation/commit/Movement-3
          stage -> skip everything already durably completed, go straight
          to it (each node handles started-vs-interrupted itself via
          find_in_progress_checkpoint).
        - resume_stage FAILED -> end immediately.
        """
        if not state.get("run_id"):
            return "intake"

        run_id = uuid.UUID(state["run_id"])
        resume_stage = await get_resume_stage(session, run_id)

        if resume_stage == WorkflowState.COMPLETED:
            return "rule_validation_pending" if state.get("rule_set_id") else "end"
        if resume_stage == WorkflowState.AWAITING_HUMAN_REVIEW:
            return "review_closed" if state.get("close_review") else "end"
        if resume_stage == WorkflowState.WATCHING:
            return "new_document_detected" if state.get("new_document") else "end"
        if resume_stage == WorkflowState.FAILED:
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
    graph.add_node("review_closed", review_closed)
    graph.add_node("committing", committing)
    graph.add_node("watching", watching)
    graph.add_node("new_document_detected", new_document_detected)
    graph.add_node("impact_analysis", impact_analysis)
    graph.add_node("focused_update_drafting", focused_update_drafting)

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
            "review_closed": "review_closed",
            "committing": "committing",
            "watching": "watching",
            "new_document_detected": "new_document_detected",
            "impact_analysis": "impact_analysis",
            "focused_update_drafting": "focused_update_drafting",
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
    # Shared review/commit chain: once explicitly closed, the remaining
    # mechanical steps (commit, then watch) proceed in the same invocation.
    graph.add_edge("review_closed", "committing")
    graph.add_edge("committing", "watching")
    graph.add_edge("watching", END)
    # Movement 3: once a new document is detected, the remaining analysis
    # steps proceed in the same invocation, ending back in the shared
    # review queue.
    graph.add_edge("new_document_detected", "impact_analysis")
    graph.add_edge("impact_analysis", "focused_update_drafting")
    graph.add_edge("focused_update_drafting", END)

    return graph.compile()


async def run_workflow(
    session: AsyncSession,
    *,
    run_id: uuid.UUID | None = None,
    documents: list[DocumentInput] | None = None,
    rule_set_id: uuid.UUID | None = None,
    close_review: bool = False,
    new_document: DocumentInput | None = None,
) -> SentinelGraphState:
    """Convenience entry point: build the graph and invoke it once.

    - `documents` with no `run_id` -> start a brand new run.
    - `run_id` + `rule_set_id` (for a COMPLETED run) -> hand off into
      Movement 2 validation.
    - `run_id` + `close_review=True` (for an AWAITING_HUMAN_REVIEW run) ->
      the explicit close, proceeding through commit to WATCHING.
    - `run_id` + `new_document` (for a WATCHING run) -> Movement 3's
      scoped incremental path.
    - `run_id` alone -> resume whatever was interrupted.
    """
    graph = build_graph(session)
    initial_state: SentinelGraphState = {}
    if run_id is not None:
        initial_state["run_id"] = str(run_id)
    if documents is not None:
        initial_state["documents"] = documents
    if rule_set_id is not None:
        initial_state["rule_set_id"] = str(rule_set_id)
    if close_review:
        initial_state["close_review"] = True
    if new_document is not None:
        initial_state["new_document"] = new_document

    return await graph.ainvoke(initial_state)
