# Development Status — Restaurant AI Agent

**Дата:** 2026-11-17. **Текущий milestone:** M1.1 — Infrastructure Verification & Fixes. **Состояние:** M1 READY; Compose, live PostgreSQL/Qdrant/FastAPI, migrations, constraints, lifecycle и tests проверены.

## Этапы
| Этап | Цель | Статус | Подтверждение |
|---|---|---|---|
| M1 / дни 1–2 | Каркас, FastAPI, PostgreSQL, Alembic, Docker, Telegram bootstrap | DONE / READY | Compose smoke PASS; 10 unit + 2 integration PASS; live migration/readiness PASS |
| M2 / день 3 | Sheets FAQ → OpenAI embeddings → Qdrant, fallback | NOT STARTED | — |
| M3 / день 4 | LangGraph state, routing, checkpoint | NOT STARTED | — |
| M4 / день 5 | MCP server/client, меню, расчет | NOT STARTED | — |
| M5 / день 6 | Корзина, сбор данных, подтверждение | NOT STARTED | — |
| M6 / день 7 | Сохранение заказа, Calendar, идемпотентность | NOT STARTED | — |
| M7 / день 8 | Admin, Sheets Menu/Settings sync, логи, оператор | NOT STARTED | — |
| M8 / день 9 | Тесты, 15+ evaluation, smoke | NOT STARTED | — |
| M9 / день 10 | README, GitHub, скриншоты, скринкаст | NOT STARTED | — |

> Этапы M1–M9 — рабочая группировка. Контрольная точка: к концу дня 7 работает end-to-end заказ. Сроки и разбиение могут корректироваться по фактическому прогрессу, но не скрытно.

## Последнее выполненное задание
M1.1 завершил инфраструктурную верификацию. Инициализирован Git repository без commit. Удален небезопасный Compose fallback пароля; `.env.example` содержит явно демонстрационный согласованный пароль. Исправлен container CMD: backend запускает `/app/.venv/bin/uvicorn` без runtime-установки dev dependencies. Добавлен unit-тест lifespan и усилены migration integration tests. Принят ADR-008 о стабильном `source_row_id` и soft-disable будущего Menu sync; sync не реализован.

## Проверки
| Проверка | Команда | Результат |
|---|---|---|
| Git init/status/ignore | `git init`; `git status --short --branch`; `git check-ignore -v ...` | PASS: repository создан, секретные/local artifacts ignored, commit отсутствует |
| Compose config | `docker compose config` | PASS |
| Required password negative check | `env -u POSTGRES_PASSWORD docker compose --env-file /dev/null config` | PASS: ожидаемый отказ без пароля |
| Build/start | `docker compose up --build -d` | PASS |
| Service status | `docker compose ps`; PostgreSQL inspect; Qdrant `/readyz` | PASS: PostgreSQL healthy, Qdrant ready, backend up |
| Live health | `curl .../health/live`; `curl .../health/ready` | PASS: 200/200 при доступной DB; readiness 503 при остановленной DB и снова 200 после запуска |
| Startup/shutdown | `docker compose logs --no-color backend`; stop/start backend | PASS: startup/shutdown complete, без ошибок |
| Working DB migration | `docker compose exec -T backend /app/.venv/bin/alembic upgrade head` дважды | PASS |
| Clean test DB migration/schema | one-off test container, `TEST_DATABASE_URL=.../restaurant_test`, `pytest -m integration` | PASS: 2 passed; 8 tables, PK/FK/UNIQUE/CHECK/indexes и ORM match |
| Unit suite | `.venv/bin/pytest tests/unit -q` | PASS: 10 passed |
| Full local suite без URL | `.venv/bin/pytest -q` | PASS: 10 passed, 2 skipped (integration требует URL) |
| Python compile | `.venv/bin/python -m compileall -q app mcp_server alembic tests` | PASS |

## Текущие блокеры и риски
Реальные Google/Telegram/OpenAI credentials не нужны для M1. Локальный `.env` создан из демонстрационного template и игнорируется. Docker volumes сохранены; отдельная `restaurant_test` создана без destructive операций над development DB. Репозиторий новый и не имеет первого commit, поэтому `git diff` пуст для untracked файлов; фактический список виден в `git status`. Перед публичным использованием заменить демонстрационный пароль.

## Следующий шаг
M1 READY. Остановиться и не начинать M2 автоматически. Следующий milestone выполнять только по отдельному заданию; перед ним зафиксировать согласованный initial Git commit вручную, если владелец проекта этого хочет.
