# Архитектура Restaurant AI Agent

**Статус:** целевая архитектура; M1-каркас FastAPI, health, DB lifecycle, миграция, Compose и Telegram bootstrap реализован. Реальное состояние проверок — `DEVELOPMENT_STATUS.md`.

## Контекст
Telegram (`python-telegram-bot`, long polling) → FastAPI application / LangGraph → ветви FAQ RAG, меню через MCP и оформление заказа. Один backend deployment + локальный отдельный MCP process; без микросервисной платформы.

## Компоненты
- `app/bot`: получение Telegram updates, привязка user/thread, передача сообщений в workflow.
- `app/api`: health и служебные endpoints; `app/admin`: защищенный UI и запуск синхронизации.
- `app/agent`: LangGraph state, routing, interrupt/checkpoint, подтверждение, error/fallback.
- `app/rag`: Google Sheets `FAQ` → embeddings → Qdrant; retrieval и ответы с опорой на контекст.
- `mcp_server`: отдельный Python MCP SDK process, stdio; read-only tools `search_menu`, `get_dish`, `calculate_cart` читают PostgreSQL.
- `app/orders`: детерминированные расчеты, валидация, сохранение подтвержденного заказа.
- `app/calendar`: создание события из сохраненного заказа, `calendar_jobs`, retry и идемпотентность.
- `app/db`: SQLAlchemy models, migrations, DB access.
- `app/sync` (будущий модуль): `FAQ`, `Menu`, `Settings` sync из Google Sheets.

## Основные потоки
1. **FAQ:** сообщение → LangGraph → Qdrant retrieval → ответ по контексту либо оператор; обезличенный журнал.
2. **Меню:** запрос → MCP client → stdio MCP server → PostgreSQL → структурированные блюда и актуальные цены.
3. **Заказ:** корзина → server-side расчет и проверка доставки → сбор контактов → показ итога → явное подтверждение → повторная валидация → PostgreSQL order + snapshots → `calendar_jobs` → Google Calendar.
4. **Изменение данных:** администратор редактирует Google Sheets → запускает sync в admin UI → валидация → атомарное обновление PostgreSQL для `Menu`/`Settings`, переиндексация Qdrant для `FAQ`. Ошибка не должна разрушать последний корректный набор данных.

## Границы ответственности
- LLM не вычисляет цены и не совершает необратимые действия без backend validation.
- MCP инструменты меню только читают данные; запись заказа и Calendar выполняют application services.
- PostgreSQL — runtime source of truth; Google Sheets — редактируемый источник; Qdrant — индекс FAQ, не БД заказов.
- Состояние LangGraph должно переживать перезапуск; формат checkpointer выбрать на M4, не создавать самодельную несовместимую сериализацию на M1.

## Инфраструктура
FastAPI, PostgreSQL, Qdrant в Docker Compose; MCP stdio процесс запускается/управляется приложением на M5. Telegram polling только в одном экземпляре. На M1 — лишь skeleton, health, migrations, bootstrap.

## Безопасность
Credentials только из environment/секретов; тестовые контакты; отсутствие PII в FAQ логах/Qdrant; ограничение admin; проверка user/thread binding; timeouts внешних API.

## Открытые технические детали
Реализацию LangGraph checkpointer, auth для admin и стратегию безопасного upsert при sync уточнять на соответствующем этапе, не считать согласованными без решения. Формат `ingredients/allergens` принят в ADR-007.
