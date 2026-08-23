# Project Sentinel

An AI-powered Project Documentation Analyst that continuously analyzes software engineering documentation — PRDs, SRS, architecture documents, API specifications, sprint documents, meeting notes, test plans, release notes, risk registers, bug reports, and change requests — detects inconsistencies across them, validates them against configurable engineering rules, and maintains evidence-backed reports through a human-approved incremental update workflow.

## Status

This repository currently contains the **repository foundation** (Build Order 001): infrastructure, backend bootstrap, and frontend bootstrap. The application layer — document ingestion, the LangGraph workflow, agents, the review queue, and reporting - is implemented in later Build Orders and is not yet present. The backend currently exposes a single endpoint (`/health`); the frontend currently renders a single placeholder page.

## Stack

- **Frontend:** React, TypeScript, Tailwind CSS, shadcn/ui
- **Backend:** FastAPI
- **Workflow:** LangGraph (not yet implemented — foundation only)
- **Database:** PostgreSQL + pgvector
- **Storage:** local document storage
- **Deployment:** Docker Compose

## Running locally

Requires Docker and Docker Compose.

1. Copy the environment template and adjust values as needed:

   ```bash
   cp .env.example .env
   ```

2. Build and start the stack:

   ```bash
   docker compose up -d --build
   ```

3. Confirm the backend is healthy:

   ```bash
   curl http://localhost:8000/health
   ```

4. Open the frontend:

   ```
   http://localhost:5173
   ```

## Running backend tests

Tests run through a dedicated Compose profile, kept separate from the production backend image so test tooling never ships in it:

```bash
docker compose --profile test run --rm backend-test
```

## License

MIT — see [LICENSE](./LICENSE).
