# Restaurant AI Agent

## Overview

This repository contains the infrastructure, FAQ RAG, hybrid LangGraph order dialog, PostgreSQL-backed conversation checkpoints, and read-only MCP menu tools for a single synthetic restaurant. FAQ data is read from Google Sheets, embedded through OpenAI, indexed in Qdrant, and converted into a grounded Russian answer. Order language is extracted into a strict proposal while deterministic code owns cart mutations, pricing, validation, and confirmation.

Cart/contact/confirmed drafts persist in the official checkpoint. Durable order creation, Google Calendar, Menu/Settings sync, admin UI, and operator handoff are not implemented yet.

## Architecture

- FastAPI owns the application lifespan, async PostgreSQL engine, and optional Telegram polling.
- `app/rag/sheets_source.py` reads and validates the `FAQ` worksheet with read-only Google Sheets scope.
- `app/rag/sync_service.py` compares content hashes, embeds only added/changed entries, and safely upserts/deletes Qdrant points.
- `app/rag/retrieval.py` applies configurable `top_k` and score threshold.
- `app/rag/answer_service.py` asks OpenAI for a structured support/conflict decision and only returns a grounded answer.
- `app/bot` reuses one `python-telegram-bot` polling application and derives versioned HMAC thread IDs without storing raw chat IDs in graph state.
- `app/agent` compiles one state-aware LangGraph with an official async PostgreSQL checkpointer; same-thread messages are serialized and Telegram update replay is bounded.
- `app/orders` limits LLM authority to structured intent extraction and implements deterministic cart, contact, quote, and explicit confirmation rules.
- `app/menu` performs bounded menu search and deterministic integer-minor-unit cart calculation in transaction-level read-only PostgreSQL sessions.
- `app/catalog_ui` defines a Telegram-independent, pure five-screen catalog state, renderer, navigation and bounded callback protocol; it is not connected to the running bot yet.
- `mcp_server` exposes six read-only tools over stdio: `search_menu`, `get_dish`, `calculate_cart`, `list_categories`, `browse_menu`, and `inspect_cart`; `app/mcp` is the typed backend client adapter.
- PostgreSQL remains the application database; M2 does not use it as a second FAQ index-state store.

See [architecture](docs/ARCHITECTURE.md), [database architecture](docs/DATABASE_ARCHITECTURE.md), [decisions](docs/DECISIONS.md), [development status](docs/DEVELOPMENT_STATUS.md), [project handoff](docs/PROJECT_HANDOFF.md), and the [M5.2 design handoff](docs/M5_2_DESIGN_HANDOFF.md). Later decisions supersede older design-handoff text where they differ.

## Stack

- Python 3.12 and `uv`
- FastAPI, SQLAlchemy async, asyncpg, Alembic, PostgreSQL
- LangChain Core/OpenAI, OpenAI embeddings/chat
- LangGraph and `langgraph-checkpoint-postgres`
- Qdrant and `qdrant-client`
- `gspread` with a Google service account
- `python-telegram-bot` long polling
- Python MCP SDK v2 with local stdio transport
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
LLM_DEBUG_RESPONSE_JSON
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
MCP_STARTUP_TIMEOUT_SECONDS
MCP_TOOL_TIMEOUT_SECONDS
MCP_SHUTDOWN_TIMEOUT_SECONDS
```

`LLM_DEBUG_RESPONSE_JSON` по умолчанию выключен. Значение `true` действует только при
`APP_ENV=development`, `dev` или `local`; в production оно принудительно игнорируется.
Debug record содержит только распарсенную API DTO после структурной валидации: произвольные
строки блюда, selectors и clarification заменяются на `[REDACTED]`, структура и quantities
сохраняются, а запись ограничена 4000 символами. Prompts, user message, raw HTTP response,
headers, credentials и непроверенный model text не логируются.

To use Google Sheets, create a service account, enable Google Sheets API access, share the spreadsheet with the service-account email as Viewer, and set `GOOGLE_CREDENTIALS_PATH` to the local JSON file. The adapter requests the read-only Sheets scope. The spreadsheet must contain a worksheet named `FAQ` with these headers:

```text
id | category | question | answer | updated_at | active
```

Use stable unique `id` values. Empty rows are skipped, inactive rows are removed from the searchable index, and invalid/duplicate/PII-like active rows abort synchronization so the previous usable index remains recoverable.

## Local run

Run PostgreSQL separately, set `DATABASE_URL` for the host, then:

```bash
uv sync --frozen
uv run uvicorn app.main:app --host 0.0.0.0 --port 8000
```

The example `DATABASE_URL` and `QDRANT_URL` use Compose service names (`postgres`, `qdrant`); use host-reachable addresses outside Compose. Apply the migrations below before relying on application tables. If Telegram is enabled, initialize the separate checkpoint schema before starting polling.

`GET /health/live` reports process liveness. `GET /health/ready` returns `200` only when PostgreSQL accepts a query and, when Telegram is enabled, the checkpoint schema is usable. Failures return `503` without exposing the DSN.

## Docker

```bash
cp .env.example .env
docker compose up --build
```

This starts `backend`, `postgres`, and `qdrant` with persistent volumes. The Compose bind mount uses host path `GOOGLE_CREDENTIALS_PATH` (or `/dev/null` when unset), read-only at the configured container path; credentials are not copied into the image. The MCP server is not a separate Compose service: the M5 order dialog starts it on demand through `MenuMcpClient`.

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

Migration `0002` adds explicit `menu_items.dietary_tags TEXT[] NOT NULL DEFAULT '{}'`. Apply it through the normal explicit Alembic command before using M4 tools against a database. Dietary tags are source data; they are never inferred from ingredients or allergens. The matching Google Sheets Menu column and synchronization remain M7 work.

## Development menu seed

After applying migration `0002`, populate a development database with the 15-item synthetic European menu using an explicit command:

```bash
uv run python -m scripts.seed_menu
docker compose exec backend /app/.venv/bin/python -m scripts.seed_menu
```

The command is idempotent: stable UUIDs and `dev-menu-*` source IDs identify only its own rows. A repeated run restores those rows to the declared seed values without creating duplicates or changing other menu records. It performs no schema migration, does not run at backend startup, and never deletes arbitrary data. Review `DATABASE_URL` before running it; the command writes to the configured database and prints only safe JSON counters.

## MCP menu tools

The server can be launched by an MCP host over stdio:

```bash
uv run python -m mcp_server
```

The backend-facing `MenuMcpClient` launches that fixed module command with a restricted child environment and validates structured results. All tools read PostgreSQL in read-only transactions:

- `search_menu(query?, category?, dietary_tags?)` returns at most 20 available dishes; omitted/blank query browses the menu in deterministic order;
- `get_dish(dish_id)` returns current details, price, dietary data, and availability;
- `calculate_cart(items)` requires a nonempty cart, aggregates duplicate IDs and returns the authoritative current EUR `subtotal_minor` without delivery;
- `list_categories()` returns categories containing available dishes, counts, and the category string as both ID and name;
- `browse_menu(category, offset=0, limit=5)` pages available dishes in one required, nonblank category (maximum 120 characters). Offset is non-negative and limit is 1–20. PostgreSQL orders by lowercased name and dish ID, returns `has_more`, and does not create a catalog snapshot. An unknown category returns an empty page;
- `inspect_cart(items)` accepts an empty list, preserves input order and reports current prices and availability. Missing items retain ID and quantity with null price fields; unavailable items retain their price but do not contribute to `available_subtotal_minor`. It does not create a quote, mutate cart state, or authorize checkout.

Cart inputs are bounded to 20 lines, quantity 1–20 per dish after aggregation, and 100 total units. Missing/unavailable dishes, unsupported currencies, or invalid items reject the complete `calculate_cart` calculation; `inspect_cart` reports missing/unavailable items instead and does not replace strict checkout calculation. Search permits a query up to 200 characters and at most 10 dietary tags.

## M5 cart and confirmation

The Telegram order flow supports menu search/details, Russian word forms through canonical lookup plus PostgreSQL full-text fallback, atomic multi-item add, ordered multi-action plans for add/set/decrement/remove, clear/view, delivery contacts, ASAP or exact `DD.MM.YYYY HH:MM` delivery time, authoritative quote, and explicit confirmation. The read-only category tools do not yet imply a Telegram catalog UI. The LLM receives the current non-contact message plus a bounded allowlisted `OrderContext` and returns a typed proposal or cart plan; backend code resolves dish IDs and owns cart mutations, prices and validation. Stored contacts and full checkpoint are excluded from the LLM context; a user may nevertheless type personal data in an ordinary non-contact message. Multi-action dishes are resolved and validated before a candidate cart is committed in one state update.

Outside contact collection, bounded semantic clarification and server-side choices support safe continuation. During name, phone, address and time collection or editing, ordinary text is handled only by the deterministic validator for the expected field, without FAQ or LLM calls; the dedicated cancel-checkout callback preserves the cart. Session- and revision-bound callbacks reject stale actions, and the bounded update replay guard prevents duplicate mutations.

`restaurant_settings` must contain a canonical non-negative integer `delivery_fee_minor`. There is no application default. For the approved development value:

```sql
INSERT INTO restaurant_settings (key, value)
VALUES ('delivery_fee_minor', '350')
ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now();
```

The final summary has revision-bound inline actions: **Подтвердить / Изменить / Отменить**. Edit offers cart, name, phone, address, time, and fresh-summary choices; a real change invalidates the previous quote and requires explicit confirmation again. Cancel requires a second **Да, отменить** action. Stale callbacks are rejected before mutation. A cart-only strict typo fallback can propose a numbered candidate, but never silently selects it.

Confirmation produces an immutable checkpoint `confirmed_order` for M6 and replies that the order has not yet been placed. «Новый заказ» archives it in FIFO order and starts an empty draft; at most five current/archive snapshots are retained per thread. `/start` is non-destructive and confirmed snapshots survive draft cancellation. Telegram responses use one sanitized delivery helper: only pre-delivery `ConnectError` gets one bounded retry; ambiguous timeouts are not retried. None of these actions writes `orders`, `order_items`, or `calendar_jobs`.

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

MCP stdio integration tests require a separately migrated PostgreSQL database whose name contains `test`. They insert and remove only unique synthetic menu rows:

```bash
TEST_MCP_DATABASE_URL=postgresql+asyncpg://user:password@localhost:5432/restaurant_mcp_test \
  uv run pytest tests/integration/test_mcp_stdio_postgres.py
```

The M5 integration test uses the same guarded test-only URL, real stdio MCP, official checkpoint, and restores its synthetic setting/menu rows:

```bash
TEST_MCP_DATABASE_URL=postgresql+asyncpg://user:password@localhost:5432/restaurant_m5_test \
  uv run pytest tests/integration/test_m5_order_postgres.py
```

Manual Telegram checks should use synthetic contact data and cover `/start` in idle/draft/confirmed; multi-item and ordered multi-action requests; typo and ambiguity clarification without partial mutation; inline ASAP/manual time; FAQ outside contact collection; deterministic handling and cancellation during contact collection; summary editing; stale callbacks and replay; restart recovery; repeated confirmation; and preservation of earlier confirmed snapshots. Verify afterward that business order/calendar tables remain unchanged.

Live Google Sheets/OpenAI/Qdrant verification has passed on synthetic FAQ data, including update and deactivation without restart. Telegram `/start` and FAQ chat flows have also been verified. HTTP diagnostics remain enabled, with centralized redaction of tokens, API keys, authorization headers, URL passwords, and exception tracebacks.

## Current status

M1–M4 are accepted/READY. M5 remains **IN PROGRESS**. M5.2-A2.3 passed owner manual Telegram acceptance; M5.2-A3.1 and M5.2-A3.2 are **ACCEPTED**. M5.2-A3.3 is **IMPLEMENTED / READY FOR OWNER REVIEW**, not accepted: the «Меню» entry, five catalog screens, checkpoint acknowledgement, replay, same-thread serialization and user-initiated recovery are wired. Inline catalog navigation is edit-first; the explicit «Меню» recovery sends a fresh HOME so client-side deletion of old Telegram history cannot make a successful edit invisible. Catalog cart mutations remain disabled until A3.4. Automated unit and isolated PostgreSQL saver checks pass; Telegram LIVE was not rerun. M6 and M7 are **NOT STARTED**. No durable order or Calendar side effect exists in M5. PERF-001 and checkpoint PII retention/pruning remain open. See [development status](docs/DEVELOPMENT_STATUS.md) for evidence and limits.

## Next milestones

The next step is owner review of M5.2-A3.3. A3.4 and later slices do not start automatically. M6 durable orders/Calendar and M7 admin/Menu/Settings sync remain future work. Checkpoint retention, distributed multi-instance coordination, evaluation and PERF-001 instrumentation also remain open. See the [M5.2 design handoff](docs/M5_2_DESIGN_HANDOFF.md) and [project handoff](docs/PROJECT_HANDOFF.md).
