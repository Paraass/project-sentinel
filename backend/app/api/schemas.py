"""Request/response models for the API layer.

Pure data shapes only — no business logic, no persistence, no workflow
decisions. Routes translate between these and the existing domain
functions/models; nothing here duplicates what workflow_service already
owns.
"""
import base64
import uuid
from datetime import datetime
from typing import ClassVar

from pydantic import BaseModel, Field

from app.persistence.models import ReviewDecision
from app.storage.document_storage import DocumentInput


class DocumentUpload(BaseModel):
    """One document as submitted over HTTP — content is base64-encoded
    since this is a JSON API, not a multipart upload endpoint.
    """

    filename: str
    content_base64: str
    content_type: str | None = None

    def to_document_input(self) -> DocumentInput:
        return DocumentInput(
            filename=self.filename,
            content=base64.b64decode(self.content_base64),
            content_type=self.content_type,
        )


class CreateRunRequest(BaseModel):
    name: str | None = None
    documents: list[DocumentUpload] = Field(min_length=1)


class RunResponse(BaseModel):
    run_id: uuid.UUID
    name: str | None
    current_state: str
    created_at: datetime
    updated_at: datetime
    document_count: int
    resume_stage: str


class ReviewItemResponse(BaseModel):
    id: uuid.UUID
    run_id: uuid.UUID
    item_type: str
    source_reference: str
    content: dict
    decision: str
    decision_reason: str | None
    decided_by: str | None
    decided_at: datetime | None
    created_at: datetime


class ReviewDecisionRequest(BaseModel):
    decision: str  # "approve" | "reject" | "defer"
    decided_by: str
    reason: str | None = None

    _DECISION_MAP: ClassVar[dict[str, ReviewDecision]] = {
        "approve": ReviewDecision.APPROVED,
        "reject": ReviewDecision.REJECTED,
        "defer": ReviewDecision.DEFERRED,
    }

    def to_review_decision(self) -> ReviewDecision:
        try:
            return self._DECISION_MAP[self.decision.lower()]
        except KeyError as exc:
            raise ValueError(
                f"decision must be one of {sorted(self._DECISION_MAP)}, got {self.decision!r}"
            ) from exc


class ReportResponse(BaseModel):
    id: uuid.UUID
    run_id: uuid.UUID
    version: int
    content: dict
    is_current: bool
    created_at: datetime


class ChangelogEntryResponse(BaseModel):
    id: uuid.UUID
    report_version: int
    summary: str
    source_document_ids: list[str]
    affected_claim_ids: list[str]
    created_at: datetime


class ErrorResponse(BaseModel):
    detail: str
