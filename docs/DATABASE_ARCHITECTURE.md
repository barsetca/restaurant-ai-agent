# Архитектура базы данных

**Статус:** проектная модель для M1. Реальную миграционную схему проверять по `alembic/versions/`. PostgreSQL — runtime source of truth.

## Таблицы
| Таблица | Основные поля | Ограничения / назначение |
|---|---|---|
| `menu_items` | `id`, `source_row_id`, `name`, `description`, `ingredients`, `allergens`, `category`, `price_minor`, `currency`, `available` | Уникальный `source_row_id`, `price_minor >= 0`; доступность |
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
- Google Sheets `Menu`: id, название, описание, ингредиенты, аллергены, категория, цена, доступность. Валидировать вход, не допускать частичного применения ошибочного набора.
- Google Sheets `Settings`: ключи, значения, описания. `delivery_fee_minor` — редактируемая настройка, не константа.
- Google Sheets `FAQ`: id, category, question, answer, updated_at, active; Qdrant хранит embeddings, `content_hash` и metadata. Таблица `faq_sync` зарезервирована для будущего audit/job metadata и в M2 не используется как второй источник текущего sync state (ADR-009).
- Не удалять блюда, уже упомянутые в заказах, при удалении строки Sheets; использовать безопасную деактивацию.

## Миграции и тесты
Alembic; миграции запускаются отдельной командой; проверять clean database и повторный upgrade. Тесты — на отдельной БД, без реальных контактов. Не выполнять destructive операции на рабочей БД.

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

## Нерешенные вопросы
Представление `restaurant_settings.value`, жизненный цикл статусов заказа и Calendar, а также retention/pruning checkpoint history. Решать в соответствующем milestone и отражать в `DECISIONS.md`.
