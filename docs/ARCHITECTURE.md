# Архитектура Restaurant AI Agent

**Статус:** целевая архитектура; M1, M2, M3.1, M3.2 и M4 приняты; M5 реализован и ожидает ручной Telegram acceptance владельца. Реальное состояние — `DEVELOPMENT_STATUS.md`.

## Контекст
Telegram (`python-telegram-bot`, long polling) → FastAPI application / LangGraph → ветви FAQ RAG, меню через MCP и оформление заказа. Один backend deployment + локальный отдельный MCP process; без микросервисной платформы.

## Компоненты
- `app/bot`: получение Telegram updates, привязка user/thread, передача сообщений в workflow.
- `app/api`: health и служебные endpoints; `app/admin`: защищенный UI и запуск синхронизации.
- `app/agent`: typed LangGraph state, state-aware hybrid FAQ/order routing, bounded Telegram replay guard и semantic history, application service и official PostgreSQL checkpointer.
- `app/orders`: allowlisted `ContextBuilder`, OpenAI-compatible flat strict DTO, action-specific DTO normalization с raw-output PII gate и повторной domain validation в `action/actions/clarify/unsupported`; распознанный hard quantity overflow преобразуется backend adapter в typed `validation_error`, максимум один LLM repair retry разрешен только для allowlisted missing/count rules, adapter создает single proposal либо ordered cart plan, deterministic cart/contact/time/confirmation state machine, read-only delivery settings repository и MCP orchestration. Optional redacted DTO debug logging выключено по умолчанию и разрешено только в local/dev environments.
- `app/rag`: read-only Google Sheets `FAQ` adapter, валидация, LangChain/OpenAI embeddings, идемпотентный Qdrant sync, retrieval и grounded answer service.
- `app/menu`: typed contracts, SQLAlchemy repository с transaction-level `READ ONLY`, bounded search и детерминированный cart subtotal в integer minor units.
- `mcp_server`: отдельный Python MCP SDK v2 process, stdio; read-only tools `search_menu`, `get_dish`, `calculate_cart` читают PostgreSQL.
- `app/mcp`: backend client adapter; запускает фиксированный Python module, передаёт только DB URL/log level, применяет startup/tool/shutdown timeouts и валидирует structured output.
- `app/orders`: детерминированные расчеты, валидация, сохранение подтвержденного заказа.
- `app/calendar`: создание события из сохраненного заказа, `calendar_jobs`, retry и идемпотентность.
- `app/db`: SQLAlchemy models, migrations, DB access.
- `app/sync` (будущий модуль): `FAQ`, `Menu`, `Settings` sync из Google Sheets.

## Основные потоки
1. **Telegram/LangGraph:** сообщение/callback + `update_id` → HMAC-derived versioned thread → один compiled `StateGraph` с `AsyncPostgresSaver` → duplicate/out-of-order guard → callback/destructive/exact deterministic guards. На contact-stage любой ordinary text немедленно идет только в validator ожидаемого поля; FAQ, embeddings, contextual extractor, semantic continuation и text-команды не вызываются. Вне contact-stage при необходимости строится allowlisted `OrderContext` и выполняется strict contextual extraction. Checkout callbacks содержат action + draft revision + independent checkout session ID; destructive callbacks используют собственный pending confirmation ID. Все callbacks проходят тот же `AgentService`/per-thread lock и проверяются по current stage/revision/session до mutation. Replay result, mutation и bounded semantic event сохраняются одним checkpoint node update.
2. **Меню M4/M5:** backend adapter → local stdio MCP server → `MenuService` → read-only PostgreSQL transaction. `search_menu` принимает optional query и при пустом запросе детерминированно возвращает до 20 available dishes; detail/availability берутся через `get_dish`; authoritative subtotal — только `calculate_cart`.
3. **Заказ M5:** LLM предлагает single action либо ordered plan максимум из 8 cart mutations (`add`, `set_quantity`, `decrement`, `remove`) с items/canonical lookup query. Deterministic `OrderDialog` сначала разрешает все блюда через read-only MCP, при неоднозначности сохраняет весь pending plan без mutation, отклоняет cross-operation повтор одной dish как конфликт, затем последовательно применяет операции к локальной candidate cart и только одним state update заменяет cart. Поэтому unknown/ambiguous/bounds failure не применяет ни одной операции; неупомянутые и помеченные «не трогай» блюда не меняются. Summary/edit/cancel callbacks revision-bound; изменение одного поля сохраняет остальные и инвалидирует quote. Cart-only strict fuzzy fallback всегда требует numbered selection. Destructive `new_order`, `clear` и `cancel` при непустом active unconfirmed draft сначала сохраняются как optional pending action с unique short session ID и требуют отдельного callback, совпадающего по stage/revision/session; до confirm draft не меняется. Confirmation повторно рассчитывает menu quote и fee; changed snapshot повышает draft revision и требует reconfirm. Результат — immutable `confirmed_order` и bounded FIFO archive в checkpoint, не business order row.
3. **Заказ:** корзина → server-side расчет и проверка доставки → сбор контактов → показ итога → явное подтверждение → повторная валидация → PostgreSQL order + snapshots → `calendar_jobs` → Google Calendar.
4. **Изменение FAQ в M2:** оператор запускает `python -m scripts.sync_faq` → read-only Sheets/fixture → полная валидация → сравнение `content_hash` → embeddings только измененных записей → upsert → удаление stale points. Пустой active source при непустом index блокируется; намеренная очистка требует `--allow-empty-source` и сохраняет metadata point. Рестарт backend не нужен. Admin UI и sync `Menu`/`Settings` остаются будущими этапами.

## Границы ответственности
- LLM не вычисляет цены и не совершает необратимые действия без backend validation.
- MCP инструменты меню и M5 settings repository только читают данные; запись заказа и Calendar остаются M6.
- MCP tool bounds: 20 search results, 10 dietary tags, 20 distinct cart lines, quantity 1–20 после aggregation и максимум 100 units. SQL LIKE wildcards экранируются.
- `dietary_tags` — явные source данные PostgreSQL; их запрещено выводить из отсутствия ingredients/allergens. Sheets Menu sync этого поля относится к M7.
- PostgreSQL — runtime source of truth для меню/настроек/заказов; Google Sheets — редактируемый источник; Qdrant — runtime searchable index FAQ, не БД заказов.
- Graph state содержит только JSON-safe dialog/replay primitives: message/intent/response/error, pseudonymous conversation ID, callback action/revision/optional confirmation ID, independent checkout session ID, cart, optional pending destructive/semantic state, pending single-action batch либо multi-action plan/candidates, validated contacts, quote, current/archive confirmed snapshots, bounded processed updates и до 10 typed semantic events без raw user/assistant history. Callback data не содержит PII; credentials, Telegram objects, clients и ORM entities в state не входят.
- M3.2 использует `langgraph-checkpoint-postgres` `AsyncPostgresSaver` поверх отдельного Psycopg async pool; SQLAlchemy/asyncpg остаётся клиентом бизнес-БД и health query.
- Saver и pool создаются один раз на FastAPI lifespan только при `TELEGRAM_ENABLED=true`, schema readiness проверяется до polling, ресурсы закрываются после остановки polling. DDL не выполняется на startup.
- `scripts.setup_checkpoints` явно и повторяемо вызывает official `setup()` под session advisory lock. Official tables не управляются Alembic приложения.
- Сообщения одного `thread_id` сериализуются per-thread `asyncio.Lock`; разные thread обрабатываются параллельно. Один Telegram polling instance остаётся deployment invariant; multi-instance distributed locking не реализован.

## Инфраструктура
FastAPI, PostgreSQL, Qdrant в Docker Compose; FAQ и M5 dialog работают внутри существующего backend. Credentials монтируются read-only и не копируются в image. MCP не является Compose service: `MenuMcpClient` запускает локальный stdio subprocess on demand. Telegram polling допускается только в одном backend instance и выключен по умолчанию.

## Безопасность
Contact stages имеют раннюю routing boundary и пустой LLM action allowlist: text имени, телефона, адреса и времени не попадает ни в order LLM, ни в FAQ retrieval/embeddings/answerer. Semantic history записывает только typed `contact_input`, но не значение поля. `OrderContext` не принимает contacts, raw checkpoint, callback/session data, responses или snapshot bodies.

Credentials только из environment/секретов; тестовые данные; FAQ validation отклоняет email/phone-like content; отсутствие исходного пользовательского текста в FAQ logs; ограниченный контекст и timeouts внешних API. Contextual order LLM получает только `OrderContext`: без contacts, IDs, callback/session data, raw checkpoint/snapshots/replay responses; contact stages не вызывают contextual extractor, а obvious PII patterns вне их fail closed. Semantic history хранит только typed action/result и optional trusted public dish label. Regex gate не считается абсолютной PII detection. MCP child получает минимальный environment с `DATABASE_URL` и `LOG_LEVEL`, не получает Telegram/OpenAI/HMAC secrets; DSN/password зарегистрированы в redactor child process. Expected menu errors не содержат DB details, unexpected errors логируют только exception type. Telegram chat ID преобразуется HMAC-SHA256 с отдельным `THREAD_ID_HMAC_SECRET`; versioned key ID допускает контролируемую будущую ротацию без ложной миграции старых thread. Централизованный `LogRecord` redactor санитизирует `msg`, `args`, URL/header credentials и traceback до передачи handlers; JSON formatter повторяет redaction для поздних `extra` fields. Telegram HTTP timing logs содержат только method, endpoint без token, status и duration. Единый response sender логирует только operation/attempt/outcome/duration/exception type; один retry разрешен только для chained `httpx.ConnectError`, а ambiguous timeout не повторяется. Checkpoint не откатывается при delivery failure; durable outbox остается вне M5.

## Открытые технические детали
Retention/pruning official checkpoint tables с validated PII, distributed coordination при будущем multi-instance deployment и auth для admin уточнять отдельными решениями. LangGraph terminal pending write при failure финального checkpoint может восстановить старый state либо весь terminal batch; per-operation partial cart не возникает, но гарантия обязательного rollback к старой корзине потребовала бы изменения transaction semantics official saver. M3 routing/persistence boundaries закреплены ADR-015–018; hybrid M5 authority/confirmation/replay — ADR-021, multi-action policy — ADR-022.

Отложенная privacy-задача: определить срок хранения и безопасное удаление PII из исторических official checkpoint versions. Очистка текущего contact state при отмене checkout не удаляет старые checkpoint versions; retention не реализуется в M5.2-A2.3.

## M5.2-A2.3 semantic continuation

- Optional typed `pending_clarification` хранится official checkpointer как bounded JSON-safe reason, allowlisted choices, return stage и 12-character session ID. Raw messages, prompts, contacts и произвольный payload туда не входят.
- Semantic callback повторно использует envelope `order:<action>:<draft_revision>:<session_id>` и проходит replay, revision и session validation. Existing numbered dish disambiguation остается отдельным catalog-resolution mechanism.
- При active clarification `ContextBuilder` выставляет `expected_input=clarification_choice` и сужает LLM allowlist до server-side choices. A1 destructive stages имеют приоритет; FAQ/topic switch инвалидирует pending без потери checkout fields.
- Backend-only `last_referenced_dish_id` устанавливается только после catalog resolution. LLM видит лишь public name; details/allergens перечитываются через свежий `get_dish(dish_id)`.
- Previous-order query читает только current/archive confirmed checkpoint snapshot и явно отличает его от отсутствующего до M6 business order/status. Mutation, Calendar и snapshot update не выполняются.

## M5.2-A2.3 deterministic checkout boundary

- При новом checkout один раз показывается предупреждение о последовательном сборе имени, телефона, адреса и времени и возможности проверить/изменить данные перед подтверждением. При редактировании предупреждение не повторяется.
- Все восемь initial/edit contact stages принимают ordinary text только как ожидаемое поле. Name/address проходят Unicode NFC и allowlist validation; phone содержит 7–15 ASCII digits с optional leading `+`; time остается deterministic.
- «Отменить оформление» доступно на каждой contact stage, сохраняет cart, очищает unconfirmed contacts/quote/session и возвращает `idle`; это не A1 destructive cancel.
- Checkout session — optional 12-character JSON-safe token в existing checkpoint. Time/cancel callbacks обязаны совпасть по action, current stage, `draft_revision` и session; old/legacy/wrong-session buttons fail safe. Старые checkpoints без token продолжают принимать field text, но session-bound callbacks не исполняют.
