# Архитектура базы данных

**Статус:** проектная модель для M1. Реальную миграционную схему проверять по `alembic/versions/`. PostgreSQL — runtime source of truth.

## Таблицы
| Таблица | Основные поля | Ограничения / назначение |
|---|---|---|
| `menu_items` | `id`, `source_row_id`, `name`, `description`, `ingredients`, `allergens`, `dietary_tags`, `category`, `price_minor`, `currency`, `available` | Уникальный `source_row_id`, `price_minor >= 0`; явные dietary tags и доступность |
| `restaurant_settings` | `key`, `value`, `updated_at` | `key` PK; проверка типов на уровне sync service |
| `orders` | `id`, `telegram_user_id`, `customer_name`, `phone`, `address`, `requested_at`, `subtotal_minor`, `delivery_fee_minor`, `total_minor`, `currency`, `status`, `confirmation_key`, `calendar_event_id` | Уникальный `confirmation_key`; снимок суммы |
| `order_items` | `id`, `order_id`, `menu_item_id`, `quantity`, `unit_price_minor`, `line_total_minor`, `name_snapshot` | FK на заказ/блюдо, `quantity > 0`; снимок цены и названия |
| `conversation_state` | `thread_id`, `user_id`, `updated_at` | Только application metadata M1; не используется как LangGraph checkpoint |
| `faq_sync` | `source_row_id`, `content_hash`, `indexed_at` | Контроль версии FAQ в Qdrant |
| `unanswered_questions` | `id`, `sanitized_question`, `created_at`, `escalation_status` | Только обезличенный текст |
| `calendar_jobs` | `id`, `order_id`, `attempt_count`, `status`, `next_attempt_at`, `last_error` | FK на заказ; повторные попытки и аудит |

## Связи
`orders 1:N order_items`; `menu_items 1:N order_items`; `orders 1:N calendar_jobs` (или один активный job с уникальным ограничением — уточнить на M7). Внешние ключи и индексы обязательны.

## Деньги и подтверждение
EUR; integer minor units. Сумма позиций, доставка и общий итог вычисляются сервером. При изменении цен/доставки до подтверждения нужен новый показ итоговой суммы и новое подтверждение. После подтверждения snapshots не переписываются. `confirmation_key` обеспечивает идемпотентность заказа; для Calendar нужна отдельная защита от дублей.

## Источники и синхронизация
- Google Sheets `Menu`: id, название, описание, ингредиенты, аллергены, dietary tags, категория, цена, доступность. Реализация sync dietary tags остается M7; валидировать вход, не допускать частичного применения ошибочного набора.
- Google Sheets `Settings`: ключи, значения, описания. `delivery_fee_minor` — редактируемая настройка, не константа.
- Google Sheets `FAQ`: id, category, question, answer, updated_at, active; Qdrant хранит embeddings, `content_hash` и metadata. Таблица `faq_sync` зарезервирована для будущего audit/job metadata и в M2 не используется как второй источник текущего sync state (ADR-009).
- Не удалять блюда, уже упомянутые в заказах, при удалении строки Sheets; использовать безопасную деактивацию.

## Миграции и тесты
Alembic; миграции запускаются отдельной командой; проверять clean database и повторный upgrade. Тесты — на отдельной БД, без реальных контактов. Не выполнять destructive операции на рабочей БД.

## Development menu seed

`python -m scripts.seed_menu` — явная DEV-only operator command, не migration и не startup hook. Набор владеет только 15 парами stable UUID5 / `source_row_id` с namespace `dev-menu-*`. Перед upsert команда под transaction advisory lock проверяет отсутствие конфликтующих UUID/source pairs; конфликт откатывает всю transaction. Повторный запуск обновляет только эти пары до declarative seed state, не создаёт дубликаты, не удаляет другие menu rows и не выводит DSN/credentials. Working development DB `restaurant` содержит этот набор; production data mechanism и Google Sheets Menu sync остаются M7.

## LangGraph checkpoint schema — M3.2

Official `langgraph-checkpoint-postgres==3.2.1` управляет четырьмя служебными таблицами в той же PostgreSQL database:

- `checkpoint_migrations` — версии official saver migrations;
- `checkpoints` — checkpoint metadata/state по составному PK (`thread_id`, `checkpoint_ns`, `checkpoint_id`);
- `checkpoint_blobs` — сериализованные channel values;
- `checkpoint_writes` — промежуточные writes/tasks.

Эти таблицы не дублируют и не заменяют `conversation_state`. Их DDL принадлежит версии official saver и намеренно не отражается в SQLAlchemy ORM/Alembic metadata приложения. Одноразовая/повторная инициализация выполняется явно командой `python -m scripts.setup_checkpoints`, которая вызывает official async `setup()` под PostgreSQL advisory lock. Backend startup DDL не выполняет: при `TELEGRAM_ENABLED=true` отсутствие таблиц или недоступность saver приводит к startup failure/readiness 503, а не к in-memory fallback.

Psycopg соединения saver используют `autocommit=True`, `prepare_threshold=0`, `row_factory=dict_row`; бизнес engine продолжает использовать SQLAlchemy/asyncpg. Integration tests требуют отдельный `TEST_CHECKPOINT_DATABASE_URL` с `test` в имени БД и удаляют только созданные ими synthetic thread rows.

## Реализация M1
Начальная миграция `alembic/versions/0001_initial_schema.py` создает все восемь таблиц, PK/FK, уникальный `confirmation_key`, индексы и базовые CHECK-ограничения. `orders.delivery_fee_minor` имеет `NOT NULL` и CHECK `>= 0`. Миграция выполняется только отдельной командой, не при startup приложения.

`ingredients` и `allergens` реализованы как PostgreSQL `TEXT[] NOT NULL`. Для M1 это нормализует список строк без неограниченной JSON-структуры, сохраняет порядок исходных значений и допускает штатные PostgreSQL array-операторы/индексы позднее. Отдельные справочники и связи many-to-many для текущего одноресторанного MVP избыточны; формат элементов и нормализация входа остаются ответственностью будущего sync service.

## Реализация M4

Миграция `0002_add_menu_item_dietary_tags.py` отдельно от M1 добавляет `menu_items.dietary_tags TEXT[] NOT NULL DEFAULT '{}'`. Существующие блюда получают пустой массив, который означает только отсутствие подтвержденных тегов, а не доказательство dietary suitability. Теги не выводятся автоматически из ingredients/allergens; будущий M7 sync должен принимать явные нормализованные значения источника.

MCP repository использует существующий SQLAlchemy model и для каждого tool call открывает транзакцию с `SET TRANSACTION READ ONLY`. `calculate_cart` читает актуальные rows одним запросом и вычисляет line totals/subtotal в Python integer minor units; таблицы cart/order не изменяются.

## Реализация M5

- Persistent cart table не добавляется. Official checkpoint хранит JSON-safe bounded state: stage, current callback action/revision/optional destructive confirmation ID, canonical `{dish_id, quantity}`, candidates/full pending batch, optional pending destructive action/return stage/session ID, validated contacts, quote/revision, immutable current/archive confirmed snapshots и до 32 processed Telegram updates. Destructive callback data содержит bounded action name, integer draft revision и short opaque session ID без PII; остальные callbacks сохраняют action/revision.
- M5.2-A2.1 добавляет только optional checkpoint values без DB migration: до 10 FIFO typed semantic events, `last_action` и trusted public `last_referenced_dish_name` до 80 символов. Raw user/assistant messages, contacts, IDs, callback/session payload, quote/confirmed bodies и replay responses не копируются в semantic history или LLM context. Старые checkpoints без этих keys читаются с empty defaults.
- M5.2-A2.2 не меняет PostgreSQL/Alembic schema: составной cart plan и вложенные resolved dish IDs сохраняются в существующем JSON-safe `action_proposal`/`pending_batch`. После полной resolution/validation весь итоговый cart возвращается одним terminal graph update; отдельных writes на каждую операцию нет. Official saver может восстановить terminal pending write целиком при сбое финального checkpoint, поэтому наблюдаемое cart state остается old-or-complete, но не частичным.
- Validated name/phone/address находятся в checkpoint и поэтому являются PII. Unconfirmed draft очищается при cancel; до пяти confirmed snapshots суммарно сохраняются FIFO для M6. Production retention/pruning и удаление archive пока не определены.
- `OrderSettingsRepository` читает `restaurant_settings['delivery_fee_minor']` внутри `SET TRANSACTION READ ONLY`; допускается только canonical non-negative integer string. Missing/invalid/negative value блокирует summary/confirmation.
- В основной development-БД key установлен явной operator-операцией в `350` minor units. Это runtime data, не Alembic migration и не application default.
- M5 не выполняет INSERT/UPDATE в `orders`, `order_items`, `calendar_jobs` или menu rows. Quote и confirmation остаются checkpoint draft; durable order появляется только в M6 после повторной проверки.

## Нерешенные вопросы
Жизненный цикл статусов заказа и Calendar, схема M6 persistence/idempotency, а также retention/pruning checkpoint history с PII. Решать в соответствующем milestone и отражать в `DECISIONS.md`.
