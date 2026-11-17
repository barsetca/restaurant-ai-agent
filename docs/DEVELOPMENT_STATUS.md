# Development Status — Restaurant AI Agent

**Дата:** 2026-11-18. **Текущий milestone:** M3.2 — PostgreSQL Checkpoint. **Состояние:** M3.1 READY, M3.2 READY; implementation и automated/live проверки PASS, постоянный local HMAC secret настроен владельцем, manual Telegram E2E PASS по подтверждению владельца. Общий M3 остаётся IN PROGRESS.

## Этапы

| Этап | Цель | Статус | Подтверждение |
|---|---|---|---|
| M1 / дни 1–2 | Каркас, FastAPI, PostgreSQL, Alembic, Docker, Telegram bootstrap | DONE / READY | Compose, health, migrations/schema PASS |
| M2 / день 3 | Sheets FAQ → OpenAI embeddings → Qdrant, grounded answer/fallback | DONE / READY | Live Sheets/OpenAI/Qdrant, Telegram E2E и secure logging PASS |
| M3 / день 4 | LangGraph state, routing, checkpoint | IN PROGRESS / M3.1 READY / M3.2 READY | Official PostgreSQL persistence/restart PASS; manual Telegram E2E M3.2 owner-confirmed PASS |
| M4 / день 5 | MCP server/client, меню, расчет | NOT STARTED | — |
| M5 / день 6 | Корзина, сбор данных, подтверждение | NOT STARTED | — |
| M6 / день 7 | Сохранение заказа, Calendar, идемпотентность | NOT STARTED | — |
| M7 / день 8 | Admin, Sheets Menu/Settings sync, логи, оператор | NOT STARTED | — |
| M8 / день 9 | Тесты, 15+ evaluation, smoke | NOT STARTED | — |
| M9 / день 10 | README, GitHub, скриншоты, скринкаст | NOT STARTED | — |

## Изменения M2.1

- Пустой active source при непустом FAQ index блокируется `EmptyFaqSourceError`.
- Намеренная полная очистка требует `--allow-empty-source`; metadata point сохраняется.
- Fault-injection tests покрывают failure embeddings, upsert/partial upsert, stale delete и recovery повторным sync.
- `secrets/` исключен из Git и Docker build context; credential JSON монтируется read-only.
- Retrieval threshold откалиброван live с `0.72` на `0.30` и зафиксирован ADR-013.
- Google Sheets изменял только пользователь: update `faq-001` и deactivate `faq-008`. Codex выполнял read-only Sheets access.

## Live Google Sheets / OpenAI / Qdrant

| Проверка | Результат |
|---|---|
| Initial/repeated sync текущего набора | PASS: 10 unchanged, без duplicates |
| Manual update `faq-001` → sync | PASS: updated=1, unchanged=9, added=0, removed=0; один embeddings batch |
| `faq-001` Qdrant payload/count | PASS: один point, answer `12:30–23:00`, total FAQ=10 |
| Real answer «Во сколько открывается ресторан?» | PASS: top score 0.5762; «Ресторан открывается ежедневно в 12:30.» |
| Manual deactivate `faq-008` → sync | PASS: removed=1, inactive=1, unchanged=9, added=0, updated=0; embeddings не вызывался |
| Qdrant после deactivate | PASS: active FAQ=9, `faq-008`=0, metadata=1, total points=10 |
| Retrieval «Можно ли забронировать столик?» | PASS: `faq-008` отсутствует; unrelated candidates отвергнуты structured check, возвращен fallback |
| Остальные FAQ | PASS: opening → 12:30; delivery → корректный FAQ answer |
| Prompt injection с real OpenAI | PASS: supported=false, system prompt/credentials не раскрыты |
| Conflicting context | PASS: direct conflict detected, arbitrary answer не выбирается |
| Empty source на live collection без opt-in | PASS: blocked=true, count 10 → 10 на момент проверки |

## Threshold calibration

При `0.72` known/paraphrased/unknown возвращали 0 candidates. Scores без отсечения: known `0.5748`, paraphrased `0.3581`, unknown максимум `0.2555`. При `0.30` known/paraphrased получили корректные grounded answers, unknown — fallback. После update known top score `0.5762`. Threshold требует повторной оценки при смене данных/model.

## Telegram investigation (исторический результат M2.1)

| Проверка | Host | Docker |
|---|---|---|
| Proxy env присутствует | нет | нет |
| Token format | valid shape | valid shape |
| DNS `api.telegram.org` | PASS: IPv4 + IPv6 | PASS: IPv4 + IPv6 |
| TCP/TLS | PASS: TLS 1.3 | PASS: TLS 1.3 |
| Root endpoint | PASS: HTTP 302 | PASS: HTTP 302 |
| Synthetic invalid token `getMe` | PASS: HTTP 401 | PASS: HTTP 401 |
| Configured real token `getMe` GET/POST | FAIL: ReadTimeout | FAIL: ReadTimeout/ConnectError |
| `python-telegram-bot` startup with Telegram enabled | FAIL: `telegram.error.TimedOut`, restart loop |
| `/start` и text handler в реальном chat | NOT RUN: test chat ID не предоставлен и polling не стартовал |

На момент M2.1 DNS/TCP/TLS/proxy/Docker network были исправны, а failure выглядел специфичным для real token/Telegram-side path. Этот timeout перестал воспроизводиться при M2.2 без изменения token или сетевой конфигурации.

## Telegram M2.2 diagnostics

- Token присутствует в ignored `.env`, имеет валидный формат и без изменений передается в running backend container, Pydantic Settings и `python-telegram-bot`.
- Текущая конфигурация host/container: `TELEGRAM_ENABLED=false`; поэтому lifespan не запускает polling и `/start` handler не работает.
- Повторный `getMe` 2026-11-17: host **PASS HTTP 200**; из backend container через project venv **PASS**, bot `@restaurant_faq_agent_bot`, id `9262411763`.
- Контрольный synthetic invalid token: host **PASS HTTP 401**; `python-telegram-bot` в container получил ожидаемый `InvalidToken`.
- `getWebhookInfo`: webhook отсутствует, `pending_update_count=3`.
- `getUpdates` без `offset`: **PASS**, возвращены 3 pending update, среди них 2 команды `/start`; chat ID, тексты и данные пользователя не выводились. Updates не подтверждались и не удалялись.
- Прежний token-specific timeout сейчас не воспроизводится и, вероятно, был временным сбоем Telegram/API route. Сетевая конфигурация и token не изменялись.
- Immediate cause отсутствия ответа: polling намеренно отключен. Для завершения E2E владелец должен подтвердить, что чат открыт с `@restaurant_faq_agent_bot`, затем включить `TELEGRAM_ENABLED=true` и пересоздать только backend.

## Secure Logging M2.3

- Владелец подтвердил Telegram E2E: `/start` и FAQ-вопросы получают ответы.
- `SecretRedactor` централизованно обрабатывает `LogRecord.msg`, parameterized/mapping/object `args`, runtime secrets и санитизированный traceback; exception type и traceback сохраняются.
- Второй слой в `JsonFormatter` покрывает поздние `extra` fields и случай замены record factory сторонней библиотекой.
- Покрыты Telegram Bot token в URL, OpenAI keys, Authorization/Bearer/API-key headers, database URL password и private-key blocks.
- `httpx`, `httpcore`, `telegram` не отключены. Реальные штатные `httpx` records остаются доступны с redacted URL.
- Два `HTTPXRequest` Telegram используют async hooks только для безопасной диагностики: method, endpoint без token, status, `duration_ms`; payload и chat data не логируются.
- Первый live audit выявил, что `httpx.URL` приходит как object-valued arg; backend был остановлен, redactor исправлен и container пересоздан. Старый container log больше не доступен через текущий Compose container; Docker volumes не удалялись.
- Финальный live audit: polling/startup PASS; `getMe`, `deleteWebhook`, `getUpdates` status 200; HTTPX records=4, timing records=3, exact/generic secret hits=0, health live/ready=200/200.
- Контролируемый runtime exception: exact secret hits=0, `[REDACTED]` присутствует, `RuntimeError` и traceback сохранены.

## Регрессионные проверки

| Проверка | Команда | Результат |
|---|---|---|
| Unit | `.venv/bin/pytest tests/unit -q` | PASS: 44 passed до final timing test; final full suite содержит 45 tests |
| Full без external URLs | `.venv/bin/pytest -q` | PASS: 45 passed, 5 skipped |
| Qdrant integration | `TEST_QDRANT_URL=http://localhost:6333 ... test_faq_qdrant.py -q` | PASS: 3 passed |
| Clean/repeated Alembic/schema | dedicated `restaurant_test_m2_1_20261117` | PASS: 2 passed |
| Compile/lock/diff | `compileall`; `uv lock --check`; `git diff --check` | PASS |
| Compose/build/services | `docker compose config --quiet`; `up --build -d`; `ps` | PASS |
| Health | live/ready/Qdrant healthz | PASS: HTTP 200/200/200 |
| CLI | `python -m scripts.sync_faq --help` | PASS: guard flag documented |
| Credentials mount | Docker inspect/container check | PASS: bind, read-only |
| Logs/image history secret scan | regex scan | PASS |

## M3.1 LangGraph Foundation

- Добавлен `langgraph==1.2.22`, совместимый с `langchain-core==1.8.1`; lock содержит 87 packages.
- `AgentState`: `user_message`, `intent`, `response`, `error_code`, `conversation_id`; только сериализируемые значения.
- Graph: `START → route → faq | order_placeholder | unknown → END`; все control nodes async, graph компилируется один раз при создании `AgentService`.
- Deterministic router отправляет только явные order actions в placeholder. «Как оформить заказ?» и неоднозначные сообщения остаются FAQ.
- `FaqAssistant` не перенесён и не продублирован: FAQ node вызывает существующий service; его fallback переиспользуется.
- Telegram использует один text handler через `AgentService`; `/start` не изменён.
- `conversation_id` стабильно вычисляется как SHA-256-derived value от chat ID; raw chat ID не попадает в state logs. Persistence/checkpointer отсутствуют.
- Режим Telegram enabled + FAQ disabled создаёт graph без внешних FAQ clients и возвращает safe fallback.
- Order placeholder не создаёт cart/order, не вызывает MCP/PostgreSQL/Calendar.

## M3.1 проверки

| Проверка | Команда | Результат |
|---|---|---|
| Targeted M3.1 | `pytest test_app.py test_agent_router.py test_agent_graph.py test_faq_telegram.py -q` | PASS: 28 passed |
| Full offline | `.venv/bin/pytest -q` | PASS: 68 passed, 5 skipped external integration tests |
| Compile/lock/diff | `compileall`; `uv lock --check`; `git diff --check` | PASS |
| Compose/build | `docker compose config --quiet`; `docker compose up --build -d backend` | PASS; frozen install includes LangGraph 1.2.22 |
| Services/health | `docker compose ps`; live/ready/Qdrant | PASS: backend up, PostgreSQL healthy, HTTP 200/200/200 |
| Secure logs | exact/generic secret scan current backend logs | PASS: 0 secret hits; polling started; no traceback |
| Live AgentService FAQ | real OpenAI/Qdrant: «Во сколько открывается ресторан?» | PASS: response contains 12:30 |
| Live AgentService order | «Хочу заказать пиццу» | PASS: exact order placeholder |
| Live AgentService unknown | unsupported synthetic question | PASS: existing fallback |
| Telegram `/start`/FAQ/order/unknown after M3.1 | manual chat | PASS: владелец подтвердил успешный E2E для всех четырёх сценариев |

Промежуточные FAIL: два неверных запуска standalone `uv` image и cache permission errors не меняли lockfile; затем lock успешно создан через project backend image. Первый targeted graph run завис из-за sync conditional selector/executor thread; selector переведён в async, повторные tests PASS.

## Ограничения и следующий шаг

- **M3.1 READY**: владелец подтвердил ручной Telegram E2E для `/start`, FAQ, order placeholder и unknown fallback.

## M3.2 PostgreSQL Checkpoint

- Добавлены `langgraph-checkpoint-postgres==3.2.1`, `psycopg[binary]==3.3.9`; frozen lock содержит 92 packages.
- `AsyncPostgresSaver` использует lifespan-managed `AsyncConnectionPool` (`autocommit`, `prepare_threshold=0`, `dict_row`) и strict serializer без arbitrary module imports.
- Official schema создаётся только явной командой `python -m scripts.setup_checkpoints`; повторный setup защищён advisory lock и не выполняется на startup.
- Startup/readiness fail closed, если Telegram включён, но checkpoint schema/connection недоступны. При `TELEGRAM_ENABLED=false` saver не создаётся.
- Graph компилируется с saver; `conversation_id` передаётся как `configurable.thread_id`. Partial input очищает текущие `response`/`error_code`, не стирая восстановленный `turn_count`.
- Telegram identity заменена с unsalted SHA-256 на versioned HMAC-SHA256 с отдельным обязательным `THREAD_ID_HMAC_SECRET`; raw chat ID/secret не логируются.
- Один `AgentService` сериализует сообщения одного thread per-thread lock; разные threads выполняются параллельно.
- `conversation_state` и business schema не изменялись; official checkpoint tables управляются отдельно от Alembic.

## M3.2 проверки

| Проверка | Команда | Результат |
|---|---|---|
| Targeted unit | container pytest: agent graph/persistence/checkpoint/Telegram/lifespan/health/config | PASS: initial 40 passed/1 test-only FAIL; focused retry 7 passed; full suite confirms regression PASS |
| Full offline | container `.venv/bin/pytest -q` | PASS: 82 passed, 6 skipped external integration tests |
| All integrations | isolated checkpoint/migration DBs + Qdrant collections | PASS: 6 passed |
| Official saver integration | `test_agent_checkpoint_postgres.py` | PASS: setup twice, isolation, service/pool recreation, concurrent same-thread count |
| Working DB schema setup | `python -m scripts.setup_checkpoints` дважды | PASS: 4 official tables present, existing business tables untouched |
| Docker build/start/restart | `docker compose build`; `up -d --force-recreate backend`; `restart backend` | PASS |
| Live health | `/health/live`, `/health/ready`, Qdrant | PASS: HTTP 200 |
| Live FAQ/order/unknown | real OpenAI/Qdrant + official saver | PASS: FAQ contains 12:30; placeholders/fallback exact |
| Restart recovery | synthetic thread before/after backend restart | PASS: `turn_count` 2 → 3; isolated second thread remained 1 |
| Synthetic checkpoint cleanup | query `thread_id LIKE 'm32-live-%'` | PASS: 0 rows after targeted cleanup |
| Secure logs | exact token/HMAC + generic pattern scan | PASS: 0/0 hits, traceback=0, graceful polling shutdown present |
| Manual Telegram E2E after HMAC/checkpoint change | owner chat | PASS: владелец настроил постоянный HMAC secret, запустил backend и подтвердил `/start`, FAQ, order placeholder, unknown fallback без изменения поведения M3.1; Codex проверку не повторял |

Промежуточные FAIL/ограничения: host `uv` отсутствовал, первый Docker `uv add --exact` не поддерживался текущим uv; зависимости затем зафиксированы явными версиями. Первый targeted suite: 40 PASS/1 FAIL из-за ожидания `exception_type` в `caplog.text`, хотя structured record и JSON log содержали его; assertion исправлен на `LogRecord.extra`, повтор PASS. Первый health curl был выполнен до завершения startup и получил connection reset; после startup live/ready PASS.

## PERF-001 — Slow first FAQ response

**Статус:** OPEN, наблюдение владельца; причина не установлена.

- Первый FAQ-запрос занимает ориентировочно 5–10 секунд, последующие запросы обрабатываются быстрее.
- Наблюдение повторялось на M3.1 и M3.2.
- Возможные cold start и установление соединений с внешними API не подтверждены измерениями и не считаются установленной причиной.
- На последующем техническом этапе рекомендуется добавить безопасные duration metrics отдельно для LangGraph, OpenAI embeddings, Qdrant retrieval, OpenAI answer generation, PostgreSQL checkpoint и Telegram send. Пользовательский текст, identifiers и credentials в metrics/logs не включать.
- Метрики, caching и performance optimization в acceptance task не реализовывались.

## Ограничения и следующий шаг

- **M3.2 READY**: automated/live persistence checks PASS; постоянный `THREAD_ID_HMAC_SECRET`, запуск backend и ручной Telegram E2E подтверждены владельцем.
- Codex не повторял ручные Telegram-сценарии в acceptance task.
- Multi-instance distributed locking и checkpoint retention/pruning остаются отдельными будущими решениями.
- PERF-001 остаётся OPEN до инструментального измерения; причина slow first FAQ response неизвестна.
- Общий M3 остаётся IN PROGRESS. Следующий этап — M4 MCP Tools, NOT STARTED; не начинать автоматически. Commit/push не выполнялись.
