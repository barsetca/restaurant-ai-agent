# Project Handoff — контекст для нового чата

## Что это
Учебный Restaurant AI Agent: Telegram FAQ/RAG и агент оформления заказа на 10 дней. Основное ТЗ v1.0 и исходное школьное задание находятся в ChatGPT Sources, не в репозитории. Рабочая адаптация — `docs/ORIGINAL_REQUIREMENTS.md`.

## Прежде чем продолжать
1. Прочитай `docs/CODEX_RULES.md`, `docs/DEVELOPMENT_STATUS.md`, `docs/ORIGINAL_REQUIREMENTS.md`, `docs/ARCHITECTURE.md`, `docs/DATABASE_ARCHITECTURE.md`, `docs/DECISIONS.md`.
2. Посмотри `git status`, код и тесты; не доверяй статусу без проверки.
3. Сверь последний отчет `history/NNN_*_report.md` (если история доступна локально). `history/` не в Git и не гарантируется в новом клоне.
4. Выполняй только следующий согласованный атомарный этап.

## Утвержденные решения
- Google Sheets: `FAQ`, `Menu`, `Settings`; синхронизация через admin UI без рестарта.
- PostgreSQL: актуальное меню, цены, настройки, заказы. Qdrant: индекс FAQ.
- MCP stdio, три read-only инструмента меню; LangGraph orchestration; OpenAI единственный LLM.
- EUR, integer minor units, динамическая доставка, снимки цен, повторное подтверждение при изменении суммы.
- Telegram long polling; Calendar через `calendar_jobs` и идемпотентность.
- Все тестовые данные синтетические; исходные DOCX не копировать в `docs/`.

## Текущий статус
M1 READY после M1.1. Compose реально собран и запущен: PostgreSQL healthy, Qdrant ready, FastAPI live/ready работают. Readiness дает 503 при остановленной PostgreSQL и восстанавливается до 200. Initial migration и повторный upgrade прошли; отдельная чистая `restaurant_test` проверена integration suite (2 PASS), включая 8 таблиц, PK/FK/UNIQUE/CHECK/indexes и отсутствие ORM drift. Lifespan unit test подтверждает создание engine и `dispose()` без Telegram credentials. Backend startup/shutdown проверены по live logs.

Git repository инициализирован, но commit не создавался: все проектные файлы пока untracked, локальные/секретные artifacts игнорируются. Compose требует `POSTGRES_PASSWORD`; демонстрационные `.env`/`.env.example` согласованы. Docker volumes не удалялись. Точные команды — `DEVELOPMENT_STATUS.md` и `history/002_M1_1_report.md`.

Приняты ADR-007 (`ingredients`/`allergens` как `TEXT[]`) и ADR-008 (будущий Menu sync делает upsert по стабильному `source_row_id`, сохраняет `menu_items.id`, отсутствующие блюда деактивирует, snapshots заказов сохраняет). Sync еще не реализован. `conversation_state` содержит только metadata и не является самодельным LangGraph checkpointer.

## Для нового чата ChatGPT
Приложить обновленные `docs/` и последний безопасный ZIP-снимок из `history/`, а также сообщить текущий milestone. Исходные DOCX уже должны быть доступны через Sources проекта; при необходимости добавить их отдельно. Нельзя предполагать, что Git-игнорируемая `history/` доступна автоматически.

## Для нового чата Codex
Стартовое задание: «Прочитай все `docs/*.md`, проверь Git status, Compose status, код и тесты. M1 принят как READY. Выполняй только отдельно заданный следующий milestone; соблюдай `CODEX_RULES.md` и не расширяй scope».
