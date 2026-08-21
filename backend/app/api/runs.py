"""API routes exposing the existing durable workflow.

Every route is thin: parse the request, call an existing workflow_service
or graph function, translate the result/exception into an HTTP response.
No workflow decisions, no persistence, no business logic live here — that
would duplicate what workflow_service.py and graph.py already own.
"""
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas import (
    ChangelogEntryResponse,
    CreateRunRequest,
    DocumentUpload,
    ReportResponse,
    ReviewDecisionRequest,
    ReviewItemResponse,
    RunResponse,
)
from app.orchestration.graph import run_workflow
from app.orchestration.workflow_service import (
    RunNotFoundError,
    WorkflowError,
    decide_review_item,
    get_changelog,
    get_current_report,
    get_report_version,
    get_resume_stage,
    get_review_items,
    get_run,
)
from app.persistence.database import get_db_session
from app.persistence.models import Document, WorkflowRun, WorkflowState

router = APIRouter(tags=["runs"])


async def _document_count(session: AsyncSession, run_id: uuid.UUID) -> int:
    # Explicit query rather than accessing run.documents: lazy relationship
    # loading relies on SQLAlchemy's implicit-await greenlet trick, which
    # broke once already under LangGraph's execution machinery (Batch 7).
    # An explicit query has no such dependency and is the safer pattern
    # regardless of caller.
    result = await session.execute(
        select(func.count()).select_from(Document).where(Document.run_id == run_id)
    )
    return result.scalar_one()


async def _run_response(
    session: AsyncSession, run: WorkflowRun, resume_stage: WorkflowState
) -> RunResponse:
    document_count = await _document_count(session, run.id)
    return RunResponse(
        run_id=run.id,
        name=run.name,
        current_state=run.current_state.value,
        created_at=run.created_at,
        updated_at=run.updated_at,
        document_count=document_count,
        resume_stage=resume_stage.value,
    )


def _review_item_response(item) -> ReviewItemResponse:
    return ReviewItemResponse(
        id=item.id,
        run_id=item.run_id,
        item_type=item.item_type,
        source_reference=item.source_reference,
        content=item.content,
        decision=item.decision.value,
        decision_reason=item.decision_reason,
        decided_by=item.decided_by,
        decided_at=item.decided_at,
        created_at=item.created_at,
    )


def _report_response(report) -> ReportResponse:
    return ReportResponse(
        id=report.id,
        run_id=report.run_id,
        version=report.version,
        content=report.content,
        is_current=report.is_current,
        created_at=report.created_at,
    )


@router.post("/runs", response_model=RunResponse, status_code=status.HTTP_201_CREATED)
async def create_run_endpoint(
    request: CreateRunRequest, session: AsyncSession = Depends(get_db_session)
) -> RunResponse:
    """Start a new Movement 1 run from uploaded documents.

    Delegates entirely to run_workflow() — the exact same entry point
    every test in this codebase uses to start a cold-start run. This route
    invents no new intake or classification logic.
    """
    documents = [doc.to_document_input() for doc in request.documents]
    result = await run_workflow(session, documents=documents, rule_set_id=None)

    if result.get("error"):
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Run failed during Movement 1 processing.",
        )

    run_id = uuid.UUID(result["run_id"])
    run = await get_run(session, run_id)
    resume_stage = await get_resume_stage(session, run_id)
    return await _run_response(session, run, resume_stage)


@router.get("/runs/{run_id}", response_model=RunResponse)
async def get_run_endpoint(
    run_id: uuid.UUID, session: AsyncSession = Depends(get_db_session)
) -> RunResponse:
    try:
        run = await get_run(session, run_id)
    except RunNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    resume_stage = await get_resume_stage(session, run_id)
    return await _run_response(session, run, resume_stage)


@router.get("/runs/{run_id}/review-items", response_model=list[ReviewItemResponse])
async def list_review_items_endpoint(
    run_id: uuid.UUID, session: AsyncSession = Depends(get_db_session)
) -> list[ReviewItemResponse]:
    try:
        await get_run(session, run_id)  # 404 if the run itself doesn't exist
    except RunNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    items = await get_review_items(session, run_id)
    return [_review_item_response(item) for item in items]


@router.post("/review-items/{item_id}/decision", response_model=ReviewItemResponse)
async def decide_review_item_endpoint(
    item_id: uuid.UUID,
    request: ReviewDecisionRequest,
    session: AsyncSession = Depends(get_db_session),
) -> ReviewItemResponse:
    try:
        decision = request.to_review_decision()
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    try:
        item = await decide_review_item(
            session, item_id, decision, decided_by=request.decided_by, reason=request.reason
        )
    except WorkflowError as exc:
        # decide_review_item's only failure mode is "no such item" —
        # translated to 404 rather than a generic 400/500.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    return _review_item_response(item)


@router.post("/runs/{run_id}/review/close", response_model=RunResponse)
async def close_review_endpoint(
    run_id: uuid.UUID, session: AsyncSession = Depends(get_db_session)
) -> RunResponse:
    """The required explicit close operation.

    Refuses (409) unless the run is genuinely sitting at
    AWAITING_HUMAN_REVIEW — run_workflow's own routing would otherwise
    silently no-op on close_review=True from any other state, which would
    look like success to a caller without actually closing anything. This
    check is what turns that silent no-op into an honest error, never
    bypassing human review.
    """
    try:
        resume_stage = await get_resume_stage(session, run_id)
    except RunNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    if resume_stage != WorkflowState.AWAITING_HUMAN_REVIEW:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Review cannot be closed from state {resume_stage.value!r}; "
                "the run must be AWAITING_HUMAN_REVIEW."
            ),
        )

    result = await run_workflow(session, run_id=run_id, close_review=True)
    if result.get("error"):
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Run failed during review closure/commit.",
        )

    run = await get_run(session, run_id)
    new_resume_stage = await get_resume_stage(session, run_id)
    return await _run_response(session, run, new_resume_stage)


@router.get("/runs/{run_id}/report", response_model=ReportResponse)
async def get_current_report_endpoint(
    run_id: uuid.UUID, session: AsyncSession = Depends(get_db_session)
) -> ReportResponse:
    try:
        await get_run(session, run_id)
    except RunNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    report = await get_current_report(session, run_id)
    if report is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No report has been committed for this run yet.",
        )
    return _report_response(report)


@router.get("/runs/{run_id}/report/{version}", response_model=ReportResponse)
async def get_report_version_endpoint(
    run_id: uuid.UUID, version: int, session: AsyncSession = Depends(get_db_session)
) -> ReportResponse:
    try:
        await get_run(session, run_id)
    except RunNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    report = await get_report_version(session, run_id, version)
    if report is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Report version {version} does not exist for this run.",
        )
    return _report_response(report)


@router.get("/runs/{run_id}/changelog", response_model=list[ChangelogEntryResponse])
async def get_changelog_endpoint(
    run_id: uuid.UUID, session: AsyncSession = Depends(get_db_session)
) -> list[ChangelogEntryResponse]:
    try:
        await get_run(session, run_id)
    except RunNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    entries = await get_changelog(session, run_id)
    return [
        ChangelogEntryResponse(
            id=entry.id,
            report_version=entry.report_version,
            summary=entry.summary,
            source_document_ids=entry.source_document_ids,
            affected_claim_ids=entry.affected_claim_ids,
            created_at=entry.created_at,
        )
        for entry in entries
    ]


@router.post("/runs/{run_id}/documents", response_model=RunResponse)
async def submit_new_document_endpoint(
    run_id: uuid.UUID,
    document: DocumentUpload,
    session: AsyncSession = Depends(get_db_session),
) -> RunResponse:
    """Submit a new document into the EXISTING Movement 3 path.

    Refuses (409) unless the run is genuinely WATCHING, for the same
    reason close_review is gated: run_workflow's routing would otherwise
    silently no-op on a new_document argument from any other state.
    Movement 1 is never re-run — this always enters through
    NEW_DOCUMENT_DETECTED's scoped intake, exactly as graph.py already
    guarantees for every existing test.
    """
    try:
        resume_stage = await get_resume_stage(session, run_id)
    except RunNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    if resume_stage != WorkflowState.WATCHING:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Cannot submit a new document from state {resume_stage.value!r}; "
                "the run must be WATCHING."
            ),
        )

    result = await run_workflow(
        session, run_id=run_id, new_document=document.to_document_input()
    )
    if result.get("error"):
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Run failed during Movement 3 processing.",
        )

    run = await get_run(session, run_id)
    new_resume_stage = await get_resume_stage(session, run_id)
    return await _run_response(session, run, new_resume_stage)
