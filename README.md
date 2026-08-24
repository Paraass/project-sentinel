# Project Sentinel

An AI-powered project documentation analyst that ingests related software-engineering documents, classifies and extracts claims from them, detects disagreements, validates the result against a configurable rule set, and maintains a versioned, human-reviewed report that stays current as new documents arrive.

> **Submission evidence note:** Repository implementation details below are based on the project implementation record and validation performed during development. Deployment evidence in Sections 9–10 was checked against the live Render services by the engineer running the submission on August 24, 2026. Claims about test counts or repository state are only stated where they are supported by the available implementation record or current deployment evidence.

---

## 1. What It Does

Project Sentinel implements three of the movements from its own locked behavioral architecture:

1. **Understand (Movement 1 — cold start).** Given an initial pile of documents, the system classifies each one (PRD / SRS / Architecture / API Spec / Meeting Notes / Unknown), extracts individual claims with source provenance (document + line), consolidates them into a grounded deliverable, and scans for claims that contradict each other across documents.
2. **Examine (Movement 2 — rule validation and review).** A user-supplied, data-driven rule set is checked against the grounded deliverable and the underlying claims. Violations and "cannot evaluate" outcomes become independently reviewable items. A human (or any caller of the API) must explicitly approve, reject, or defer each item, then explicitly close the review before anything commits.
3. **Stay alive (Movement 3 — incremental updates).** A new document can be submitted against an already-committed report. Only that new document is classified and extracted — existing documents and the existing report are never reprocessed. The system determines which parts of the current report are affected, drafts a focused update proposal for only those parts, and surfaces any direct contradiction as a conflict rather than resolving it automatically. The proposal re-enters the same review queue as Movement 2.

Every commit produces an immutable, versioned report plus a changelog entry recording what changed, when, and which source document caused it.

---

## 2. Architecture

```
Frontend (React + TypeScript, Vite, Tailwind)
        │  HTTP/JSON
        ▼
FastAPI routes (thin — parse request, call one existing function, translate result)
        │
        ▼
Orchestration (LangGraph workflow + workflow_service.py)
        │
        ├──▶ Agent modules (classifier, extractor, consolidator, conflict_scanner,
        │     rule_validator, impact_analyzer, update_drafter)
        │     — pure functions: take data in, return data out, perform no I/O
        │
        ├──▶ Persistence (SQLAlchemy models over PostgreSQL)
        │
        └──▶ Document storage (content-addressed local filesystem)

PostgreSQL + pgvector — pgvector extension is enabled at database init time but is
not currently used by any query; no embedding/similarity search is implemented.
```

The locked boundary — API → Orchestration → Agents, with Orchestration alone talking to Persistence/Document Storage, and Agents never performing I/O — is the shape the code was built to, module by module, from the first backend batch onward. Routes in `app/api/` do not call agent functions directly, and agent functions do not import the database or storage modules.

**Watcher, as actually built:** there is no standalone filesystem-watching daemon or background process. `WATCHING` is a workflow *state* — a run sits there, durably, after a commit — and a new document is introduced by an explicit API call (`POST /runs/{run_id}/documents`), not by anything polling a directory. This is narrower than "watcher" might imply and is called out again in Section 14.

Logging is structured JSON to stdout via the standard library's `logging` module (no external logging framework). Configuration is environment-variable driven through a single `pydantic-settings` `Settings` object.

---

## 3. Workflow

The actual state machine, as implemented in `workflow_service.py` and driven by `graph.py`:

```
INTAKE_PENDING → CLASSIFYING → EXTRACTING → CONSOLIDATING → CONFLICT_SCAN → COMPLETED
                                                                                  │
                                                              (rule set supplied) │
                                                                                  ▼
                                                          RULE_VALIDATION_PENDING → VALIDATING
                                                                                        │
                                                                                        ▼
                                                                          AWAITING_HUMAN_REVIEW
                                                                                        │
                                                                    (explicit close_review)
                                                                                        ▼
                                                    REVIEW_CLOSED → COMMITTING → REPORT_COMMITTED → WATCHING
                                                                                                          │
                                                                                    (new document supplied)
                                                                                                          ▼
                                            NEW_DOCUMENT_DETECTED → IMPACT_ANALYSIS → FOCUSED_UPDATE_DRAFTING
                                                                                                          │
                                                                                                          ▼
                                                                                    AWAITING_HUMAN_REVIEW (loop)
```

Every state transition is validated against an explicit allow-list (`_ALLOWED_TRANSITIONS`) and persisted as a `StageCheckpoint` row before any work for that stage begins, and again when it completes. A stage interrupted mid-work — process killed between `start_stage` and `complete_stage` — is resumed from that same stage on the next invocation, never restarted from the beginning and never silently skipped; this is proven directly by tests that start a stage, deliberately never complete it, open a brand-new database engine/session (simulating a fresh process with no memory of the interrupted one), and assert the resume logic identifies the correct incomplete stage.

Later stages read earlier stages' output from their persisted `StageCheckpoint.output_data`, never from in-memory state carried by the workflow engine itself — this is what makes "don't rerun already-completed work" true as a mechanism, not just a claim: no LangGraph checkpointer is configured, deliberately, so there is exactly one source of durable truth (Postgres), not two competing ones.

Approve/reject/defer decisions are recorded on independently addressable `ReviewItem` rows, each with a required attributed decider, optional reason, and timestamp. Rejecting one item never touches any other item's row. A report only becomes the current, retrievable baseline (`Report.is_current = True`) inside the atomic `COMMITTING` stage, which only runs after an explicit `REVIEW_CLOSED` — there is no code path that reaches `COMMITTING` automatically from `AWAITING_HUMAN_REVIEW`.

---

## 4. Human Review and No-Bluffing

- Every finding, proposed change, and conflict Movement 2/3 produce becomes its own `ReviewItem` row — independently visible, independently decidable.
- Decisions are made through the API (`POST /review-items/{item_id}/decision`) with an explicit `decided_by` and optional `reason`, both persisted.
- A rejected or deferred item is never deleted — it stays in the table exactly as decided, for audit.
- `get_current_report()` returns `None` until a commit has actually happened; nothing in the codebase treats an uncommitted deliverable as if it were an approved report.
- The rule validator has three, and only three, outcomes per rule: satisfied (no row written), `VIOLATION` (row written, with evidence), or `CANNOT_EVALUATE` (row written, distinctly typed, never collapsed into a pass). A rule that can't be checked from available evidence is never silently treated as passing.
- The claim extractor rejects any claim missing document id, source location, or text before it can enter the deliverable — this is enforced by a dedicated validation function, directly unit-tested by constructing a deliberately malformed claim and asserting it's rejected.

---

## 5. Incremental Updates

`POST /runs/{run_id}/documents` is refused (HTTP 409) unless the run is genuinely in `WATCHING`. When accepted, the new document is registered and classified/extracted in isolation — the impact analyzer then compares its claims against the **current committed report's content**, not against the original intake claims. Relevance between a new claim and an existing report statement has three outcomes: a confident match (same key on a key/value claim), a confident non-match (no shared key, no significant word overlap), or ambiguous — and ambiguous is always treated as affecting the section, never as unaffected, by explicit design in `impact_analyzer.py`.

Only affected report entries are touched by the drafted proposal; unaffected entries are carried into the next report version byte-for-byte from the current report's own stored content, never regenerated. A same-key, different-value pairing between the new claim and the existing report entry is written as a conflict, not auto-resolved — `update_drafter.py` has no code path that picks a value on its own. The resulting proposal re-enters the identical `ReviewItem`/`AWAITING_HUMAN_REVIEW` queue Movement 2 uses; there is no second approval mechanism.

A successful approved commit increments the report version, marks the previous version `is_current = False` without deleting or mutating its stored content, and writes a `ReportChangelogEntry` recording the new version number, a summary, the source document id(s) responsible, and the affected claim ids.

---

## 6. Accepted Inputs

Documents are stored as raw bytes (any content type is accepted and stored, content-addressed by SHA-256), but **classification and extraction only operate on UTF-8-decodable text** — content is decoded with `errors="replace"` before being passed to the classifier/extractor. There is no PDF, DOCX, or other binary-format parser anywhere in this implementation. The classifier and extractor were built and tested against plain-text synthetic engineering documents styled as PRDs, SRS documents, architecture notes, API specs, and meeting notes — the domain the assignment specifies — using keyword/pattern matching, not a general-purpose document parser.

---

## 7. API

All routes are defined in `app/api/health.py` and `app/api/runs.py`, registered in `app/main.py`.

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Liveness check |
| POST | `/runs` | Start a new Movement 1 run from uploaded documents |
| GET | `/runs/{run_id}` | Current run state, resume stage, document count |
| GET | `/runs/{run_id}/review-items` | The run's review queue |
| POST | `/review-items/{item_id}/decision` | Approve / reject / defer one item |
| POST | `/runs/{run_id}/review/close` | Explicit review closure (409 unless `AWAITING_HUMAN_REVIEW`) |
| GET | `/runs/{run_id}/report` | Current committed report (404 if none committed) |
| GET | `/runs/{run_id}/report/{version}` | A specific historical report version |
| GET | `/runs/{run_id}/changelog` | All changelog entries for the run |
| POST | `/runs/{run_id}/documents` | Submit a new document into Movement 3 (409 unless `WATCHING`) |

Interactive documentation is available at `/docs` (Swagger UI) once the backend is running. Domain errors (`RunNotFoundError`, `InvalidStateTransitionError`, `StageNotInProgressError`, and the base `WorkflowError`) are translated to `404`/`409`/`400` respectively by dedicated FastAPI exception handlers, so a route never needs to construct these HTTP responses itself.

---

## 8. Local Setup

```bash
cp .env.example .env
# edit .env to set POSTGRES_USER / POSTGRES_PASSWORD / POSTGRES_DB and related values

docker compose up -d --build
```

- Backend: `http://localhost:8000`
- Swagger/OpenAPI: `http://localhost:8000/docs`
- Frontend: `http://localhost:5173`

Backend tests run through a dedicated Compose profile, isolated from the production image:

```bash
docker compose --profile test run --rm backend-test
```

---

## 9. Render Deployment

The current submission is deployed on Render:

- **Frontend:** [Project Sentinel](https://sentinel-frontend-w2o6.onrender.com)
- **Backend:** [Project Sentinel API](https://project-sentinel-fk8f.onrender.com)
- **Backend health check:** [Health endpoint](https://project-sentinel-fk8f.onrender.com/health)
- **API documentation:** [Swagger UI](https://project-sentinel-fk8f.onrender.com/docs)

The deployment uses Render's free tier. Free instances can sleep after inactivity, so the first request after an idle period may be delayed.

The backend requires `DATABASE_URL`, `LOG_LEVEL`, `ENVIRONMENT`, `CORS_ORIGINS`, and `DOCUMENT_STORAGE_PATH` to be configured in the deployed environment. `CORS_ORIGINS` must include the deployed frontend origin.

---

## 10. Validation / Evidence

The deployed services were exercised directly on August 24, 2026.

### Backend

- `/health` returned HTTP 200 with:
  `{"status":"ok","service":"Project Sentinel","version":"0.1.0","environment":"development"}`
- Render logs showed successful application startup and Uvicorn listening on port 8000.
- Swagger UI was reachable at `/docs`.
- `POST /runs` was exercised through Swagger and returned HTTP 201.
- A deployment test run returned `current_state: "COMPLETED"`, `document_count: 1`, and `resume_stage: "COMPLETED"`.
- A second API test using a Markdown document also returned HTTP 201 and completed successfully.

### Frontend

- The deployed frontend loads successfully.
- The application shell exposes **Review**, **Report**, and **History** navigation.
- Opening a run-specific URL with `?run_id=<UUID>` successfully loads the run/review page and retrieves the backend run state.
- The tested completed run displayed 1 document, 0 pending review items, and no committed report.

### Important test boundary

The deployment smoke runs used documents without a rule set. Therefore, those runs correctly completed without creating review items or a committed report. This validates intake, processing, persistence, API connectivity, and the deployed frontend/backend path, but it does **not** by itself prove the human-review, commit, report-versioning, or Movement 3 path.

Those deeper behaviors are covered by the repository's integration tests described in Section 11.

---

## 11. Tests

I have direct, firsthand knowledge of these test files because I wrote and ran them myself against a real PostgreSQL instance during this project's build — but I have not re-run them against the repository's current state in this documentation pass, and cannot confirm they still pass right now without doing so. The engineer's own most recent reported full-suite result should be treated as authoritative for the current pass/fail count, not the description below.

| File | What it proves |
|---|---|
| `test_health.py` | Liveness endpoint |
| `test_document_storage.py` | Content-addressed storage: bytes written and read back correctly, deterministic hashing, metadata correctness, survival across a genuinely separate OS process |
| `test_workflow_service.py` | State transitions persist; invalid transitions are rejected; a completed checkpoint's output survives a brand-new database session; interrupted-stage resume identifies the correct stage; a completed stage is never re-entered |
| `test_graph.py` | The LangGraph skeleton compiles; the cold-start path runs end to end; conditional routing correctly resumes an interrupted run and correctly no-ops a completed one |
| `test_movement1.py` | Deterministic classification (including a below-threshold "uncertain" case that is never silently treated as confident); claim provenance rejection; conflict detection with both sources retained; zero-conflict as a valid outcome; **prompt-injection-style document content is extracted as ordinary data and never alters control flow** |
| `test_rule_validation.py` | Rule set/rule persistence; satisfied rules produce zero findings; violations carry evidence and correct rule/version identity; `CANNOT_EVALUATE` is never a silent pass; independent findings per rule; **prompt-injection-style content in a rule check does not change the validation outcome**; a specific resume-safety regression (rule set id surviving an interruption without being re-supplied) |
| `test_review_and_movement3.py` | Review cannot commit without explicit closure; first commit produces Report v1; a report survives a new session; changelog persists; rejected/deferred decisions remain recorded; new-document intake is scoped (existing documents are not reprocessed); impact analysis reads the committed report; ambiguous relevance defaults to affected; unaffected content is byte-identical after a commit; contradictions become conflicts, never silent resolutions; an approved update produces Report v2 while v1 remains unchanged; Movement 3 resume from an interrupted analytical stage |
| `test_api_runs.py` | The HTTP API surface itself: run creation/retrieval, 404s for missing resources, review item retrieval and decision persistence through the API, review closure refusing to bypass the required state, report/version/changelog retrieval through the API, and the new-document endpoint entering Movement 3 without re-running Movement 1 |

**Concurrency:** no dedicated test exercising two runs (or two operations on the same run) executing genuinely concurrently exists in what I built. Each run is scoped by its own `run_id` and checkpoint rows, and commits are atomic single-transaction writes, which is architecturally suggestive of safety — but this is an assumption, not something proven by a concurrency-specific test in this codebase.

---

## 12. Assignment Requirements Mapping

| Requirement | Implementation / Evidence | Status |
|---|---|---|
| 1. Visible, watchable stages with branching | `WorkflowState` enum + `StageCheckpoint` rows per stage; `route_entry`'s conditional routing branches on durable state (resume/skip/end) | Implemented, tested (`test_graph.py`, `test_review_and_movement3.py`) |
| 2. Kill/resume, no lost work | Stage interrupted between `start_stage`/`complete_stage`, resumed via a brand-new DB engine/session with no shared memory | Implemented, directly tested at multiple stages including a specific resume-safety regression fix |
| 3. Item-level human approval gate | `ReviewItem` rows, independently decidable, `decide_review_item()` | Implemented, tested via service layer and API |
| 4. Machine-drivable, approval as an exposed operation | `POST /review-items/{item_id}/decision` and `POST /runs/{run_id}/review/close` are ordinary HTTP endpoints, callable by any client; the state machine treats human and programmatic callers identically | Implemented at the API/orchestration level; not separately demonstrated by a distinct non-human caller beyond the test suite itself |
| 5. Never bluffs | Provenance-rejecting extractor, explicit `CANNOT_EVALUATE` outcome, atomic commit | Implemented, tested |
| 6. Stranger can run it in minutes | `docker compose up -d --build` documented | Documented; not independently verified via an actual fresh-clone run in this session |
| 7. Real tests, no live key | No LLM/external API used anywhere in the tested path; all tests run against real Postgres | Implemented and tested |
| 8. Doesn't take orders from its documents | Dedicated prompt-injection-style tests in both Movement 1 extraction and Movement 2 validation | Implemented, tested |
| 9. Concurrent runs stay safe | Per-run scoping, atomic commits | Architecturally plausible; not proven by a dedicated concurrency test |
| 10. Knows what it cost | `StageCheckpoint.started_at`/`completed_at` give per-stage duration | Minimal — no operation-cost tracking exists (no LLM calls to cost, so none was built) |

---

## 13. Assumptions and Ambiguities

- **No LangGraph checkpointer is configured, on purpose.** Durability lives entirely in Postgres via `workflow_service.py`; a second, LangGraph-native persistence layer would duplicate that and create two possible sources of truth. Every node re-derives what to do from a database read, never from LangGraph's own state dict.
- **Terminal-state transitions are an explicit dict (`_TERMINAL_TRANSITIONS`), not inferred from list position.** An earlier version used a `_STAGE_SEQUENCE[-2]` positional trick that only worked when there was exactly one terminal transition in the whole sequence; once Movement 2 and the shared review/commit lifecycle added a second one, that trick was replaced with an explicit per-stage mapping.
- **Ambiguous Movement 3 relevance defaults to affected**, deliberately biased toward over-inclusion rather than silently dropping something that might matter.
- **Classification confidence threshold is `0.4`**, a fixed constant in `classifier.py`, not configurable at runtime.
- **API routes are intentionally thin** — every route's body is: validate input, call one existing `workflow_service`/`graph` function, translate the result or exception into an HTTP response. No route contains a workflow decision.
- **Agent modules perform no I/O** — every function in `app/agents/` takes plain data (strings, dicts, dataclasses) as input and returns plain data; reading document bytes or querying the database is always the orchestration layer's job, never the agent's.
- **`decided_by` is a free-text field**, not tied to any authentication system — there is no user identity model in this build.
- **Native Postgres enum types are reconciled additively at startup** (`init_models`), adding any Python-enum values missing from an already-existing database type, specifically because `create_all()` never alters an existing type on its own. This was added after a real stale-enum bug surfaced during development, not planned from the start.

---

## 14. What Was Cut / Limitations

Stated plainly, as instructed — these are not "future improvements," they are gaps:

- **No LLM is used anywhere.** Classification, extraction, rule checking, impact analysis, and update drafting are all deterministic, keyword/pattern/heuristic-based Python — a stated, deliberate scope decision (the assignment's own task explicitly did not require a live model or API key for the tested path), not an oversight. Whether this satisfies the spirit of "AI-powered" is a judgment call the reviewer should make directly rather than one this README should paper over.
- **No document format parsing beyond plain UTF-8 text.** No PDF, DOCX, or similar parser exists.
- **No real watcher/daemon.** `WATCHING` is a workflow state entered and left explicitly via API calls, not a background process observing a filesystem location.
- **`pgvector` is enabled but unused.** The extension is created at database init time; no embedding generation or similarity search exists anywhere in the codebase.
- **No authentication or authorization.** Anyone who can reach the API can create runs, decide review items, and submit documents.
- **No Alembic / migration framework.** Schema changes rely on `create_all()` plus an additive enum-value reconciliation step; this does not handle removing or renaming a column or enum value safely — that would require recreating the affected type or a real migration tool, which was explicitly deferred throughout this project's build.
- **Concurrency is not stress-tested**, as noted in Section 11.
- **The deployed frontend currently exposes Review, Report, and History navigation and supports run-specific URLs via `?run_id=<UUID>`.** The deployed run/review page was exercised successfully against the live backend. The smoke-tested run had no pending review items and no committed report because it was created without a rule set. The exact completeness of every frontend surface should still be judged from the current repository/UI rather than inferred from the navigation alone.

---

## 15. Reproducibility

```bash
git clone <this-repository>
cd <repository-root>
cp .env.example .env
docker compose up -d --build
curl http://localhost:8000/health
# frontend at http://localhost:5173, API docs at http://localhost:8000/docs
```

---

## 16. AI Disclosure

AI coding assistance was used extensively throughout this project's development, consistent with the assignment's own explicit encouragement to do so. Architecture decisions, scope boundaries, review of generated code, and validation of what was actually built versus what was merely intended were checked against the real requirements and the real implementation at each step, rather than accepted at face value from generated output alone.

---

## 17. Submission Notes

- This repository is intended to be private, per the assignment's submission instructions.
- Live deployment URLs and deployment evidence are documented in Sections 9–10.
- The demo video, one-page write-up, and architecture diagram are separate submission artifacts from this repository and are not reproduced here.
- No credentials, API keys, database passwords, or other secrets are included in this document or should be committed anywhere in this repository — use `.env`, which is gitignored, for all of that.
- The README intentionally distinguishes deployed smoke-test evidence from deeper behavior covered by the integration suite; a successful no-rule-set run must not be presented as proof of the review/commit path.
