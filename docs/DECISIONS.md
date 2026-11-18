# Architecture Decision Log

Записывать новые решения в формате: дата, ID, контекст, решение, последствия, статус. Не переписывать задним числом принятые решения; изменение оформлять новой записью.

## ADR-001 — Один backend + отдельный MCP process
**Дата:** 2026-11-17. **Статус:** принято. FastAPI + Telegram + LangGraph в одном приложении; локальный Python MCP SDK server через stdio. Нет платформы микросервисов.

## ADR-002 — Разделение Google Sheets и PostgreSQL
**Дата:** 2026-11-17. **Статус:** принято. Google Sheets с вкладками `FAQ`, `Menu`, `Settings` — интерфейс редактирования; PostgreSQL — runtime source of truth для меню/настроек/заказов; Qdrant — FAQ index. Sync из admin UI без перезапуска, с валидацией и сохранением предыдущих корректных значений при ошибке.

## ADR-003 — Деньги и доставка
**Дата:** 2026-11-17. **Статус:** принято. EUR, integer minor units, редактируемая `delivery_fee_minor` в Settings; в заказе сохраняется снимок доставки и цен. При изменении итога требуется повторное подтверждение.

## ADR-004 — Подтверждение и Calendar
**Дата:** 2026-11-17. **Статус:** принято. Никаких побочных эффектов до явного подтверждения; уникальный `confirmation_key`; `calendar_jobs` для retry и идемпотентности; при ошибке `calendar_pending`.

## ADR-005 — Локальная разработка и документы
**Дата:** 2026-11-17. **Статус:** принято. Telegram long polling; документация `docs/` в Git, отчеты/ZIP в игнорируемом `history/`; два исходных DOCX остаются отдельно в Sources ChatGPT.

## ADR-006 — Учебные ограничения
**Дата:** 2026-11-17. **Статус:** принято. 10 дней, один вымышленный ресторан, русский интерфейс, синтетические данные, без реальных платежей, GitHub Actions, Kubernetes, n8n и второго LLM-провайдера.

## ADR-007 — Представление ingredients и allergens
**Дата:** 2026-11-17. **Статус:** принято для MVP. Поля `menu_items.ingredients` и `menu_items.allergens` хранятся как PostgreSQL `TEXT[] NOT NULL`. Это сохраняет типизированный плоский список и не вводит неограниченную JSON-схему; более сложная нормализация отдельными таблицами не нужна для одного демонстрационного ресторана. Sync service позднее отвечает за очистку и нормализацию элементов.

## ADR-008 — Идентичность блюд при синхронизации Menu
**Дата:** 2026-11-17. **Статус:** принято для будущего sync milestone. Google Sheets `Menu` остается редактируемым источником, а PostgreSQL `menu_items` — runtime source of truth. Sync выполняет upsert по стабильному `source_row_id`, сохраняя существующий `menu_items.id`. Блюда, отсутствующие в очередном корректном наборе источника, физически не удаляются, а получают `available=false`. Исторические `order_items` независимо сохраняют `name_snapshot`, `unit_price_minor` и `line_total_minor`. Реализация sync в M1.1 не входит.

## ADR-009 — Идемпотентный FAQ index и состояние sync
**Дата:** 2026-11-17. **Статус:** принято для M2. `FAQ` в Google Sheets — редактируемый источник FAQ, Qdrant collection `faq` — runtime searchable index. Стабильный `source_id` преобразуется в UUID5 point ID; `content_hash` хранится в payload и позволяет не вызывать embeddings для неизмененных записей. Измененные записи upsert-ятся, отсутствующие/неактивные удаляются только после успешных embeddings и upsert. Повторный запуск восстанавливает частично обновленный индекс. Таблица PostgreSQL `faq_sync` в M2 не используется, чтобы не создавать второй независимый источник состояния; она зарезервирована для будущего audit/job metadata.

## ADR-010 — Совместимость embedding collection
**Дата:** 2026-11-17. **Статус:** принято для M2. Qdrant collection хранит служебную metadata point с именем embedding model и размерностью. Несовпадение размерности или модели приводит к явной ошибке; молчаливое смешивание несовместимых vectors запрещено. Плановая смена модели требует новой collection либо отдельной контролируемой полной переиндексации.

## ADR-011 — Grounded FAQ answer и safe fallback
**Дата:** 2026-11-17. **Статус:** принято для M2. Similarity threshold лишь отбирает кандидатов и не считается доказательством ответа. Ограниченный FAQ-контекст передается как недоверенные JSON-данные в `ChatOpenAI`; structured decision отдельно сообщает `supported` и `conflicting`. Пустой, недостаточный, противоречивый контекст либо ошибка Sheets/OpenAI/Qdrant/Telegram приводит к фиксированному fallback без технических деталей. Исходный текст сообщения и exception details не логируются.

## ADR-012 — Защита FAQ index от пустого источника
**Дата:** 2026-11-17. **Статус:** принято для M2.1. Если источник возвращает ноль active FAQ при наличии FAQ points в Qdrant, sync по умолчанию завершается `EmptyFaqSourceError` до удаления. Намеренная полная очистка требует явного CLI-флага `--allow-empty-source`; metadata point не входит в список FAQ hashes и сохраняется. Это защищает от пустой/ошибочной Sheet, не вводя distributed transaction.

## ADR-013 — Live-калибровка FAQ retrieval threshold
**Дата:** 2026-11-17. **Статус:** принято для текущего synthetic dataset. Threshold `0.72` отклонял известный вопрос (top score `0.5748`) и перефразированный вопрос (`0.3581`). Unknown question имел максимум `0.2555`. Значение `0.30` пропускает known/paraphrased context, отсекает согласованный unknown case, после чего structured answer check сохраняет fallback. Threshold необходимо переоценивать при смене FAQ dataset или embedding model; это не универсальная константа качества.

## ADR-014 — Централизованная санитизация логов
**Дата:** 2026-11-17. **Статус:** принято для M2.3. Все Python `LogRecord` проходят redaction до handlers: обрабатываются `msg`, parameterized `args`, object-valued args (включая `httpx.URL`) и предварительно форматированный `exc_info`. JSON formatter выполняет второй redaction поздних `extra` fields. Маскируются Telegram token, OpenAI key, Authorization/Bearer/API-key headers, URL passwords, private-key blocks и runtime secrets, переданные из Settings. Сторонние логгеры не отключаются. Telegram HTTP timing записывается отдельным безопасным record с method, endpoint без token, status и duration; message payload/chat data не логируются.

## ADR-015 — M3.1 deterministic routing и ephemeral graph state
**Дата:** 2026-11-17. **Статус:** принято для M3.1. Один compiled `StateGraph` становится основной точкой обработки Telegram text messages. Router не вызывает LLM: только явные action-patterns заказа ведут в `order`, пустые/некорректные сообщения — в `unknown`, остальные и неоднозначные — в `faq`. FAQ node переиспользует `FaqAssistant`; order node не имеет side effects и возвращает placeholder. State состоит только из сериализируемых примитивов: message, intent, response, safe error code и pseudonymous conversation ID. Checkpointer отсутствует; `MemorySaver`/`InMemorySaver`, custom serialization и ложная persistence запрещены. PostgreSQL checkpoint определяется отдельным будущим решением.

## ADR-016 — Official PostgreSQL saver и schema ownership
**Дата:** 2026-11-17. **Статус:** принято для M3.2. Persistence реализуется только `langgraph-checkpoint-postgres==3.2.1` через `AsyncPostgresSaver` и managed Psycopg async pool; SQLAlchemy/asyncpg не подменяется. Official tables `checkpoint_migrations`, `checkpoints`, `checkpoint_blobs`, `checkpoint_writes` принадлежат saver и не включаются в Alembic/ORM приложения. `setup()` запускается явной operator-командой под PostgreSQL advisory lock, не на backend startup. Startup и readiness fail closed при обязательном, но недоступном saver; in-memory fallback запрещён. Serializer разрешает только безопасные primitive state values и не импортирует произвольные msgpack modules.

## ADR-017 — Versioned HMAC thread identity
**Дата:** 2026-11-17. **Статус:** принято для M3.2. Telegram chat ID преобразуется в `telegram:<key_id>:<HMAC-SHA256>` с отдельным `THREAD_ID_HMAC_SECRET` длиной не менее 32 символов. Telegram token не используется как HMAC key. Secret обязателен только при `TELEGRAM_ENABLED=true`, попадает в runtime log redactor и не хранится в Git/checkpoint. Смена key/`THREAD_ID_HMAC_KEY_ID` начинает новый namespace; автоматической миграции старых диалогов нет, поскольку до M3.2 persistent checkpoints отсутствовали. Ротация после появления production state требует отдельного migration/dual-read решения.

## ADR-018 — Сериализация сообщений одного thread
**Дата:** 2026-11-17. **Статус:** принято для текущего single-instance Telegram deployment. `AgentService` использует слабый registry per-thread async locks: сообщения одного `thread_id` выполняются последовательно, разные thread не блокируют друг друга. Это предотвращает lost updates `turn_count` внутри единственного backend process. Глобальная user-processing lock не вводится. Несколько backend/polling instances не поддерживаются; перед горизонтальным масштабированием потребуется distributed per-thread coordination или другой официальный concurrency control.

## ADR-019 — Explicit dietary tags и read-only MCP menu boundary
**Дата:** 2026-11-18. **Статус:** принято для M4. `menu_items.dietary_tags` хранится как `TEXT[] NOT NULL DEFAULT '{}'` и добавляется отдельной Alembic migration `0002`; исходная M1 migration не изменяется. Теги являются только явными source данными и никогда не выводятся из отсутствия ingredients/allergens. Будущий Google Sheets Menu sync добавит это поле в M7. MCP реализован отдельным Python MCP SDK v2 stdio process; backend использует typed client adapter. Tools работают в transaction-level read-only PostgreSQL sessions, возвращают structured output, ограничивают размеры входа и вычисляют только EUR subtotal в integer minor units без delivery. M4 не подключает MCP к LangGraph, не создаёт persistent cart/orders/Calendar и не меняет M3.2 workflow.

## ADR-020 — Явный owned DEV menu seed
**Дата:** 2026-11-18. **Статус:** принято как подготовка development data к M5 без начала M5. Synthetic menu загружается только явной operator-командой `python -m scripts.seed_menu`, не через Alembic и не при backend startup. Ровно 15 записей имеют stable UUID5 и `dev-menu-*` source IDs. Atomic upsert обновляет только заранее объявленные UUID/source pairs; предварительная ownership-проверка и PostgreSQL transaction advisory lock предотвращают перезапись конфликтующих или concurrent данных. Команда не удаляет произвольные строки, не выводит DSN и не заменяет будущий Google Sheets Menu sync M7.
