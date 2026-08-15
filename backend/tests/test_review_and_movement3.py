"""Tests proving Batch 11: the shared review/commit lifecycle, and
Movement 3 (the living document) built on top of it.

Integration tests only — everything here runs the real graph against real
Postgres and real stored document bytes. No mocking of persistence or
orchestration.
"""
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.orchestration.graph import run_workflow
from app.orchestration.workflow_service import (
    complete_stage,
    create_rule_set,
    create_run,
    decide_review_item,
    get_changelog,
    get_current_report,
    get_report_version,
    get_resume_stage,
    get_review_items,
    get_run,
    start_stage,
)
from app.persistence.models import (
    CheckpointStatus,
    ReviewDecision,
    StageCheckpoint,
    WorkflowState,
)
from app.storage.document_storage import DocumentInput
from tests.conftest import TEST_DATABASE_URL

RELEASE_NOTES_V1 = """Release Notes
Status: Approved
Owner: Alice
Priority: High
"""

# Same "Owner" fact, unrelated to Status/Priority -> should stay unaffected
# when a new doc only talks about Status.
UPDATE_DOC_STATUS_ONLY = """Change Request
Status: Rejected
"""

# Directly contradicts Status -> conflict, since baseline already says
# Approved via a *different* value.
CONTRADICTING_DOC = """Change Request
Status: Rejected
"""

# Ambiguous relevance: shares words with the "Priority: High" statement's
# text via a related, non-key_value new claim.
AMBIGUOUS_DOC = """Meeting Notes
The team agreed the release priority remains High for this cycle.
"""

# Ambiguous relevance, non-key_value: shares words with the "Priority:
# High" baseline statement without being a key/value match, so it can
# never become a contradiction — always a proposed_change if affected.
NEW_INFO_DOC = """Meeting Notes
The team confirmed the release priority remains High for this cycle.
"""

NO_RULE_SET_NEEDED = None  # first commits in these tests skip Movement 2


def _doc(filename: str, text: str) -> DocumentInput:
    return DocumentInput(filename=filename, content=text.encode("utf-8"))


async def _checkpoints_for(db_session, run_id, stage):
    result = await db_session.execute(
        select(StageCheckpoint).where(StageCheckpoint.run_id == run_id, StageCheckpoint.stage == stage)
    )
    return result.scalars().all()


async def _commit_first_report(db_session, run_id):
    """Cold-start a run all the way to a committed Report v1, with an empty
    rule set (zero findings -> nothing blocks closing review) and an
    explicit close. Shared helper for tests focused on what happens next.
    """
    rule_set = await create_rule_set(db_session, name="empty", version="v1", rules=[])
    await db_session.commit()
    await run_workflow(db_session, run_id=run_id, rule_set_id=rule_set.id)

    result = await run_workflow(db_session, run_id=run_id, close_review=True)
    assert result.get("error") is None
    return result


# === Shared review/commit lifecycle =========================================


@pytest.mark.asyncio
async def test_report_cannot_commit_without_explicit_review_closure(db_session):
    rule_set = await create_rule_set(db_session, name="empty", version="v1", rules=[])
    await db_session.commit()

    m1 = await run_workflow(db_session, documents=[_doc("notes.txt", RELEASE_NOTES_V1)])
    run_id = uuid.UUID(m1["run_id"])
    await run_workflow(db_session, run_id=run_id, rule_set_id=rule_set.id)

    run = await get_run(db_session, run_id)
    assert run.current_state == WorkflowState.AWAITING_HUMAN_REVIEW

    # Plain resume, no close_review flag -> nothing happens, stays put.
    result = await run_workflow(db_session, run_id=run_id)
    assert result.get("error") is None
    run = await get_run(db_session, run_id)
    assert run.current_state == WorkflowState.AWAITING_HUMAN_REVIEW

    report = await get_current_report(db_session, run_id)
    assert report is None  # nothing committed


@pytest.mark.asyncio
async def test_first_successful_commit_creates_report_v1(db_session):
    m1 = await run_workflow(db_session, documents=[_doc("notes.txt", RELEASE_NOTES_V1)])
    run_id = uuid.UUID(m1["run_id"])
    await _commit_first_report(db_session, run_id)

    report = await get_current_report(db_session, run_id)
    assert report is not None
    assert report.version == 1
    assert report.is_current is True
    assert len(report.content["statements"]) > 0

    run = await get_run(db_session, run_id)
    assert run.current_state == WorkflowState.WATCHING


@pytest.mark.asyncio
async def test_committed_report_survives_new_session_and_is_retrievable(db_session):
    m1 = await run_workflow(db_session, documents=[_doc("notes.txt", RELEASE_NOTES_V1)])
    run_id = uuid.UUID(m1["run_id"])
    await _commit_first_report(db_session, run_id)

    fresh_engine = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    fresh_session_factory = async_sessionmaker(bind=fresh_engine, expire_on_commit=False)
    async with fresh_session_factory() as fresh_session:
        report = await get_current_report(fresh_session, run_id)
        assert report is not None
        assert report.version == 1
    await fresh_engine.dispose()


@pytest.mark.asyncio
async def test_changelog_persists_for_first_commit(db_session):
    m1 = await run_workflow(db_session, documents=[_doc("notes.txt", RELEASE_NOTES_V1)])
    run_id = uuid.UUID(m1["run_id"])
    await _commit_first_report(db_session, run_id)

    changelog = await get_changelog(db_session, run_id)
    assert len(changelog) == 1
    assert changelog[0].report_version == 1
    assert changelog[0].summary
    assert len(changelog[0].source_document_ids) == 1


@pytest.mark.asyncio
async def test_rejected_and_deferred_review_items_remain_recorded(db_session):
    forbidden_rule = {
        "identifier": "R1",
        "description": "no TODO",
        "rule_type": "forbidden_keyword",
        "parameters": {"keyword": "TODO"},
    }
    rule_set = await create_rule_set(db_session, name="checklist", version="v1", rules=[forbidden_rule])
    await db_session.commit()

    doc_with_todo = "Release Notes\nTODO: revisit\nStatus: Approved\n"
    m1 = await run_workflow(db_session, documents=[_doc("notes.txt", doc_with_todo)])
    run_id = uuid.UUID(m1["run_id"])
    await run_workflow(db_session, run_id=run_id, rule_set_id=rule_set.id)

    items = await get_review_items(db_session, run_id)
    assert len(items) == 1
    await decide_review_item(db_session, items[0].id, ReviewDecision.REJECTED, decided_by="tester", reason="acceptable risk")
    await db_session.commit()

    await run_workflow(db_session, run_id=run_id, close_review=True)

    items_after = await get_review_items(db_session, run_id)
    assert len(items_after) == 1
    assert items_after[0].decision == ReviewDecision.REJECTED
    assert items_after[0].decided_by == "tester"
    assert items_after[0].decision_reason == "acceptable risk"


# === Movement 3 ==============================================================


@pytest.mark.asyncio
async def test_new_document_takes_incremental_path_without_reprocessing_existing(db_session):
    m1 = await run_workflow(db_session, documents=[_doc("notes.txt", RELEASE_NOTES_V1)])
    run_id = uuid.UUID(m1["run_id"])
    await _commit_first_report(db_session, run_id)

    counts_before = {
        stage: len(await _checkpoints_for(db_session, run_id, stage))
        for stage in (WorkflowState.CLASSIFYING, WorkflowState.EXTRACTING, WorkflowState.CONSOLIDATING, WorkflowState.CONFLICT_SCAN)
    }
    assert all(c == 1 for c in counts_before.values())

    result = await run_workflow(
        db_session, run_id=run_id, new_document=_doc("update.txt", UPDATE_DOC_STATUS_ONLY)
    )
    assert result.get("error") is None

    # Existing Movement 1 stages: still exactly one checkpoint each — never rerun.
    counts_after = {
        stage: len(await _checkpoints_for(db_session, run_id, stage)) for stage in counts_before
    }
    assert counts_after == counts_before

    ndd = await _checkpoints_for(db_session, run_id, WorkflowState.NEW_DOCUMENT_DETECTED)
    assert len(ndd) == 1
    # Only the new document's claims were extracted here.
    new_claim_doc_ids = {c["document_id"] for c in ndd[0].output_data["new_claims"]}
    assert len(new_claim_doc_ids) == 1


@pytest.mark.asyncio
async def test_impact_analysis_uses_committed_report_affected_and_unaffected(db_session):
    m1 = await run_workflow(db_session, documents=[_doc("notes.txt", RELEASE_NOTES_V1)])
    run_id = uuid.UUID(m1["run_id"])
    await _commit_first_report(db_session, run_id)

    await run_workflow(db_session, run_id=run_id, new_document=_doc("update.txt", UPDATE_DOC_STATUS_ONLY))

    impact_cp = (await _checkpoints_for(db_session, run_id, WorkflowState.IMPACT_ANALYSIS))[0]
    report = await get_current_report(db_session, run_id)
    status_claim_id = next(s["claim_id"] for s in report.content["statements"] if s.get("key") == "status")
    owner_claim_id = next(s["claim_id"] for s in report.content["statements"] if s.get("key") == "owner")

    assert status_claim_id in impact_cp.output_data["affected_claim_ids"]
    assert owner_claim_id in impact_cp.output_data["unaffected_claim_ids"]


@pytest.mark.asyncio
async def test_unaffected_section_is_byte_identical_after_update(db_session):
    m1 = await run_workflow(db_session, documents=[_doc("notes.txt", RELEASE_NOTES_V1)])
    run_id = uuid.UUID(m1["run_id"])
    await _commit_first_report(db_session, run_id)
    v1 = await get_current_report(db_session, run_id)
    owner_statement_before = next(s for s in v1.content["statements"] if s.get("key") == "owner")

    await run_workflow(db_session, run_id=run_id, new_document=_doc("update.txt", CONTRADICTING_DOC))
    focused_cp = (await _checkpoints_for(db_session, run_id, WorkflowState.FOCUSED_UPDATE_DRAFTING))[0]
    proposed_change_items = [
        i for i in await get_review_items(db_session, run_id) if i.item_type == "proposed_change"
    ]
    for item in proposed_change_items:
        await decide_review_item(db_session, item.id, ReviewDecision.APPROVED, decided_by="tester")
    await db_session.commit()

    await run_workflow(db_session, run_id=run_id, close_review=True)

    v2 = await get_current_report(db_session, run_id)
    owner_statement_after = next(s for s in v2.content["statements"] if s.get("key") == "owner")
    assert owner_statement_after == owner_statement_before  # byte-identical, untouched


@pytest.mark.asyncio
async def test_ambiguous_impact_defaults_to_affected(db_session):
    m1 = await run_workflow(db_session, documents=[_doc("notes.txt", RELEASE_NOTES_V1)])
    run_id = uuid.UUID(m1["run_id"])
    await _commit_first_report(db_session, run_id)

    await run_workflow(db_session, run_id=run_id, new_document=_doc("meeting.txt", AMBIGUOUS_DOC))

    impact_cp = (await _checkpoints_for(db_session, run_id, WorkflowState.IMPACT_ANALYSIS))[0]
    report = await get_current_report(db_session, run_id)
    priority_claim_id = next(s["claim_id"] for s in report.content["statements"] if s.get("key") == "priority")

    # "priority" and "High" are shared significant words with the new,
    # non-key_value claim -> ambiguous overlap -> must default to affected.
    assert priority_claim_id in impact_cp.output_data["affected_claim_ids"]


@pytest.mark.asyncio
async def test_contradiction_creates_conflict_not_silent_resolution(db_session):
    m1 = await run_workflow(db_session, documents=[_doc("notes.txt", RELEASE_NOTES_V1)])
    run_id = uuid.UUID(m1["run_id"])
    await _commit_first_report(db_session, run_id)

    await run_workflow(db_session, run_id=run_id, new_document=_doc("update.txt", CONTRADICTING_DOC))

    focused_cp = (await _checkpoints_for(db_session, run_id, WorkflowState.FOCUSED_UPDATE_DRAFTING))[0]
    assert len(focused_cp.output_data["conflicts"]) == 1
    conflict = focused_cp.output_data["conflicts"][0]
    assert conflict["key"] == "status"
    assert conflict["baseline_value"] == "Approved"
    assert conflict["new_value"] == "Rejected"

    items = await get_review_items(db_session, run_id)
    conflict_items = [i for i in items if i.item_type == "conflict"]
    assert len(conflict_items) == 1


@pytest.mark.asyncio
async def test_approved_update_creates_report_v2_and_v1_remains_intact(db_session):
    m1 = await run_workflow(db_session, documents=[_doc("notes.txt", RELEASE_NOTES_V1)])
    run_id = uuid.UUID(m1["run_id"])
    await _commit_first_report(db_session, run_id)
    v1_content_before = (await get_current_report(db_session, run_id)).content

    await run_workflow(db_session, run_id=run_id, new_document=_doc("update.txt", NEW_INFO_DOC))
    items = await get_review_items(db_session, run_id)
    proposed = [i for i in items if i.item_type == "proposed_change"]
    assert len(proposed) >= 1
    for item in proposed:
        await decide_review_item(db_session, item.id, ReviewDecision.APPROVED, decided_by="tester")
    await db_session.commit()

    result = await run_workflow(db_session, run_id=run_id, close_review=True)
    assert result.get("error") is None

    v2 = await get_current_report(db_session, run_id)
    assert v2.version == 2
    assert v2.is_current is True

    v1 = await get_report_version(db_session, run_id, 1)
    assert v1 is not None
    assert v1.is_current is False
    assert v1.content == v1_content_before  # v1 itself never mutated

    run = await get_run(db_session, run_id)
    assert run.current_state == WorkflowState.WATCHING


@pytest.mark.asyncio
async def test_changelog_identifies_change_and_source_document_for_v2(db_session):
    m1 = await run_workflow(db_session, documents=[_doc("notes.txt", RELEASE_NOTES_V1)])
    run_id = uuid.UUID(m1["run_id"])
    await _commit_first_report(db_session, run_id)

    new_doc_result = await run_workflow(
        db_session, run_id=run_id, new_document=_doc("update.txt", NEW_INFO_DOC)
    )
    ndd_cp = (await _checkpoints_for(db_session, run_id, WorkflowState.NEW_DOCUMENT_DETECTED))[0]
    new_document_id = ndd_cp.output_data["document_id"]

    items = await get_review_items(db_session, run_id)
    for item in [i for i in items if i.item_type == "proposed_change"]:
        await decide_review_item(db_session, item.id, ReviewDecision.APPROVED, decided_by="tester")
    await db_session.commit()
    await run_workflow(db_session, run_id=run_id, close_review=True)

    changelog = await get_changelog(db_session, run_id)
    assert len(changelog) == 2
    v2_entry = changelog[1]
    assert v2_entry.report_version == 2
    assert new_document_id in v2_entry.source_document_ids
    assert len(v2_entry.affected_claim_ids) >= 1


@pytest.mark.asyncio
async def test_movement3_resume_from_interrupted_impact_analysis(db_session):
    m1 = await run_workflow(db_session, documents=[_doc("notes.txt", RELEASE_NOTES_V1)])
    run_id = uuid.UUID(m1["run_id"])
    await _commit_first_report(db_session, run_id)

    # Drive manually: register + detect the new document normally via the
    # graph, then interrupt IMPACT_ANALYSIS by starting it and never
    # completing it (crash simulation).
    await run_workflow(db_session, run_id=run_id, new_document=_doc("update.txt", UPDATE_DOC_STATUS_ONLY))
    # (the call above already completed the full Movement 3 pass for this
    # run once; to test a genuine interruption, drive a second run.)

    m1b = await run_workflow(db_session, documents=[_doc("notes2.txt", RELEASE_NOTES_V1)])
    run_id_b = uuid.UUID(m1b["run_id"])
    await _commit_first_report(db_session, run_id_b)

    from app.orchestration.workflow_service import add_document_to_run

    doc = await add_document_to_run(db_session, run_id_b, _doc("update2.txt", UPDATE_DOC_STATUS_ONLY))
    await db_session.commit()
    await start_stage(
        db_session, run_id_b, WorkflowState.NEW_DOCUMENT_DETECTED,
        initial_output_data={"document_id": str(doc.id)},
    )
    await db_session.commit()

    from app.storage.document_storage import read_document
    from app.agents.classifier import classify_document
    from app.agents.extractor import extract_claims

    text = read_document(doc.storage_key).decode("utf-8")
    classification = classify_document(str(doc.id), text)
    new_claims = extract_claims(str(doc.id), text)
    from app.orchestration.graph import _serialize

    await complete_stage(
        db_session, run_id_b, WorkflowState.NEW_DOCUMENT_DETECTED,
        output_data={
            "document_id": str(doc.id),
            "classification": _serialize(classification),
            "new_claims": [_serialize(c) for c in new_claims],
        },
    )
    await db_session.commit()
    await start_stage(db_session, run_id_b, WorkflowState.IMPACT_ANALYSIS)
    await db_session.commit()
    # Crashed here: IMPACT_ANALYSIS is IN_PROGRESS, never completed.

    fresh_engine = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    fresh_session_factory = async_sessionmaker(bind=fresh_engine, expire_on_commit=False)
    async with fresh_session_factory() as fresh_session:
        resume_stage = await get_resume_stage(fresh_session, run_id_b)
        assert resume_stage == WorkflowState.IMPACT_ANALYSIS

        ndd_before = await _checkpoints_for(fresh_session, run_id_b, WorkflowState.NEW_DOCUMENT_DETECTED)
        assert len(ndd_before) == 1
        assert ndd_before[0].status == CheckpointStatus.COMPLETED  # untouched by the interruption

        result = await run_workflow(fresh_session, run_id=run_id_b)
        assert result.get("error") is None

        impact_after = await _checkpoints_for(fresh_session, run_id_b, WorkflowState.IMPACT_ANALYSIS)
        assert len(impact_after) == 1  # not duplicated
        assert impact_after[0].status == CheckpointStatus.COMPLETED

        run = await get_run(fresh_session, run_id_b)
        assert run.current_state == WorkflowState.AWAITING_HUMAN_REVIEW
    await fresh_engine.dispose()
