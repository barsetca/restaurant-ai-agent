# Restaurant AI Agent

## Overview

This repository contains the infrastructure, FAQ knowledge-base/RAG, deterministic LangGraph routing, and PostgreSQL-backed conversation checkpoints for a single synthetic restaurant. FAQ data is read from Google Sheets, embedded through OpenAI, indexed in Qdrant, and converted into a grounded Russian answer. When evidence is missing, conflicting, or an external dependency fails, the assistant returns a fixed safe fallback.

MCP, menu/order business flows, Google Calendar, admin UI, and operator handoff are not implemented yet.

## Architecture

- FastAPI owns the application lifespan, async PostgreSQL engine, and optional Telegram polling.
- `app/rag/sheets_source.py` reads and validates the `FAQ` worksheet with read-only Google Sheets scope.
- `app/rag/sync_service.py` compares content hashes, embeds only added/changed entries, and safely upserts/deletes Qdrant points.
- `app/rag/retrieval.py` applies configurable `top_k` and score threshold.
- `app/rag/answer_service.py` asks OpenAI for a structured support/conflict decision and only returns a grounded answer.
- `app/bot` reuses one `python-telegram-bot` polling application and derives versioned HMAC thread IDs without storing raw chat IDs in graph state.
- `app/agent` compiles one deterministic LangGraph with an official async PostgreSQL checkpointer; same-thread messages are serialized while different threads remain concurrent.
- PostgreSQL remains the application database; M2 does not use it as a second FAQ index-state store.

See `docs/ARCHITECTURE.md`, `docs/DATABASE_ARCHITECTURE.md`, and `docs/DECISIONS.md`.

## Stack

- Python 3.12 and `uv`
- FastAPI, SQLAlchemy async, asyncpg, Alembic, PostgreSQL
- LangChain Core/OpenAI, OpenAI embeddings/chat
- LangGraph and `langgraph-checkpoint-postgres`
- Qdrant and `qdrant-client`
- `gspread` with a Google service account
- `python-telegram-bot` long polling
- pytest, HTTPX, Docker Compose

All direct Python dependencies are pinned in `pyproject.toml` and resolved in `uv.lock`.

## Prerequisites

- Python 3.12
- `uv` 0.9.11 or compatible
- Docker with Docker Compose for the complete local stack
- For live FAQ use only: an OpenAI API key and a Google service-account JSON file

## Environment

```bash
cp .env.example .env
```

`DATABASE_URL` is required and has no Python default. The values in `.env.example` are demonstration-only. Keep `POSTGRES_PASSWORD` and the password in `DATABASE_URL` consistent. Never commit `.env` or credentials.

When Telegram is enabled, generate one independent thread-identity secret and keep it stable in the ignored `.env`:

```bash
openssl rand -hex 32
```

Store the output as `THREAD_ID_HMAC_SECRET`; do not reuse the Telegram token. `THREAD_ID_HMAC_KEY_ID=v1` identifies the derivation namespace. Changing either value starts new conversation threads; existing checkpoints are not migrated automatically.

FAQ and Telegram are disabled by default, so ordinary local startup requires neither OpenAI/Google credentials nor a Telegram token. Relevant FAQ settings are:

```text
FAQ_ENABLED
OPENAI_API_KEY
OPENAI_CHAT_MODEL
OPENAI_EMBEDDING_MODEL
OPENAI_EMBEDDING_DIMENSIONS
GOOGLE_SHEETS_SPREADSHEET_ID
GOOGLE_CREDENTIALS_PATH
QDRANT_URL
QDRANT_COLLECTION
FAQ_RETRIEVAL_TOP_K
FAQ_RETRIEVAL_SCORE_THRESHOLD
FAQ_REQUEST_TIMEOUT_SECONDS
FAQ_MAX_CONTEXT_CHARACTERS
```

To use Google Sheets, create a service account, enable Google Sheets API access, share the spreadsheet with the service-account email as Viewer, and set `GOOGLE_CREDENTIALS_PATH` to the local JSON file. The adapter requests the read-only Sheets scope. The spreadsheet must contain a worksheet named `FAQ` with these headers:

```text
id | category | question | answer | updated_at | active
```

Use stable unique `id` values. Empty rows are skipped, inactive rows are removed from the searchable index, and invalid/duplicate/PII-like active rows abort synchronization so the previous usable index remains recoverable.

## Local run

Run PostgreSQL separately, set `DATABASE_URL`, then:

```bash
uv sync --frozen
uv run uvicorn app.main:app --host 0.0.0.0 --port 8000
```

`GET /health/live` reports process liveness. `GET /health/ready` returns `200` only when PostgreSQL accepts a query and, when Telegram is enabled, the checkpoint schema is usable. Failures return `503` without exposing the DSN.

## Docker

```bash
cp .env.example .env
docker compose up --build
```

This starts `backend`, `postgres`, and `qdrant` with persistent volumes. The credentials file is bind-mounted read-only into the backend when `GOOGLE_CREDENTIALS_PATH` points to a host file; it is not copied into the image. MCP is not started.

```bash
docker compose config
docker compose ps
curl --fail http://localhost:8000/health/live
curl --fail http://localhost:8000/health/ready
curl --fail http://localhost:6333/healthz
docker compose logs backend
```

## FAQ synchronization

Synchronization is an explicit, repeatable operator action and does not require an application restart:

```bash
uv run python -m scripts.sync_faq
docker compose exec backend /app/.venv/bin/python -m scripts.sync_faq
```

For offline development, a synthetic JSON source can replace Google Sheets, but OpenAI embeddings and Qdrant are still required by the production CLI:

```bash
uv run python -m scripts.sync_faq --fixture fixtures/faq_sample.json
```

The command prints JSON counters for added, updated, removed, unchanged, inactive, empty, and error records. Stable Qdrant point IDs prevent duplicates. A collection metadata point records the embedding model and vector size; a mismatch fails explicitly. If the embedding model changes intentionally, use a new collection name or perform an explicitly planned reindex—do not silently reuse incompatible vectors.

If a source unexpectedly contains zero active FAQ rows while the collection still contains FAQ points, synchronization fails without deleting the index. An intentional full cleanup requires an explicit destructive opt-in:

```bash
python -m scripts.sync_faq --allow-empty-source
```

Use this flag only after independently confirming that an empty source is intended. The collection metadata point is preserved. The default retrieval threshold is `0.30`, calibrated on the synthetic live FAQ set; re-evaluate it when the dataset or embedding model changes.

## Migrations

Migrations never run at application startup:

```bash
uv run alembic upgrade head
docker compose exec backend /app/.venv/bin/alembic upgrade head
```

Never point migration integration tests at a working database.

LangGraph's official saver owns its own additive schema. Initialize it explicitly after PostgreSQL is healthy and before enabling Telegram polling:

```bash
uv run python -m scripts.setup_checkpoints
docker compose run --rm --no-deps backend \
  /app/.venv/bin/python -m scripts.setup_checkpoints
```

The command is repeatable and protected by a PostgreSQL advisory lock. It creates/updates `checkpoint_migrations`, `checkpoints`, `checkpoint_blobs`, and `checkpoint_writes`. It is deliberately separate from application Alembic migrations and is never executed automatically at backend startup.

## Tests

Unit tests use synthetic fixtures and mocks and need no real Google/OpenAI/Telegram credentials:

```bash
uv run pytest tests/unit
```

Qdrant integration tests create and remove only unique `faq_test_*` collections:

```bash
TEST_QDRANT_URL=http://localhost:6333 uv run pytest tests/integration/test_faq_qdrant.py
```

Migration integration tests require a separately provisioned empty PostgreSQL database whose name contains `test`:

```bash
TEST_DATABASE_URL=postgresql+asyncpg://user:password@localhost:5432/restaurant_test \
  uv run pytest tests/integration/test_migrations.py
```

Official saver integration tests require a separate test PostgreSQL URL and remove only their unique synthetic thread rows:

```bash
TEST_CHECKPOINT_DATABASE_URL=postgresql+asyncpg://user:password@localhost:5432/restaurant_checkpoint_test \
  uv run pytest tests/integration/test_agent_checkpoint_postgres.py
```

Live Google Sheets/OpenAI/Qdrant verification has passed on synthetic FAQ data, including update and deactivation without restart. Telegram `/start` and FAQ chat flows have also been verified. HTTP diagnostics remain enabled, with centralized redaction of tokens, API keys, authorization headers, URL passwords, and exception tracebacks.

## Current status

M2, M3.1, and M3.2 are READY. M3.2 automated/live verification passed, including state recovery after a backend restart; the owner then configured a stable local `THREAD_ID_HMAC_SECRET` and confirmed `/start`, FAQ, order placeholder, and unknown fallback in Telegram. Codex did not repeat the owner's manual chat checks. PERF-001 remains open: the owner observes a roughly 5–10 second first FAQ response with faster subsequent responses, but the cause has not been measured. See `docs/DEVELOPMENT_STATUS.md` for exact results.

## Next milestones

The next milestone is M4 MCP Tools and is NOT STARTED. Do not start it automatically. Full LLM conversation history, checkpoint retention, distributed multi-instance coordination, orders, Calendar, admin sync, evaluation, and PERF-001 instrumentation remain future work.
