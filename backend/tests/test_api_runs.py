"""Tests for the Batch 12 API surface.

Real HTTP calls through FastAPI's TestClient against the real app, backed
by real Postgres — nothing mocked. Batch 12's required endpoint list has
no HTTP path to trigger Movement 2 (rule validation) itself, so tests that
need a run sitting at AWAITING_HUMAN_REVIEW drive that setup directly
through workflow_service/graph — the exact same functions every other test
file in this suite already uses — then exercise the actual API endpoints
for everything Batch 12 asks to prove.
"""
import base64
import os

import pytest
import pytest_asyncio

os.environ["DATABASE_URL"] = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://postgres:postgres@localhost:5432/sentinel_test",
)

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.orchestration.graph import run_workflow  # noqa: E402
from app.orchestration.workflow_service import create_rule_set  # noqa: E402
from app.persistence.database import dispose_engine  # noqa: E402
from app.core.config import get_settings  # noqa: E402

RELEASE_NOTES = b"Release Notes\nStatus: Approved\nOwner: Alice\n"


def _b64(content: bytes) -> str:
    return base64.b64encode(content).decode("ascii")


@pytest_asyncio.fixture
async def client():
    """Fresh TestClient with a fresh settings cache and engine."""

    get_settings.cache_clear()
    await dispose_engine()

    with TestClient(app) as test_client:
        yield test_client

    await dispose_engine()
    get_settings.cache_clear()


def _create_run_payload(filename: str = "notes.txt", content: bytes = RELEASE_NOTES) -> dict:
    return {
        "name": "api-test-run",
        "documents": [
            {"filename": filename, "content_base64": _b64(content), "content_type": "text/plain"}
        ],
    }


# === 1. health endpoint still works ========================================


def test_health_still_works(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


# === 2 & 3. create run works; created run can be retrieved =================


def test_create_and_retrieve_run(client):
    create_response = client.post("/runs", json=_create_run_payload())
    assert create_response.status_code == 201
    body = create_response.json()
    assert body["current_state"] == "COMPLETED"
    assert body["document_count"] == 1
    run_id = body["run_id"]

    get_response = client.get(f"/runs/{run_id}")
    assert get_response.status_code == 200
    assert get_response.json()["run_id"] == run_id
    assert get_response.json()["current_state"] == "COMPLETED"


# === 4. missing run returns 404 =============================================


def test_missing_run_returns_404(client):
    response = client.get("/runs/00000000-0000-0000-0000-000000000000")
    assert response.status_code == 404


def test_missing_run_review_items_returns_404(client):
    response = client.get("/runs/00000000-0000-0000-0000-000000000000/review-items")
    assert response.status_code == 404


# === 5 & 6. review items retrievable; decision uses existing service logic =


@pytest.mark.asyncio
async def test_review_items_retrievable_and_decision_persists(client, db_session):
    create_response = client.post("/runs", json=_create_run_payload())
    run_id = create_response.json()["run_id"]

    forbidden_rule = {
        "identifier": "R1",
        "description": "no TODO",
        "rule_type": "forbidden_keyword",
        "parameters": {"keyword": "TODO"},
    }
    rule_set = await create_rule_set(db_session, name="checklist", version="v1", rules=[forbidden_rule])
    await db_session.commit()

    doc_with_todo = _create_run_payload("with_todo.txt", b"Release Notes\nTODO: revisit\nStatus: Approved\n")
    create2 = client.post("/runs", json=doc_with_todo)
    run_id2 = create2.json()["run_id"]

    import uuid

    await run_workflow(db_session, run_id=uuid.UUID(run_id2), rule_set_id=rule_set.id)
    await db_session.commit()

    items_response = client.get(f"/runs/{run_id2}/review-items")
    assert items_response.status_code == 200
    items = items_response.json()
    assert len(items) == 1
    assert items[0]["decision"] == "PENDING"

    item_id = items[0]["id"]
    decision_response = client.post(
        f"/review-items/{item_id}/decision",
        json={"decision": "reject", "decided_by": "api-tester", "reason": "acceptable risk"},
    )
    assert decision_response.status_code == 200
    decided = decision_response.json()
    assert decided["decision"] == "REJECTED"
    assert decided["decided_by"] == "api-tester"
    assert decided["decision_reason"] == "acceptable risk"

    # Persisted for real — a fresh GET reflects the same decision.
    items_after = client.get(f"/runs/{run_id2}/review-items").json()
    assert items_after[0]["decision"] == "REJECTED"


def test_deciding_unknown_review_item_returns_404(client):
    response = client.post(
        "/review-items/00000000-0000-0000-0000-000000000000/decision",
        json={"decision": "approve", "decided_by": "tester"},
    )
    assert response.status_code == 404


def test_invalid_decision_value_returns_422(client):
    create_response = client.post("/runs", json=_create_run_payload())
    run_id = create_response.json()["run_id"]
    # Item id doesn't need to exist for this — validation fails first.
    response = client.post(
        "/review-items/00000000-0000-0000-0000-000000000000/decision",
        json={"decision": "maybe", "decided_by": "tester"},
    )
    assert response.status_code == 422


# === 7. review cannot be bypassed ===========================================


def test_review_close_refused_when_not_awaiting_review(client):
    create_response = client.post("/runs", json=_create_run_payload())
    run_id = create_response.json()["run_id"]
    assert create_response.json()["current_state"] == "COMPLETED"

    close_response = client.post(f"/runs/{run_id}/review/close")
    assert close_response.status_code == 409

    # And no report was fabricated as a side effect of the refused attempt.
    report_response = client.get(f"/runs/{run_id}/report")
    assert report_response.status_code == 404


def test_new_document_refused_when_not_watching(client):
    create_response = client.post("/runs", json=_create_run_payload())
    run_id = create_response.json()["run_id"]

    response = client.post(
        f"/runs/{run_id}/documents",
        json={"filename": "x.txt", "content_base64": _b64(b"Status: Rejected\n")},
    )
    assert response.status_code == 409


# === 8 & 9. current report + historical version retrievable ================


@pytest.mark.asyncio
async def test_current_and_historical_report_retrievable(client, db_session):
    create_response = client.post("/runs", json=_create_run_payload())
    run_id = create_response.json()["run_id"]

    import uuid

    rid = uuid.UUID(run_id)
    rule_set = await create_rule_set(db_session, name="empty", version="v1", rules=[])
    await db_session.commit()
    await run_workflow(db_session, run_id=rid, rule_set_id=rule_set.id)
    await run_workflow(db_session, run_id=rid, close_review=True)
    await db_session.commit()

    current_response = client.get(f"/runs/{run_id}/report")
    assert current_response.status_code == 200
    current_body = current_response.json()
    assert current_body["version"] == 1
    assert current_body["is_current"] is True

    v1_response = client.get(f"/runs/{run_id}/report/1")
    assert v1_response.status_code == 200
    assert v1_response.json()["version"] == 1

    missing_version_response = client.get(f"/runs/{run_id}/report/99")
    assert missing_version_response.status_code == 404


def test_report_returns_404_before_any_commit(client):
    create_response = client.post("/runs", json=_create_run_payload())
    run_id = create_response.json()["run_id"]

    response = client.get(f"/runs/{run_id}/report")
    assert response.status_code == 404


# === 10. changelog endpoint returns persisted entries =======================


@pytest.mark.asyncio
async def test_changelog_returns_persisted_entries(client, db_session):
    create_response = client.post("/runs", json=_create_run_payload())
    run_id = create_response.json()["run_id"]

    import uuid

    rid = uuid.UUID(run_id)
    rule_set = await create_rule_set(db_session, name="empty", version="v1", rules=[])
    await db_session.commit()
    await run_workflow(db_session, run_id=rid, rule_set_id=rule_set.id)
    await run_workflow(db_session, run_id=rid, close_review=True)
    await db_session.commit()

    response = client.get(f"/runs/{run_id}/changelog")
    assert response.status_code == 200
    entries = response.json()
    assert len(entries) == 1
    assert entries[0]["report_version"] == 1
    assert entries[0]["summary"]


# === 11. new document endpoint enters Movement 3, not Movement 1 rerun =====


@pytest.mark.asyncio
async def test_new_document_endpoint_enters_movement3_without_rerunning_movement1(client, db_session):
    create_response = client.post("/runs", json=_create_run_payload())
    run_id = create_response.json()["run_id"]

    import uuid
    from sqlalchemy import select
    from app.persistence.models import StageCheckpoint, WorkflowState

    rid = uuid.UUID(run_id)
    rule_set = await create_rule_set(db_session, name="empty", version="v1", rules=[])
    await db_session.commit()
    await run_workflow(db_session, run_id=rid, rule_set_id=rule_set.id)
    await run_workflow(db_session, run_id=rid, close_review=True)
    await db_session.commit()

    get_response = client.get(f"/runs/{run_id}")
    assert get_response.json()["current_state"] == "WATCHING"

    async def _checkpoint_count(stage):
        result = await db_session.execute(
            select(StageCheckpoint).where(StageCheckpoint.run_id == rid, StageCheckpoint.stage == stage)
        )
        return len(result.scalars().all())

    classifying_count_before = await _checkpoint_count(WorkflowState.CLASSIFYING)
    assert classifying_count_before == 1

    doc_response = client.post(
        f"/runs/{run_id}/documents",
        json={
            "filename": "update.txt",
            "content_base64": _b64(b"Change Request\nOwner: Bob\n"),
        },
    )
    assert doc_response.status_code == 200
    assert doc_response.json()["current_state"] == "AWAITING_HUMAN_REVIEW"

    # Movement 1's CLASSIFYING never ran again — still exactly one checkpoint.
    classifying_count_after = await _checkpoint_count(WorkflowState.CLASSIFYING)
    assert classifying_count_after == 1

    ndd_count = await _checkpoint_count(WorkflowState.NEW_DOCUMENT_DETECTED)
    assert ndd_count == 1
