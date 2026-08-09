# Project Sentinel

AI-powered software engineering documentation analysis system.

This repository contains the reviewed implementation foundation. The application is being built incrementally so each engineering layer can be tested before the next is introduced.

## Current foundation

- FastAPI backend
- React + TypeScript frontend
- PostgreSQL + pgvector
- Docker Compose
- Environment-driven configuration
- Structured JSON logging
- Backend liveness endpoint
- Basic backend test

## Run

1. Copy `.env.example` to `.env`.
2. Run `docker compose up --build`.
3. Open `http://localhost:5173`.
4. Check backend health at `http://localhost:8000/health`.

Application capabilities will be added incrementally in later build orders.
