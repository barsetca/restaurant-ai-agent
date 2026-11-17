# Restaurant AI Agent

## Overview

This repository currently contains the M1 runnable backend and infrastructure skeleton for a single-restaurant educational AI assistant. It deliberately does not implement RAG, LangGraph workflows, MCP tools, Google integrations, order business logic, or an admin UI.

## Architecture

The M1 process hosts FastAPI and, when explicitly enabled, one `python-telegram-bot` long-polling instance. PostgreSQL is the runtime database and Qdrant is provisioned for a later milestone. Database migrations are an explicit operator action and never run during application startup. See `docs/ARCHITECTURE.md` and `docs/DATABASE_ARCHITECTURE.md`.

## Stack

- Python 3.12, `uv`
- FastAPI and Uvicorn
- SQLAlchemy async engine with `asyncpg`
- Alembic and PostgreSQL
- Qdrant
- `python-telegram-bot` long polling (disabled by default)
- pytest and HTTPX
- Docker Compose

## Prerequisites

- Python 3.12
- `uv` 0.9.11 or compatible
- Docker with Docker Compose for the complete local stack

## Environment

Copy the template and adjust local values:

```bash
cp .env.example .env
```

Do not commit `.env` or credentials. `TELEGRAM_ENABLED=false` is the default, so no Telegram token is required. OpenAI and Google credentials are also optional in M1. Compose requires `POSTGRES_PASSWORD`; the template contains an explicitly demonstrational value. Keep `POSTGRES_PASSWORD` and the password embedded in `DATABASE_URL` consistent when changing it.

## Local run

Run PostgreSQL separately and set `DATABASE_URL` to its async SQLAlchemy URL, then:

```bash
uv sync --frozen
uv run uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Liveness is available at `GET /health/live`. Readiness is `200` only when PostgreSQL accepts a query and otherwise returns `503` without exposing the DSN.

## Docker

```bash
cp .env.example .env
docker compose up --build
```

This starts `backend`, `postgres`, and `qdrant` with persistent volumes. The MCP server is not started in M1. Apply migrations separately after PostgreSQL is healthy:

```bash
docker compose exec backend /app/.venv/bin/alembic upgrade head
```

Useful verification commands:

```bash
docker compose config
docker compose ps
curl --fail http://localhost:8000/health/live
curl --fail http://localhost:8000/health/ready
curl --fail http://localhost:6333/readyz
docker compose logs backend
```

## Migrations

For a local Python environment:

```bash
uv run alembic upgrade head
```

The initial migration creates the eight tables documented in `docs/DATABASE_ARCHITECTURE.md`. Never point test migration commands at a working database.

## Tests

Unit tests need no external credentials or live services:

```bash
uv run pytest tests/unit
```

Migration integration tests require a separately provisioned, empty PostgreSQL database whose database name contains `test`:

```bash
TEST_DATABASE_URL=postgresql+asyncpg://user:password@localhost:5432/restaurant_test \
  uv run pytest -m integration tests/integration
```

## Current status

M1 provides the application factory, lifespan-managed async engine, health endpoints, disabled-by-default Telegram bootstrap, initial schema, Compose stack, and baseline tests. Refer to `docs/DEVELOPMENT_STATUS.md` for verified command results and limitations.

## Next milestones

The planned next milestone is M2: FAQ ingestion, embeddings, Qdrant indexing, and fallback behavior. It is not implemented here and must not be started without a separate task. Later milestones cover LangGraph, MCP menu tools, orders, Calendar, admin sync, and evaluation.
