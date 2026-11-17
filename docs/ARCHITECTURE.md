# Архитектура Restaurant AI Agent

**Статус:** целевая архитектура; M1, M2, M3.1 и M3.2 приняты. PostgreSQL checkpoint проверен автоматически/live, постоянный HMAC secret и ручной Telegram E2E подтверждены владельцем. Общий M3 остаётся IN PROGRESS; реальное состояние — `DEVELOPMENT_STATUS.md`.

## Контекст
Telegram (`python-telegram-bot`, long polling) → FastAPI application / LangGraph → ветви FAQ RAG, меню через MCP и оформление заказа. Один backend deployment + локальный отдельный MCP process; без микросервисной платформы.

## Компоненты
- `app/bot`: получение Telegram updates, привязка user/thread, передача сообщений в workflow.
- `app/api`: health и служебные endpoints; `app/admin`: защищенный UI и запуск синхронизации.
- `app/agent`: typed LangGraph state, deterministic routing, FAQ/order-placeholder/unknown nodes, application service и M3.2 official PostgreSQL checkpointer. Interrupt и order workflow пока отсутствуют.
- `app/rag`: read-only Google Sheets `FAQ` adapter, валидация, LangChain/OpenAI embeddings, идемпотентный Qdrant sync, retrieval и grounded answer service.
- `mcp_server`: отдельный Python MCP SDK process, stdio; read-only tools `search_menu`, `get_dish`, `calculate_cart` читают PostgreSQL.
- `app/orders`: детерминированные расчеты, валидация, сохранение подтвержденного заказа.
- `app/calendar`: создание события из сохраненного заказа, `calendar_jobs`, retry и идемпотентность.
- `app/db`: SQLAlchemy models, migrations, DB access.
- `app/sync` (будущий модуль): `FAQ`, `Menu`, `Settings` sync из Google Sheets.

## Основные потоки
1. **Telegram/LangGraph:** обычное Telegram-сообщение → HMAC-derived versioned `conversation_id`/`thread_id` → один lifespan-managed compiled `StateGraph` с `AsyncPostgresSaver` → deterministic route. Новый invocation передаёт только поля текущего запроса и очищает предыдущие `response`/`error_code`; checkpoint восстанавливает остальные значения и увеличивает `turn_count`. `faq` вызывает существующий `FaqAssistant`; `order` возвращает статический placeholder без side effects; `unknown` возвращает safe fallback.
2. **Меню:** запрос → MCP client → stdio MCP server → PostgreSQL → структурированные блюда и актуальные цены.
3. **Заказ:** корзина → server-side расчет и проверка доставки → сбор контактов → показ итога → явное подтверждение → повторная валидация → PostgreSQL order + snapshots → `calendar_jobs` → Google Calendar.
4. **Изменение FAQ в M2:** оператор запускает `python -m scripts.sync_faq` → read-only Sheets/fixture → полная валидация → сравнение `content_hash` → embeddings только измененных записей → upsert → удаление stale points. Пустой active source при непустом index блокируется; намеренная очистка требует `--allow-empty-source` и сохраняет metadata point. Рестарт backend не нужен. Admin UI и sync `Menu`/`Settings` остаются будущими этапами.

## Границы ответственности
- LLM не вычисляет цены и не совершает необратимые действия без backend validation.
- MCP инструменты меню только читают данные; запись заказа и Calendar выполняют application services.
- PostgreSQL — runtime source of truth для меню/настроек/заказов; Google Sheets — редактируемый источник; Qdrant — runtime searchable index FAQ, не БД заказов.
- Graph state содержит только `user_message`, `intent`, `response`, `error_code`, `conversation_id`, `turn_count`. Credentials, Telegram objects, clients и ORM entities в state не входят.
- M3.2 использует `langgraph-checkpoint-postgres` `AsyncPostgresSaver` поверх отдельного Psycopg async pool; SQLAlchemy/asyncpg остаётся клиентом бизнес-БД и health query.
- Saver и pool создаются один раз на FastAPI lifespan только при `TELEGRAM_ENABLED=true`, schema readiness проверяется до polling, ресурсы закрываются после остановки polling. DDL не выполняется на startup.
- `scripts.setup_checkpoints` явно и повторяемо вызывает official `setup()` под session advisory lock. Official tables не управляются Alembic приложения.
- Сообщения одного `thread_id` сериализуются per-thread `asyncio.Lock`; разные thread обрабатываются параллельно. Один Telegram polling instance остаётся deployment invariant; multi-instance distributed locking не реализован.

## Инфраструктура
FastAPI, PostgreSQL, Qdrant в Docker Compose; FAQ работает внутри существующего backend. Credentials монтируются read-only и не копируются в image. MCP stdio process запускается/управляется приложением на будущем этапе. Telegram polling только в одном экземпляре и выключен по умолчанию.

## Безопасность
Credentials только из environment/секретов; тестовые данные; FAQ validation отклоняет email/phone-like content; отсутствие исходного пользовательского текста в FAQ logs; ограниченный контекст и timeouts внешних API. Telegram chat ID преобразуется HMAC-SHA256 с отдельным `THREAD_ID_HMAC_SECRET`; versioned key ID допускает контролируемую будущую ротацию без ложной миграции старых thread. Централизованный `LogRecord` redactor санитизирует `msg`, `args`, URL/header credentials и traceback до передачи handlers; JSON formatter повторяет redaction для поздних `extra` fields. Telegram HTTP timing logs содержат только method, endpoint без token, status и duration.

## Открытые технические детали
Retention/pruning official checkpoint tables, distributed coordination при будущем multi-instance deployment и auth для admin уточнять отдельными решениями. M3.1 routing/state boundaries закреплены ADR-015; M3.2 persistence/thread identity/concurrency — ADR-016–018.
