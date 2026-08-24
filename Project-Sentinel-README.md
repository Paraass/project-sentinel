# Project Sentinel

Project Sentinel is an AI-powered project documentation analyst that ingests related software-engineering documents, classifies and extracts claims, detects disagreements, validates the resulting material against a configurable rule set, and maintains a versioned, human-reviewed report that can be updated as new documents arrive.

## Quick Links

- [GitHub Repository](https://github.com/Paraass/project-sentinel)
- [Live Frontend](https://sentinel-frontend-w2o6.onrender.com)
- [Live Backend](https://project-sentinel-fk8f.onrender.com)
- [Backend Health](https://project-sentinel-fk8f.onrender.com/health)
- [Swagger / OpenAPI](https://project-sentinel-fk8f.onrender.com/docs)

> **Submission evidence note:** Repository implementation details below are based on the implementation and validation performed during development. Local test, build, Docker, and deployment results are stated only where they were actually observed. The deployed services were exercised on August 24, 2026.

---

## 1. What It Does

Project Sentinel implements three movements from its locked behavioral architecture.

### Movement 1: Understand

Given an initial pile of engineering documents, Sentinel:

1. Classifies documents such as PRD, SRS, Architecture, API Spec, Meeting Notes, or Unknown.
2. Extracts individual claims with source provenance.
3. Consolidates the extracted material into a grounded deliverable.
4. Detects disagreements between claims from different documents.
5. Persists workflow progress so completed work is not silently repeated after an interruption.

### Movement 2: Examine

A supplied rule set is checked against the grounded deliverable and its underlying claims.

1. Satisfied rules produce no review item.
2. Violations become independently reviewable findings.
3. Rules that cannot be evaluated from available evidence become explicit `CANNOT_EVALUATE` findings rather than silent passes.
4. Findings and conflicts are independently approved, rejected, or deferred.
5. Review must be explicitly closed before a report can be committed.

### Movement 3: Stay Alive

After a report has been committed, a new document can be submitted.

1. The new document is classified and extracted without reprocessing the original pile.
2. Impact analysis compares new claims with the current committed report.
3. Ambiguous relevance is treated conservatively as affected.
4. Only affected report entries are proposed for change.
5. Unaffected content is carried forward unchanged.
6. Contradictory information is surfaced as a conflict instead of being silently resolved.
7. The proposal returns to the same human review queue used by Movement 2.
8. Approved updates create a new immutable report version and changelog entry.

---

## 2. Architecture

```text
Frontend
React + TypeScript + Vite + Tailwind
        |
        | HTTP / JSON
        v
FastAPI API Layer
        |
        v
Orchestration
LangGraph workflow + workflow_service.py
        |
        +----------------------+
        |                      |
        v                      v
Agent Modules             Persistence
classifier                SQLAlchemy
extractor                 PostgreSQL
consolidator
conflict_scanner
rule_validator
impact_analyzer
update_drafter
        |
        v
Document Storage
Content-addressed local filesystem
```

### Architectural boundaries

- API routes are intentionally thin.
- API routes call orchestration or workflow-service functions rather than agent functions directly.
- Agent modules are pure data-processing functions and do not perform database or document-storage I/O.
- Orchestration is responsible for persistence and document storage access.
- PostgreSQL is the durable source of workflow state.
- Document content is stored using content-addressed storage based on SHA-256.
- Configuration is environment-variable driven through a single `pydantic-settings` `Settings` object.
- Structured logs are written to stdout using Python's standard logging module.

### PostgreSQL and pgvector

PostgreSQL is used for persistence and the `pgvector` extension is enabled during database initialization.

The current implementation does not perform embedding generation or vector similarity queries. The extension is therefore present as infrastructure support, but vector search is not represented as an implemented product capability.

### Watcher boundary

`WATCHING` is a durable workflow state. The current implementation does not contain a standalone filesystem watcher or background daemon.

A new document enters Movement 3 through:

```text
POST /runs/{run_id}/documents
```

This keeps the implemented behavior explicit rather than claiming that a background watcher exists when it does not.

---

## 3. Workflow and Crash Recovery

The implemented workflow uses durable state checkpoints.

```text
INTAKE_PENDING
      |
      v
CLASSIFYING
      |
      v
EXTRACTING
      |
      v
CONSOLIDATING
      |
      v
CONFLICT_SCAN
      |
      v
COMPLETED
      |
      | rule set supplied
      v
RULE_VALIDATION_PENDING
      |
      v
VALIDATING
      |
      v
AWAITING_HUMAN_REVIEW
      |
      | explicit review close
      v
REVIEW_CLOSED
      |
      v
COMMITTING
      |
      v
REPORT_COMMITTED
      |
      v
WATCHING
      |
      | new document supplied
      v
NEW_DOCUMENT_DETECTED
      |
      v
IMPACT_ANALYSIS
      |
      v
FOCUSED_UPDATE_DRAFTING
      |
      v
AWAITING_HUMAN_REVIEW
```

Each stage transition is checked against an explicit allow-list.

A `StageCheckpoint` is persisted before stage work begins and updated when the stage completes. If a process is interrupted after a stage has started but before it has completed, the next invocation reads the durable checkpoint state and resumes the incomplete stage.

The implementation deliberately uses PostgreSQL as the durable source of workflow state rather than introducing a second persistence mechanism through a LangGraph checkpointer.

---

## 4. Human Review and No-Bluffing

Every finding, conflict, and proposed change is independently reviewable.

Review decisions are made through:

```text
POST /review-items/{item_id}/decision
```

Each decision records:

- decision
- `decided_by`
- optional reason
- timestamp

Rejecting or deferring one item does not delete or modify sibling review items.

A report is not considered current until the commit stage has actually completed.

The rule validator has three outcomes:

- Satisfied
- `VIOLATION`
- `CANNOT_EVALUATE`

`CANNOT_EVALUATE` is intentionally distinct from a passing rule.

Claims without required provenance are rejected before entering the deliverable. This prevents unsupported statements from being presented as grounded project facts.

---

## 5. Incremental Updates

The Movement 3 endpoint is:

```text
POST /runs/{run_id}/documents
```

The endpoint accepts a new document only when the run is genuinely in `WATCHING`.

The new document is processed independently. Existing documents are not reclassified or re-extracted.

Impact analysis compares the new claims with the current committed report. Relevance can be:

- confidently affected
- confidently unaffected
- ambiguous

Ambiguous cases default to affected so that potentially important changes are not silently discarded.

For a proposed update:

- affected entries may change
- unaffected entries are carried forward from the existing report
- contradictions become explicit conflicts
- no automatic conflict resolution is performed
- the proposal enters the same review queue as Movement 2

After an approved update:

- the new report version becomes current
- the previous version remains stored
- a changelog entry records the version, summary, source document IDs, and affected claim IDs

---

## 6. Accepted Inputs

Documents are stored as raw bytes and content-addressed by SHA-256.

The current classification and extraction implementation operates on UTF-8-decodable text. It does not include native PDF, DOCX, or other binary document parsing.

The implemented classifier and extractor were tested against plain-text engineering documents representing domains such as:

- PRDs
- SRS documents
- architecture notes
- API specifications
- meeting notes

The analysis path is deterministic and uses keyword, pattern, and heuristic logic. No external LLM or API key is required by the tested implementation.

---

## 7. API

All API routes are defined under `backend/app/api/` and registered by `backend/app/main.py`.

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Liveness check |
| POST | `/runs` | Start a new Movement 1 run |
| GET | `/runs/{run_id}` | Get current run state, resume stage, and document count |
| GET | `/runs/{run_id}/review-items` | Get the run's review queue |
| POST | `/review-items/{item_id}/decision` | Approve, reject, or defer one review item |
| POST | `/runs/{run_id}/review/close` | Explicitly close the review cycle |
| GET | `/runs/{run_id}/report` | Get the current committed report |
| GET | `/runs/{run_id}/report/{version}` | Get a specific report version |
| GET | `/runs/{run_id}/changelog` | Get changelog entries |
| POST | `/runs/{run_id}/documents` | Submit a new document into Movement 3 |

Interactive API documentation is available through Swagger UI at `/docs`.

Domain errors are translated into appropriate HTTP responses by dedicated FastAPI exception handling.

---

## 8. Local Setup

### Prerequisites

- Docker Desktop
- Git

### Clone

```bash
git clone https://github.com/Paraass/project-sentinel.git
cd project-sentinel
```

### Configure environment

```bash
cp .env.example .env
```

Set the required database and application values in `.env`.

Do not commit `.env` or any credentials.

### Start the application

```bash
docker compose up -d --build
```

### Local URLs

- Frontend: http://localhost:5173
- Backend: http://localhost:8000
- Swagger / OpenAPI: http://localhost:8000/docs
- Health check: http://localhost:8000/health

### Verify backend health

```bash
curl http://localhost:8000/health
```

Expected shape:

```json
{
  "status": "ok",
  "service": "Project Sentinel",
  "version": "0.1.0",
  "environment": "development"
}
```

### Run backend tests

The repository provides a dedicated test profile:

```bash
docker compose --profile test run --rm backend-test
```

The local development test suite was also run directly from the backend virtual environment during final validation.

---

## 9. Render Deployment

The current submission is deployed on Render.

### Frontend

https://sentinel-frontend-w2o6.onrender.com

### Backend

https://project-sentinel-fk8f.onrender.com

### Backend health

https://project-sentinel-fk8f.onrender.com/health

### Swagger / OpenAPI

https://project-sentinel-fk8f.onrender.com/docs

The deployment uses Render's free tier. Free instances may sleep after inactivity, so the first request after an idle period can take longer.

The deployed backend requires environment configuration for:

- `DATABASE_URL`
- `LOG_LEVEL`
- `ENVIRONMENT`
- `CORS_ORIGINS`
- `DOCUMENT_STORAGE_PATH`

`CORS_ORIGINS` must include the deployed frontend origin.

---

## 10. Validation and Evidence

The application was validated locally and through the deployed services on August 24, 2026.

### Local validation

The backend test suite completed successfully with:

```text
75 passed
```

The frontend production build completed successfully with Vite.

The Docker images for both backend and frontend were built successfully.

Docker Compose then started:

- PostgreSQL
- backend
- frontend

The local backend health endpoint returned HTTP 200.

### Deployed backend validation

The deployed `/health` endpoint returned HTTP 200.

Swagger UI was reachable at:

https://project-sentinel-fk8f.onrender.com/docs

`POST /runs` was exercised through Swagger and returned HTTP 201.

A deployed test run completed with:

```text
current_state: COMPLETED
document_count: 1
resume_stage: COMPLETED
```

A second API test using a Markdown document also returned HTTP 201 and completed successfully.

### Deployed frontend validation

The deployed frontend loaded successfully.

The application shell exposes:

- Review
- Report
- History

A run-specific URL using:

```text
?run_id=<UUID>
```

successfully loaded the run/review page and retrieved the corresponding backend state.

### Important validation boundary

The deployment smoke runs used documents without a rule set. Those runs therefore completed without creating review items or a committed report.

This validates:

- document intake
- processing
- persistence
- API connectivity
- frontend/backend connectivity
- deployed application startup

It does not, by itself, prove the complete human-review, commit, report-versioning, or Movement 3 path. Those deeper behaviors are covered by the repository integration tests.

---

## 11. Test Coverage

The test suite covers the main workflow, persistence, API, review, and Movement 3 behavior.

| Test file | Coverage |
|---|---|
| `test_health.py` | Liveness endpoint |
| `test_document_storage.py` | Content-addressed storage, deterministic hashing, metadata, and persistence across a separate process |
| `test_workflow_service.py` | State transitions, invalid transitions, durable checkpoints, interrupted-stage resume, and prevention of completed-stage re-entry |
| `test_graph.py` | LangGraph compilation, cold-start execution, conditional routing, resume behavior, and completed-run no-op behavior |
| `test_movement1.py` | Classification, uncertainty handling, claim provenance, conflict detection, zero-conflict behavior, and prompt-injection-style source documents treated as data |
| `test_rule_validation.py` | Rule persistence, satisfied rules, violations, `CANNOT_EVALUATE`, independent findings, prompt-injection-style validation inputs, and rule-set resume safety |
| `test_review_and_movement3.py` | Human review gate, first commit, report persistence, changelog, rejected/deferred decisions, scoped new-document processing, impact analysis, byte-identical unaffected content, conflict handling, Report v2, and Movement 3 resume |
| `test_api_runs.py` | Run creation/retrieval, missing resources, review-item decisions, review closure, report/version/changelog retrieval, and Movement 3 API behavior |

### Concurrency boundary

The current repository does not contain a dedicated stress test proving genuinely concurrent execution of two runs or two operations on the same run.

The implementation scopes workflow data by `run_id` and uses atomic database transactions for commits, but concurrency safety should not be presented as fully proven without a dedicated concurrency test.

---

## 12. Assignment Requirements Mapping

| Requirement | Implementation / Evidence | Status |
|---|---|---|
| 1. Visible, watchable stages with branching | `WorkflowState`, persisted `StageCheckpoint` rows, and conditional routing in the workflow graph | Implemented and tested |
| 2. Kill and resume without losing completed work | Durable stage checkpoints and fresh-session resume tests | Implemented and tested |
| 3. Item-level human approval gate | Independently persisted `ReviewItem` rows and explicit decisions | Implemented and tested |
| 4. Machine-drivable approval | Review decision and review-close operations are exposed through HTTP endpoints | Implemented and tested through the API surface |
| 5. Never bluffs | Provenance validation, explicit `CANNOT_EVALUATE`, and commit gating | Implemented and tested |
| 6. Fresh clone and documented setup | Docker Compose setup and README instructions | Documented; not presented as an independently tested fresh-clone timing result |
| 7. Real tests without a live API key | Tests run against real PostgreSQL with no external LLM/API dependency | Implemented and tested |
| 8. Does not take orders from ingested documents | Prompt-injection-style test coverage in extraction and validation | Implemented and tested |
| 9. Concurrent runs remain safe | Per-run scoping and transactional persistence | Not proven by a dedicated concurrency stress test |
| 10. Cost/time observability | Stage start and completion timestamps provide duration information | Partially implemented; no external model operation-cost accounting exists |

---

## 13. Assumptions and Design Decisions

### PostgreSQL is the durable workflow source

LangGraph's native checkpointer is not used. Workflow durability is implemented through PostgreSQL checkpoints so there is one authoritative persistence layer.

### Ambiguous impact is treated as affected

When Movement 3 cannot confidently determine whether a new claim affects an existing report entry, the implementation chooses the conservative affected path.

### Classification threshold

The current classifier uses a fixed confidence threshold of `0.4`.

### Thin API layer

Routes validate inputs, call existing orchestration or workflow-service functions, and translate results or domain errors into HTTP responses. Workflow decisions remain outside the route layer.

### Pure agents

Agent modules do not perform I/O. Database and storage access remains in orchestration.

### Review attribution

`decided_by` is currently a free-text field. The implementation does not include an authentication or user-identity subsystem.

### Database enum reconciliation

Startup initialization includes additive reconciliation for PostgreSQL enum values because `create_all()` does not modify existing PostgreSQL enum types automatically.

---

## 14. Scope Cuts and Current Limitations

These are explicit implementation boundaries, not claims that the missing features exist.

1. **No external LLM is used in the tested implementation.** Classification, extraction, rule validation, impact analysis, and update drafting are deterministic Python logic using keyword, pattern, and heuristic processing.
2. **No native PDF or DOCX parsing.** The current analysis path operates on UTF-8-decodable text.
3. **No standalone filesystem watcher or background daemon.** The `WATCHING` state is advanced through an explicit API submission.
4. **pgvector is enabled but unused.** No embedding or similarity-search capability is implemented.
5. **No authentication or authorization.**
6. **No Alembic migration framework.** Schema setup relies on model initialization and additive enum reconciliation.
7. **Concurrency is not stress-tested.**
8. **Cost accounting is limited to stage timing.** There are no external model operations whose cost can currently be measured.
9. **The deployed smoke run did not exercise a rule-set-driven review and commit cycle.** The deeper review and Movement 3 behaviors are covered by the repository test suite.

These boundaries are documented explicitly so the repository does not claim functionality that is not present.

---

## 15. Reproducibility

From a fresh clone:

```bash
git clone https://github.com/Paraass/project-sentinel.git
cd project-sentinel

cp .env.example .env

docker compose up -d --build

curl http://localhost:8000/health
```

Then open:

- Frontend: http://localhost:5173
- API docs: http://localhost:8000/docs

Run tests with:

```bash
docker compose --profile test run --rm backend-test
```

---

## 16. AI Disclosure

AI coding assistance was used extensively during development, consistent with the assignment's explicit allowance for AI-assisted implementation.

Architecture decisions, scope decisions, code review, test review, and validation were performed against the actual requirements and observed implementation. Generated output was not treated as evidence of functionality without validation.

---

## 17. Submission Notes

- The repository is intended to remain private according to the assignment's submission instructions.
- Live deployment URLs are provided above for evaluator access.
- The demo video, one-page write-up, and architecture diagram are separate submission artifacts.
- No API keys, database passwords, or other secrets belong in this repository.
- Use `.env` for local secrets and keep it gitignored.
- The README distinguishes smoke-test evidence from deeper integration-test coverage.
- A successful run without a rule set must not be interpreted as proof that the human-review and report-commit path was exercised in that deployment smoke test.

---

## 18. Repository Structure

```text
project-sentinel/
├── backend/
│   ├── app/
│   │   ├── agents/
│   │   ├── api/
│   │   ├── orchestration/
│   │   ├── persistence/
│   │   ├── storage/
│   │   ├── config.py
│   │   └── main.py
│   ├── tests/
│   ├── Dockerfile
│   └── requirements.txt
├── frontend/
│   ├── src/
│   ├── Dockerfile
│   ├── package.json
│   └── vite.config.ts
├── docker-compose.yml
├── .env.example
├── README.md
└── LICENSE
```

---

## 19. License

See [`LICENSE`](LICENSE).
