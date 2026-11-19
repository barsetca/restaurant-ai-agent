# Development Status — Restaurant AI Agent

**Дата:** 2026-11-19. **Текущий milestone:** M5 — Cart & Confirmation. **Состояние:** IN PROGRESS / M5.2-A1 IMPLEMENTED / M5.2-A2.1 IMPLEMENTED / M5.2-A2.2 LIVE ACCEPTED / REMAINING A2 SCOPE OPEN. M1–M4 остаются READY. M5 не объявлять READY до завершения согласованного остатка M5.2. M6 не начинать.

## Этапы

| Этап | Цель | Статус | Подтверждение |
|---|---|---|---|
| M1 / дни 1–2 | Каркас, FastAPI, PostgreSQL, Alembic, Docker, Telegram bootstrap | DONE / READY | Compose, health, migrations/schema PASS |
| M2 / день 3 | Sheets FAQ → OpenAI embeddings → Qdrant, grounded answer/fallback | DONE / READY | Live Sheets/OpenAI/Qdrant, Telegram E2E и secure logging PASS |
| M3 / день 4 | LangGraph state, routing, checkpoint | IN PROGRESS / M3.1 READY / M3.2 READY | Official PostgreSQL persistence/restart PASS; manual Telegram E2E M3.2 owner-confirmed PASS |
| M4 / день 5 | MCP server/client, меню, расчет | DONE / READY | Unit/protocol/PostgreSQL/real stdio/Docker PASS |
| M5 / день 6 | Корзина, сбор данных, подтверждение | IN PROGRESS / M5.2-A2.2 LIVE ACCEPTED | Owner-confirmed Telegram Tests 10–14; remaining A2 semantics and A3–A5 open |
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

## M4 MCP Tools — analysis checkpoint

**Статус:** ANALYSIS COMPLETE / IMPLEMENTATION NOT STARTED. После M3.2 владелец выполнил `git add .`, commit и push; на момент анализа `main` чиста и синхронизирована с `origin/main` на commit `b087c31`.

- Python MCP SDK пока отсутствует в `pyproject.toml`/`uv.lock`; `mcp_server` содержит только placeholder.
- Существующая модель `MenuItem` уже задаёт UUID `id`, `ingredients`, `allergens`, `category`, integer `price_minor`, `currency` и `available`; для базовых read-only menu tools новая схема не нужна.
- Требуемый фильтр `dietary_tags` не имеет согласованного источника или DB-поля. Нельзя безопасно выводить диетические свойства из отсутствия аллергенов. До реализации требуется решение владельца: добавить явное `dietary_tags TEXT[]` и будущую колонку Sheets Menu либо сузить контракт M4.
- Рекомендуемая реализация: официальный Python MCP SDK v2, отдельный stdio server, backend client adapter с ограниченным environment, typed structured outputs, transaction-level read-only PostgreSQL access, bounded inputs/cart и безопасные tool errors.
- Рабочие данные ресторана не исследовались и не изменялись. Integration acceptance должен использовать отдельную PostgreSQL database с `test` в имени и синтетические menu rows.
- Tests, migrations, dependency resolution, Docker/live checks и M3.2 regression в analysis checkpoint не запускались; это задачи implementation/acceptance после подтверждения владельца.

## M4 MCP Tools — implementation

- Добавлен `mcp==2.0.0`; frozen lock содержит 103 packages.
- `mcp_server` — отдельный stdio process на official Python MCP SDK v2; backend adapter использует explicit `stdio_client`, restricted child environment, typed structured results и startup/tool/shutdown timeouts.
- `search_menu` ищет только available dishes, поддерживает category/all dietary tags, экранирует LIKE wildcards, стабильно сортирует и возвращает не более 20 rows.
- `get_dish` возвращает явные ingredients/allergens/dietary tags, integer price, currency и текущую availability.
- `calculate_cart` агрегирует duplicate dish IDs, одним запросом проверяет rows, целиком отклоняет missing/unavailable/non-EUR cart и возвращает только deterministic EUR subtotal без delivery.
- Bounds: 10 dietary tags, 20 distinct cart lines, quantity 1–20 после aggregation, максимум 100 units. Tool errors и unexpected failures не раскрывают DSN/DB details.
- Migration `0002` добавляет `dietary_tags TEXT[] NOT NULL DEFAULT '{}'`; M1 migration не изменена. Tags не выводятся из ingredients/allergens; Sheets sync остается M7.
- Все repository calls выполняются в PostgreSQL transaction `READ ONLY`. MCP не подключён к LangGraph и не создаёт cart/order/Calendar side effects.

## M4 проверки

| Проверка | Результат |
|---|---|
| Targeted M4 unit/protocol/client/contracts | PASS: 24 tests до final additions |
| Full offline final | PASS: 100 passed, 7 external skips |
| Real stdio + PostgreSQL | PASS: tools/list + 3 calls, filter, duplicate aggregation, safe unavailable error, process exit, row count unchanged |
| Alembic `0001 → 0002` + repeated upgrade + schema/ORM compare | PASS: 2 integration tests |
| M3.2 checkpoint + Qdrant integrations | PASS: 4 tests |
| Docker frozen build/recreate | PASS: `mcp==2.0.0`, backend polling startup healthy |
| Health | PASS: backend live/ready and Qdrant HTTP 200 |
| Live FAQ regression | PASS: answer contains `12:30` |
| Secure backend log scan | PASS: exact=0, generic=0, traceback=0 |
| Lock/compile/diff | PASS |

Промежуточные диагностические FAIL: first real stdio run выявил, что `mcp==2.0.0` требует explicit `stdio_client(parameters)` вместо shorthand из newer docs; adapter исправлен, повтор PASS. Первый checkpoint/Qdrant container run потерял project `asyncio_mode=auto` из-за частичного read-only mount; повтор с явной pytest option PASS. Две однострочные diagnostic Python команды имели syntax/quoting errors и были исправлены; implementation на них не зависела.

## M4 ограничения и следующий шаг

- M4 **READY**. После acceptance владелец отдельно разрешил штатный upgrade основной development-БД `restaurant`: `alembic upgrade head` успешно перевёл revision `0001 → 0002`. Поле `menu_items.dietary_tags` подтверждено как `TEXT[] NOT NULL DEFAULT '{}'`; список из 13 таблиц и row counts до/после совпали, PostgreSQL и backend health PASS. Рабочие menu rows не добавлялись и не изменялись (таблица `menu_items` была пуста).
- Runtime Menu может быть пустым до M7 Sheets Menu sync; M4 tools корректно возвращают empty search/missing dish.
- На момент закрытия M4 adapter намеренно не был подключён к order placeholder; это historical M4 boundary, superseded реализацией M5 ниже.
- Manual Telegram chat E2E в M4 не повторялся; existing polling startup, health, live FAQ и automated graph regressions PASS.

## DEV Menu Seed — подготовка к M5

- Добавлена явная команда `python -m scripts.seed_menu`; она не запускается при startup и не меняет schema. Стабильные UUID5 и `dev-menu-001`…`dev-menu-015` определяют только принадлежащие seed строки; ownership conflict прерывает atomic transaction без изменений.
- Основная development-БД `restaurant` проверена до записи: PostgreSQL accepting connections, Alembic `0002 (head)`, `menu_items=0`. Первый запуск: `created=15`, `updated=0`, `total=15`; повторный и финальный reconcile: `created=0`, `updated=15`, `total=15`.
- Распределение: Закуски 3, Салаты 3, Основные блюда 5, Десерты 2, Напитки 2. В наборе 1 unavailable item, 13 items с allergens, 9 items с explicit dietary tags (`vegan`, `vegetarian`, `alcohol_free`). Invalid/duplicate/empty-required checks: 0.
- MCP adapter smoke PASS: tool list; search by name/category/dietary tag; available/unavailable `get_dish`; multi-item subtotal `2450`; duplicate aggregation quantity `3`, subtotal `2250`; unavailable cart rejected.
- Targeted seed tests PASS: 3. Full offline regression PASS: 103 passed, 7 external skips. Compile, Docker build/config, frozen lock, `git diff --check`, PostgreSQL/backend live/ready PASS.
- M4 остаётся **READY**, M5 **NOT STARTED**. Google Sheets Menu sync, persistent cart/orders и LangGraph wiring не реализовывались.

## M5 Cart & Confirmation — analysis checkpoint

**Статус:** ANALYSIS COMPLETE / IMPLEMENTATION NOT STARTED. На момент анализа `main` чиста и фактически синхронизирована с `origin/main` на commit `c638a91`; remote SHA проверен через `git ls-remote`.

- Baseline подтверждён: working DB `restaurant`, Alembic `0002 (head)`, 15/15 DEV menu rows; `orders`, `order_items`, `calendar_jobs`, `restaurant_settings` пусты. Данные и schema не изменялись.
- Рекомендуется расширить существующий persistent `AgentState` JSON-примитивами для cart, dialog step, pending selection, contacts, quote snapshot, confirmed draft, draft revision и Telegram replay metadata. Отдельная session/cart storage для M5 не нужна; official PostgreSQL checkpointer обеспечивает restart recovery.
- Cart mutation должна быть чистой deterministic domain logic; menu lookup/details и каждый quote выполняются только через существующий `MenuMcpClient`, а subtotal — только `calculate_cart`. Перед подтверждением quote и delivery fee читаются повторно; любое изменение возвращает новую сводку и требует нового подтверждения.
- M5 хранит только confirmed draft/confirmation key в checkpoint и не пишет `orders`, `order_items`, `calendar_jobs`. M6 обязан повторно валидировать quote и идемпотентно сохранить order; M5 confirmation сама по себе не является размещением заказа.
- Telegram `update_id` предлагается сохранять в checkpoint вместе с last response: текущий per-thread lock сериализует сообщения одного process, а replay guard предотвращает повторную cart mutation после crash/retry. Multi-instance по-прежнему вне scope.
- Два blocking design inputs: full-menu browse невозможно с текущим обязательным `search_menu.query`; delivery fee не настроена и не может быть hardcoded. Также требуется подтвердить semantics requested time/delivery и PII retention в checkpoint.
- Tests/implementation/build в analysis task **NOT RUN**. Выполнены только read-only Git/remote, code/config и DB diagnostics. M5 остаётся **NOT STARTED** до решения владельца.

## M5 Cart & Confirmation — implementation

- Реализован ADR-021 hybrid pipeline: deterministic guards → strict Pydantic `OrderIntentProposal` через OpenAI только при необходимости → MCP dish resolution → deterministic cart/contact/confirmation logic. Contact values и full checkpoint не отправляются LLM.
- Existing `search_menu` расширен optional query: пустой запрос возвращает available menu, максимум 20 строк, deterministic order; category/dietary filters и старые M4 calls сохранены.
- Checkpoint state содержит bounded canonical cart, stage, pending candidates/action, validated contacts, quote/revision, immutable confirmed snapshot и до 32 replay entries. Telegram handler передаёт `update_id`; duplicate/out-of-order delivery не повторяет mutation.
- Cart поддерживает multi-item add, repeated increment, absolute set, decrement-one, full remove, clear/view/recalculate и limits 20 lines/20 per dish/100 total units. Ambiguous/missing dish не изменяет cart.
- Delivery-only flow валидирует name/phone/address; exact/relative precise time нормализуется в `Europe/Madrid`, ASAP хранится `None`, vague/past time отвергается.
- Subtotal всегда приходит из MCP `calculate_cart`; `OrderSettingsRepository` read-only читает `delivery_fee_minor`; total вычисляется integer minor units. Main development setting явно создан со значением `350`.
- Confirmation принимает только exact «Подтверждаю» в `awaiting_confirmation`, перед ним перечитывает menu quote и delivery fee. Price/fee/revision change показывает новую summary; repeat confirm сохраняет один confirmation key. Ответ: «Данные заказа подтверждены. Заказ еще не размещен.»
- M5 не пишет `orders`, `order_items`, `calendar_jobs`, не вызывает Calendar/Sheets/payment и не реализует M6.

## M5 проверки

| Проверка | Результат |
|---|---|
| Compile | PASS: `.venv/bin/python -m compileall -q app mcp_server tests` |
| Unit | PASS: 113 passed |
| Full host suite без external URLs | PASS: 113 passed, 8 skipped |
| Isolated PostgreSQL integrations | PASS: 5 passed; migrations/schema, real stdio MCP, official checkpoint, full M5 flow/replay |
| M4 protocol optional browse | PASS: omitted query accepted; existing filtered query preserved |
| Main development DB setting | PASS: revision `0002`; `delivery_fee_minor=350`; 15 menu rows; orders/order_items/calendar_jobs remain 0 |
| Docker frozen build | PASS |
| Live structured OpenAI intent | PASS: synthetic multi-item phrase → typed `add` with quantities 2/1; no PII/state/DB mutation |
| Backend/PostgreSQL/Qdrant runtime | PASS: polling startup, live/ready/Qdrant HTTP 200; PostgreSQL healthy |
| Startup logs | PASS: Telegram token redacted; no traceback/startup error |
| `git diff --check` | PASS |
| Lockfile | PASS: `uv lock --check`; unchanged dependency set |
| Manual Telegram E2E M5 | NOT RUN: requires owner chat confirmation |

Автоматический replay сохраняет mutation и processed update metadata одним terminal graph node update. Если process падает до этого checkpoint, update безопасно выполняется заново; если после checkpoint, повтор возвращает cached response и mutation не повторяется. Это не заменяет distributed lock: одновременно должен работать один polling/backend instance.

M5 остаётся **ACCEPTANCE PENDING**, пока владелец не подтвердит ручной Telegram E2E: menu browse/search/detail, multi-item add/edit/remove/view, FAQ interleaving, contact validation, vague/past time rejection, restart recovery, summary, exact confirmation/reconfirmation и cancel. Следующий milestone M6 не начинать без отдельного задания.

## M5 Manual E2E defects — diagnostic checkpoint

**Статус:** ANALYSIS COMPLETE / FIXES NOT STARTED. Application code, tests, schema, dependencies и runtime data не изменялись.

- **DEF-M5-001 подтверждён, BLOCKER:** `OrderDialog._handle()` после `order_stage=confirmed` разрешает только повторный `confirm`; `new_order` отсутствует в action contract. `/start` обрабатывается отдельным `CommandHandler` и checkpoint не меняет, `cancel` также блокируется confirmed guard.
- **DEF-M5-002 подтверждён:** prompt требует вернуть названия «словами пользователя», LLM фактически вернул `брускетты`, а MCP casefold/LIKE search не нашёл canonical `Брускетта с томатами`. Initial multi-item add не меняет cart до разрешения всех строк, но обнаружен связанный gap: после numbered clarification pending state хранит только одну ambiguous line и может потерять остальные строки batch.
- **DEF-M5-003 частично подтверждён как ограниченный grammar:** direct reproduction PASS для `завтра 19:30`, будущего `сегодня 20:00`, `2026-11-19 19:30`, `19.11.2026 19:30`; vague и past корректно rejected. `завтра в 19:30`, time-only и textual month сейчас rejected. Для root cause конкретного manual failure нужен точный пользовательский ввод.
- **DEF-M5-004 классифицирован как ожидаемая safe ambiguity + UX gap:** обе формулировки route в FAQ; live retrieval для vague phrase нашёл `faq-001` score `0.6094`, но grounded answerer отказался считать вопрос однозначно относящимся к часам и вернул fallback. Автоматически подменять смысл на «часы работы» нельзя.
- **DEF-M5-005 подтверждён как корректное business-idempotent поведение по коду:** repeat confirm в `confirmed` не вызывает recalc/UUID creation и не меняет snapshot; duplicate update возвращается replay cache. Coverage следует усилить deep equality и official saver restart tests.
- Targeted existing regression: **PASS, 24 tests**. Live/read-only diagnostics не изменяли checkpoint/cart/menu/DB.
- Предлагаемый fix scope и owner decisions зафиксированы в `history/018_M5_defects_analysis_report.md`. M5 остаётся **IN PROGRESS**; M6 запрещён до исправления и повторного manual acceptance.

## M5 Manual E2E defects — fixes

**Статус:** IMPLEMENTED / AUTOMATED PASS / OWNER MANUAL ACCEPTANCE PENDING.

- **DEF-M5-001:** добавлены deterministic `new_order`, non-destructive state-aware `/start`, draft-only `cancel` и FIFO `confirmed_order_archive`. Лимит — пять snapshots суммарно на thread; повторный update не архивирует дважды благодаря existing replay guard. Business order rows не создаются.
- **DEF-M5-002:** `ProposedItem` хранит original name и canonical `lookup_query`; resolver последовательно использует оба запроса, MCP repository получил Russian PostgreSQL FTS fallback. Ambiguous IDs выбирает только пользователь. `PendingBatch` сохраняет весь proposal и unresolved indices; cart меняется атомарно после полного resolution.
- **DEF-M5-003:** удалён широкий natural-language parser. ASAP хранится как `None`; ручное значение принимается только в будущем формате `ДД.ММ.ГГГГ ЧЧ:ММ`, timezone `Europe/Madrid`. Telegram показывает inline choices ASAP/manual; полноценный calendar/slots UI не реализован.
- **DEF-M5-004:** ADR-011 расширен безопасным clarification outcome. Threshold не менялся; конфликт/нет context/ошибка по-прежнему возвращают grounded fallback.
- **DEF-M5-005:** logic не менялась; deep-equality/replay/PostgreSQL restart tests подтверждают неизменность current snapshot/key, сохранение archive и новый key второго заказа.
- Main development DB после тестов: `orders=0`, `order_items=0`, `calendar_jobs=0`, `menu_items=15`, `delivery_fee_minor=350`.
- Проверки: unit **128 PASS**; full host **128 PASS / 8 external skips**; isolated Docker full suite **136 PASS**; real stdio MCP, PostgreSQL FTS, official checkpoint restart/replay, migrations и Qdrant integrations PASS. Live structured OpenAI вернул canonical lookup для 4/4 русских фраз; live FAQ вернул безопасное уточнение без фактов. Frozen build, live/ready health, `uv lock --check`, compile и `git diff --check` PASS.
- M5 остаётся **IN PROGRESS** до ручного Telegram E2E владельца. M6 не начинать.

## M5 Manual E2E round 2 — diagnostic checkpoint

**Статус:** ANALYSIS COMPLETE / DEF-M5-006–008 FIXES NOT STARTED. Application code, tests, schema, dependencies и runtime data не изменялись.

- **DEF-M5-006 подтвержден:** для `awaiting_confirmation` нет отдельного deterministic routing guard, alias «нет» отсутствует. Поэтому сообщение проходит через structured LLM classification и может попасть в FAQ. Existing `edit` сохраняет draft и инвалидирует quote, `cancel` очищает только active draft, а `_confirm()` уже выполняет fresh quote/reconfirmation; требуются state-aware aliases и Telegram confirm/edit/cancel controls.
- **DEF-M5-007 подтвержден как сочетание safe no-match и transport failure:** extractor сохранил опечатку в canonical query (`брускету` → `брускета`), MCP exact/substring/Russian FTS вернули zero, dialog подготовил безопасное clarification без mutation. Его `sendMessage` завершился `httpx.ConnectError`, поэтому пользователь не увидел ответ. Корректное написание затем прошло штатно.
- **DEF-M5-008 подтвержден:** первый confirm был рассчитан, записан official PostgreSQL checkpointer вместе с `confirmed_order`, confirmation key и replay result, после чего `sendMessage` завершился `httpx.ConnectError`. Повторный новый Telegram update вернул прежний confirmed response; snapshot/key повторно не создавались. Причина — delivery failure после durable state transition, не idempotency defect.
- Runtime log evidence: два `NetworkError: httpx.ConnectError` в `reply_text()` — `2026-11-18 15:19:50.893 UTC` и `15:27:16.833 UTC`; polling продолжился, последующие `sendMessage` получили HTTP 200. Current handlers не перехватывают send errors, а Telegram Application error handler не зарегистрирован.
- Targeted regression: **PASS, 29 tests** (`test_order_dialog`, `test_agent_replay`, `test_faq_telegram`). Повторный Docker CLI diagnostic в конце анализа **NOT RUN** из-за локального `snap-confine` capability error; использованы уже полученные runtime logs/checkpoint evidence, reset/restart не выполнялись.
- Минимальный fix scope, UX, tests и риски зафиксированы в `history/020_M5_round2_diagnostics_report.md`. M5 остаётся **IN PROGRESS**; M6 не начинать.

## M5 Manual E2E round 2 — fixes

**Статус:** DEF-M5-006–008 IMPLEMENTED / AUTOMATED PASS / OWNER MANUAL ROUND 3 PENDING.

- **DEF-M5-006:** summary получила revision-bound inline actions Confirm/Edit/Cancel. Edit menu покрывает cart/name/phone/address/time и fresh summary; фактическое изменение сохраняет остальные fields, повышает revision, инвалидирует quote и требует explicit reconfirm. Cancel двухшаговый. Stage-local text aliases обрабатываются до LLM; stale stage/revision callback возвращает safe error без mutation.
- **DEF-M5-007:** после original/canonical/MCP/FTS zero-match decrement/remove/set выполняется strict deterministic comparison только по active cart (до 20 lines). Unique close candidate показывается numbered; выбор всегда явный, weak/tied result не мутирует cart. Existing `PendingBatch` сохраняет atomicity и return stage.
- **DEF-M5-008:** text, `/start` и callback responses используют общий safe sender. Chained `httpx.ConnectError` получает максимум две попытки суммарно; `TimedOut` и другие ambiguous outcomes не retry. Зарегистрирован PTB error handler; logs содержат только exception type/operation/attempt/outcome/duration. Checkpoint-before-send/replay не менялись.
- Tests: targeted **43 PASS**; host full **142 PASS / 8 external skips**; isolated Docker full **150 PASS**, включая real MCP stdio, official PostgreSQL checkpoint/restart, callbacks, persisted confirmation after simulated delivery failure, migrations и Qdrant. Frozen build, compile, `uv lock --check`, `git diff --check`, live/ready/Qdrant 200 и startup/polling log scan PASS.
- Development DB invariants: `menu_items=15`, `delivery_fee_minor=350`, `orders=0`, `order_items=0`, `calendar_jobs=0`. Tests использовали и затем удалили только `restaurant_m5_round2_test`; development rows не изменялись.
- M5 остается **IN PROGRESS** до manual Telegram round 3. Durable outbox/orders/Calendar/M6 не реализованы.

## M5.2-A1 — защита активного заказа

**Статус:** IMPLEMENTED / AUTOMATED PASS / OWNER ACCEPTANCE PENDING.

- Destructive `new_order`, `clear` и `cancel`, включая LLM proposals и deterministic exact commands, при непустом active draft больше не очищают state сразу. В checkpoint сохраняется optional typed `pending_destructive_action` с return stage, а cart, contacts, quote и `draft_revision` остаются без изменений.
- Revision-bound confirmation/decline callbacks проходят existing AgentService/LangGraph replay path. Только актуальное подтверждение выполняет прежнюю mutation; отказ восстанавливает прежнюю stage без изменения draft. Для `new_order` Telegram показывает «Начать новый заказ» / «Оставить текущий»; clear и cancel также имеют отдельные deterministic confirmations.
- Deterministic policy: «Заказ» возвращает clarification; «Хочу оформить заказ» → checkout текущего draft; «Покажи мой заказ» → view cart. ContextBuilder, dialog history и новый LLM contract отложены в M5.2-A2.
- Проверки: targeted **52 PASS**; unit **151 PASS**; full host **151 PASS / 8 external skips**; compile и `git diff --check` PASS. `uv lock --check` NOT RUN на host (`uv` отсутствует в PATH); dependencies/lockfile не менялись. PostgreSQL integration, real Telegram/MCP и Docker NOT RUN: для A1 не требовались и без отдельной test-service конфигурации могли затронуть внешние сервисы/данные.
- Schema, MCP contracts, business tables/config/dependencies не менялись. M5 остаётся **IN PROGRESS**, M6 не начинать.

## M5.2-A1 — correction уникальности destructive confirmation

**Статус:** IMPLEMENTED / AUTOMATED PASS / OWNER ACCEPTANCE PENDING.

- Каждый pending `new_order`, `clear` или `cancel` получает новый 12-character URL-safe `confirmation_id` (72 bits entropy). Он сохраняется в existing optional `pending_destructive_action` и добавляется только к destructive callback payload.
- До mutation проверяются одновременно action allowlist, current stage, `draft_revision` и exact `confirmation_id`. После decline/confirm pending marker очищается; повторный request с той же revision/stage имеет другой ID, поэтому старая кнопка возвращает `stale_callback`.
- Ordinary text в destructive confirmation stage больше не отображается на confirm/decline actions и не может обойти callback validation. Legacy pending state без ID fails safe как stale; checkpoint schema migration не требуется.
- Проверки: targeted **61 PASS**; unit **160 PASS**; full host **160 PASS / 8 external skips**; compile и `git diff --check` PASS. PostgreSQL/external integrations NOT RUN без test-only URLs; working DB/services не менялись. Host `uv lock --check` NOT RUN (`uv` отсутствует), dependency files не менялись.
- M5.2-A2/M6 не начинались; M5 остаётся IN PROGRESS до manual owner acceptance.

## M5.2-A2 — read-only gap analysis

**Статус:** ANALYSIS COMPLETE / IMPLEMENTATION NOT STARTED.

- Git baseline: `main` / `db0fbca3cd86e9e3a0a0c16c6e5279b9430271a4`, local tracking ref `origin/main` совпадает с HEAD. До отчетности tracked working tree был чистым; `docs/M5_2_DESIGN_HANDOFF.md` найден как пользовательский untracked-файл и не редактировался.
- Код подтверждает отсутствие `ContextBuilder`, bounded dialog history, typed `action/clarify/unsupported` outcome и generic `pending_clarification`. Checkpoint хранит последнее raw `user_message`, order state и replay responses, но не semantic exchanges/callback meanings.
- A1 по коду и unit tests подтвержден: destructive barrier для `new_order/clear/cancel`, unique 12-char session, stage/revision/session validation, fail-safe legacy callback, no text confirmation, decline без business mutation и replay exactly once.
- Подтвержден A2 gap: при `collecting_name` фраза «Покажи салаты» проходит существующий name validator; referent «этого блюда», retrospective statement и previous-order query не имеют typed context. FAQ interleaving покрывается только узким heuristic.
- Рекомендованы pure allowlist `ContextBuilder`, максимум 10 typed semantic events / 5 exchanges без raw text/PII, separate LLM action allowlist, discriminated outcome union и отдельный session-bound `PendingClarification`. Existing `PendingBatch` остается atomic; A1 destructive pending имеет высший приоритет.
- A2 рекомендуется разделить на два increments: (1) contracts/context/history/typed extractor; (2) clarification и mandatory contextual scenarios. A2 не вводит `ui_view`, CatalogController или A4 `summary_session`, но сохраняет их независимость от `draft_revision`.
- Privacy blocker для решения владельца: validated contacts уже не передаются extractor, однако arbitrary current raw message может само содержать PII; regex не дает гарантии. Рекомендуется no-LLM на contact stages и fail-closed gate для obvious PII, с явным согласованием остаточного риска.
- Проверки этого задания: targeted unit 68 PASS; all unit 160 PASS; `git diff --check` до отчетности PASS. PostgreSQL/MCP/Qdrant/Docker/live OpenAI/Telegram NOT RUN; runtime data не читались и не изменялись.
- Полный gap/contract/file/test plan: `history/025_M5_2_A2_analysis_report.md`. A2/A3/A4/A5/M6/M7 не реализовывались; M5 остается IN PROGRESS.

## M5.2-A2.1 — Contextual Intent Foundation

**Статус:** IMPLEMENTED / TESTED / OWNER ACCEPTANCE PENDING.

- Добавлен pure `ContextBuilder`, принимающий explicit projection вместо full state. `OrderContext` содержит stage/expected input, cart counts, current LLM action allowlist, typed history, trusted public dish label и только confirmed facts/counts; contacts, UUID/IDs, callback/session data, quote/snapshot bodies и replay responses исключены.
- OpenAI boundary заменен на strict discriminated `ActionOutcome | ClarifyOutcome | UnsupportedOutcome`; LLM-visible actions отделены от confirm/edit/callback-only transitions. Validated action адаптируется в existing `OrderIntentProposal`, deterministic `OrderDialog` остается владельцем mutation/validation.
- Contact stages не вызывают contextual LLM. Deterministic category/question guards выполняются до contact validation; «Покажи салаты» больше не сохраняется как имя. FAQ-вопрос сохраняет checkout state. Obvious phone/email/token/UUID/private-key patterns fail closed; regex не объявляется абсолютной PII protection.
- Checkpoint получил optional bounded semantic history: максимум 10 FIFO events без raw text/PII/IDs, callback meaning без payload, `last_action` и trusted MCP public dish label. Event сохраняется terminal graph update вместе с replay/mutation; duplicate update его не дублирует. Новый заказ очищает прежний transient context.
- A1 session/stale/text-bypass, per-thread lock, replay, atomic pending batch, cart/MCP/pricing, immutable confirmed snapshots и no-business-row invariants сохранены. Schema, migrations, dependencies и Telegram callback protocol не менялись.
- Проверки: compile PASS; new A2.1 unit 12 PASS; all unit 172 PASS; full host 172 PASS / 8 external SKIP; changed-file Ruff PASS; `git diff --check` PASS. Global Ruff обнаружил только два pre-existing unrelated unused imports в неизмененных файлах. Test-only PostgreSQL/MCP/Qdrant URLs отсутствуют; integrations SKIPPED. Live OpenAI/Telegram NOT RUN по заданию.
- Persistent clarification/session, previous-order/referent resolution, A3 UI, A4 summary session, A5 images и M6 не реализовывались. Полный отчет: `history/026_M5_2_A2_1_report.md`.

## M5.2-A2.1 — подготовка Live E2E

**Статус:** BLOCKED FOR SAFE STARTUP / OFFLINE PRECHECK PASS / LIVE NOT VERIFIED.

- Baseline сохранен: `main`, HEAD и local `origin/main` = `db0fbca3cd86e9e3a0a0c16c6e5279b9430271a4`; существующие A2.1 и owner-provided изменения не редактировались и не удалялись. Commit/push не выполнялись.
- Текущий runtime по архитектуре запускается как один `backend` (`uvicorn app.main:app`): при `TELEGRAM_ENABLED=true` он открывает official PostgreSQL saver, собирает LangGraph/OrderDialog, запускает Telegram long polling; MCP menu server создается дочерним stdio process по запросу. PostgreSQL и Qdrant — Compose dependencies.
- `.env` проходит `Settings` validation; Telegram, FAQ, OpenAI, HMAC, PostgreSQL и Qdrant параметры присутствуют. Значения секретов не выводились. Обнаружено, что `.env` имеет permissions `0664`; перед live запуском рекомендуется ограничить доступ до owner-only.
- Безопасного отдельного E2E окружения нет: `TEST_DATABASE_URL`, `TEST_CHECKPOINT_DATABASE_URL`, `TEST_MCP_DATABASE_URL`, `TEST_QDRANT_URL` отсутствуют; configured PostgreSQL имеет non-test database name. Рабочие БД/Qdrant и runtime business data не читались и не изменялись.
- Docker/Compose precheck BLOCKED: все обращения к Docker CLI завершаются локальным `snap-confine` capability error (`cap_dac_override`). Поэтому `docker compose config --quiet`, `ps`, build и startup smoke фактически не выполнены.
- Installed Python/runtime dependencies найдены в `.venv`; `uv` отсутствует в PATH, а `pip check` недоступен, потому что в project venv нет module `pip`. A2.1 offline checks PASS: strict Pydantic schema generated, discriminated outcome присутствует, LangChain strict `json_schema` binding создается без network call; 12 context tests и 46 dialog/replay tests PASS.
- Реальный OpenAI structured-output call, Telegram polling/chat, PostgreSQL checkpoint, MCP stdio, Qdrant и health endpoints NOT VERIFIED: запуск был намеренно пропущен из-за отсутствия подтвержденной test-only isolation и неработающего Docker CLI.
- До ручного E2E нужны: исправный Docker/Compose, отдельный Compose project/volumes и test-only `.env`, отдельная test database/configuration, проверка отсутствия второго poller для Telegram bot и явное разрешение на реальный OpenAI API call. Полный runbook и сценарии: `history/027_M5_2_A2_1_live_e2e_preparation_report.md`.

## M5.2-A2.1 — диагностика маршрутизации сообщения

- В обычных стадиях retrospective-фраза доходит до contextual extractor; в `collecting_*` heuristic `?` + буква отправляет ее в FAQ до extractor.
- Предыдущий ответ на точное «Заказ» не согласуется с contact stage при одном checkpoint: там `unknown` proposal передается в contact capture. Реальный live route без `order_stage`/`intent`/`error_code` не установлен.
- Fake extractor: `idle` и `awaiting_confirmation` + valid `view_cart` → order/view_cart; `collecting_name` → FAQ без extractor. `view_cart` разрешен allowlist и не отклоняется adapter/dialog.
- Один fallback используется FAQ, unknown и agent-error paths. Гипотезы: LLM→FAQ+FAQ fallback; extractor unsupported/error→unknown fallback; иной state/thread/code; infrastructure failure.
- Fresh offline: 56 targeted unit PASS через system Python; project venv interpreter broken. Application code/tests/schema/dependencies не менялись. Отчет: `history/028_M5_2_A2_1_message_routing_diagnostics_report.md`.
- Future fix только после approval: узкий cart-aware deterministic retrospective `view_cart` guard и regression sequence. A1/A2.1 contracts остаются без изменений; commit/push не выполнялись.

## M5.2-A2.1 — точечная live-диагностика

- Старый запрос не восстанавливается: project log files отсутствуют; Docker stdout недоступен из-за host `snap-confine/cap_dac_override`; прежние records не содержали outcome/final route и безопасной корреляции.
- Установлена временная минимальная structured диагностика без текста/PII/IDs/secrets: `llm_called`, enum-like `llm_result`, exception-class `llm_error`, `final_route`, bounded `fallback_reason`.
- Бизнес-логика не менялась. Instrumentation находится только в `intent.safe_extract`, `OrderDialog.classify`, graph FAQ/unknown nodes и formatter allowlist.
- Checks: compile PASS; routing/context/dialog/graph targeted 65 PASS; `git diff --check` PASS. Logging suite NOT RUN на host Python 3.10 из-за existing `datetime.UTC`; project Python 3.12 symlink broken.
- Нужен один Telegram retry после backend restart. До свежих records причина и minimal fix не установлены; functional fix запрещен. Отчет: `history/029_M5_2_A2_1_targeted_live_diagnostics_report.md`.

## M5.2-A2.1 — `OpenAIInvalidRequestError`

- Live tuple подтвердил: LLM вызвана, `OpenAIInvalidRequestError` возник до structured outcome, adapter fail-closed дал `unknown`, затем `unsupported_message` fallback.
- Boundary: `gpt-4.1-mini`, default Chat Completions `/v1/chat/completions`, `temperature=0`, `max_retries=1`, `ContextualIntentResult`, `method=json_schema`, `strict=true`; locked `langchain-openai/openai/core` = `1.8.0/3.52.0/1.8.1`.
- Static strict schema имеет required/all fields и `additionalProperties=false`; конкретная API rejection reason из прежнего type-only record не определяется.
- Временно добавлены безопасные exception fields: HTTP status и allowlisted API code/type/param. Message/body/request/response/request ID не логируются.
- Checks: compile PASS; 65 targeted PASS; safe-field synthetic PASS; diff PASS. Требуется один rebuild/retry и только record `Order intent extraction failed`. Отчет: `history/030_M5_2_A2_1_openai_invalid_request_diagnostics_report.md`.
- Prompt/routing/business logic не менялись; functional fix и commit/push не выполнялись.

## M5.2-A2.1 — Structured Outputs compatibility fix

- Фактическая schema извлечена из bound `response_format` и SDK `type_to_response_format_param`, а не только `model_json_schema()`. Old on-wire domain union содержал `oneOf`, `discriminator`, defaults и unsupported length/item/range constraints; live API отклонял `response_format` HTTP 400.
- Добавлен flat API-only `ApiContextualIntentResult`/`ApiProposedItem` без domain constraints. Все properties required, objects forbid extras; nullable fields используют `anyOf`+`null`.
- После API parse DTO явно преобразуется в прежние `ActionOutcome/ClarifyOutcome/UnsupportedOutcome`; existing length/count/range/action validators, allowlist, PII guards и fail-closed behavior сохранены.
- `gpt-4.1-mini`, Chat Completions, `method=json_schema`, `strict=true`, prompt/routing/FAQ/MCP/cart/confirmation не изменены.
- Regression берет schema из real binding/SDK path, рекурсивно запрещает incompatible keywords и требует strict object shape. Targeted 67 PASS; context 14 PASS; changed-file Ruff/compile/diff PASS.
- Full host unit collection BLOCKED существующим broken Python environment (Python 3.10/missing packages; project 3.12 symlink missing). Live retry pending. Отчет: `history/031_M5_2_A2_1_structured_outputs_compatibility_fix_report.md`. Commit/push не выполнялись.

## M5.2-A2.1 — точечная диагностика `ValidationError`

- Live item-action завершился Pydantic `ValidationError` внутри extractor до domain outcome; old type-only record не показывает API parse vs DTO→domain rule.
- Synthetic `set_quantity`: quantity 2 PASS; null → `loc=[]/value_error` (`set_quantity requires quantity`); 21 → `loc=[items,0,quantity]/less_than_equal`. Общей contract incompatibility для valid quantity нет.
- Временно логируется только до 8 safe `{loc,type}` из `ValidationError.errors(include_input=False, include_context=False, include_url=False)`; input/ctx/message/payload/PII исключены.
- Targeted 67 PASS; Ruff/compile/diff PASS. Нужен один rebuild/retry и только `Order intent extraction failed` record. До loc/type functional fix не выполнялся.
- Полный отчет: `history/032_M5_2_A2_1_validation_error_diagnostics_report.md`; routing/FAQ/MCP/cart/confirmations не менялись, commit/push не выполнялись.

## M5.2-A2.1 — bounded domain-validation recovery

- Root `ValidationError` внутри extractor наиболее согласуется с model-level domain rejection, но старый run не имел pre-domain marker, поэтому exact parse/conversion stage и payload не доказаны. Возможные fixed rules перечислены и теперь безопасно диагностируются до validation; конкретный live rule остается hypothesis до повторного теста.
- Shared `action_validation_rule()` используется domain validator и diagnostics. Logs содержат только kind/action enums, item count, aggregate quantity flags и fixed rule code; payload/user text/names/PII отсутствуют.
- Ровно один LLM retry разрешен только после parsed API DTO для missing items, invalid single-item count, missing set quantity или missing search selector. Feedback содержит только fixed rule; invalid payload не отправляется. Все domain/PII/stage allowlist checks повторяются.
- API/network/schema errors, bounds и прочие нарушения не retry; second failure fail closed; max 2 calls. До valid result нет MCP/cart side effects. Replay guard unchanged.
- Tests: context 22 PASS; affected routing/order/replay 77 PASS; expanded agent/order set 95 PASS; Ruff/compile/diff PASS. Full host suite blocked existing Python environment. Live pending; Docker CLI locally blocked.
- Report: `history/033_M5_2_A2_1_validation_recovery_report.md`. No schema/dependency/FAQ/MCP/A1 changes; commit/push not performed.

## M5.2-A2.1 — DTO normalization и dev debug JSON

- Live root cause подтвержден: valid `add` item/quantity был отклонен только из-за extra selector fields (`selectors_not_allowed_for_action`). Adapter теперь выбирает поля по action; harmless irrelevant fields не попадают в domain payload и не вызывают retry.
- Item actions сохраняют items/quantity и drop selectors; search/browse сохраняют selectors и drop items; parameterless actions drop both. Unknown action/fields, contradictory variant fields, missing mandatory data, bounds, stage allowlist и PII guards остаются fail closed.
- Raw DTO проходит PII check до normalization. LLM schema descriptions/prompt фиксируют neutral fields и add increment vs set final quantity. Model/json_schema/strict unchanged.
- `LLM_DEBUG_RESPONSE_JSON=false` default; effective only for development/dev/local. Enabled logs parsed redacted DTO before normalization per attempt, max 4000 chars; arbitrary strings/PII/secrets/prompts/raw response не логируются. Production forced off.
- Tests: context 31 PASS; expanded affected 113 PASS; Ruff/compile/diff PASS. Full host suite blocked existing Python environment. Live acceptance pending.
- Report: `history/034_M5_2_A2_1_dto_normalization_debug_report.md`. No routing/FAQ/MCP/cart/DB/A1/dependency changes; no commit/push.

## M5.2-A2.2 — multi-action planning и atomic candidate cart

- Strict API DTO сохраняет single `kind=action` и добавляет flat `kind=actions` с required ordered `operations`. API schema не содержит discriminated union; model `gpt-4.1-mini`, `method=json_schema`, `strict=true` сохранены. Domain plan допускает 2–8 операций только `add`, `set_quantity`, `decrement`, `remove`.
- Adapter повторно валидирует каждую operation, stage allowlist и PII. Existing one-retry policy применяется только к тем же fixed repairable domain rules; bounds/API/network failures не retry. Неиспользуемые root single fields для `actions` обязаны быть neutral.
- `OrderDialog` сначала разрешает весь plan через существующий read-only menu/MCP path. Unknown/ambiguous/unavailable dish, conflict или cart bounds возвращают order-specific clarification и не меняют cart. Ambiguous choice сохраняет typed pending plan и продолжает resolution без ранней mutation.
- После полной validation операции применяются по порядку к локальной candidate cart. Cross-operation повтор одной resolved dish отклоняется как конфликт; повтор items внутри одного `add` остается deterministic aggregation. Успех возвращает один terminal cart update и одну revision invalidation; replay не повторяет batch.
- Failure-injection official saver semantics: при сбое final checkpoint state может восстановиться old либо из целого terminal pending write; промежуточного per-operation cart нет. Гарантия обязательного rollback к old при любой saver error требует отдельного изменения transaction architecture и не реализована.
- Проверки: A2.2/affected targeted **89 PASS**; доступный host unit subset **161 PASS**; OpenAI bound schema regression, PII/debug, replay и persistence fault injection входят в эти runs. Full `python3 -m pytest -q` **FAIL during collection**: system Python 3.10 не имеет `psycopg`, `gspread`, MCP/Postgres packages и `datetime.UTC`/`typing.Self`; project Python 3.12 venv ранее сломан. Docker Compose **NOT RUN**, `snap-confine` блокирует CLI (`cap_dac_override`). Ruff check/format, compile и diff check PASS. Live OpenAI/Telegram NOT RUN.
- Следующий шаг: rebuild backend после исправления Docker/venv и один manual Telegram сценарий с двумя `set_quantity` и фразой «не трогай» для третьего блюда. M5 остается IN PROGRESS; M6/commit/push не выполнялись.

## M5.2-A2.2 — quantity validation error handling

- Live root cause подтвержден кодом и diagnostic tuple: API DTO принимает unconstrained integer, `_api_result_validation_facts()` корректно определял `quantity_out_of_range`, но `api_result_to_outcome()` продолжал создавать domain item. `ProposedItem.le=20` выбрасывал `ValidationError`; generic extractor handler создавал unknown proposal без clarification, поэтому graph выбирал `final_route=unknown` и FAQ fallback.
- Добавлен backend-only typed `ValidationOutcome(validation_rule=quantity_out_of_range)`. Он создается только для явно превышенного max после output PII guard, до Pydantic item construction; domain `ge=1/le=20` сохранен. Adapter возвращает fixed order clarification: «Количество одного блюда не может превышать 20 порций. Корзина не изменена. Укажите допустимое количество».
- Single и multi overflow маршрутизируются в `order`; dialog не получает mutation proposal и не вызывает MCP/cart helpers. Overflow не retry. Quantity 20 валиден, 21/25 отклоняются. Quantity ниже minimum и unrelated `ValidationError` не считаются max-overflow и сохраняют прежний generic fail-closed behavior.
- Tests: affected targeted **94 PASS**; available host unit subset **166 PASS**; Ruff/format/compile/diff PASS. Full locked-runtime/Docker/live rerun ограничен прежней host environment проблемой. PostgreSQL schema, dependencies, FAQ, MCP, OpenAI model, prompt и routing rules не менялись.
- Следующий шаг: rebuild backend и повторить один live Telegram запрос из acceptance test 14. M5 остается IN PROGRESS; commit/push не выполнялись. Report/snapshot: `history/036_M5_2_A2_2_quantity_validation_report.md` / `history/036_M5_2_A2_2_quantity_validation_snapshot.zip`.

## M5.2-A2.2 — live acceptance closure и A2 gap review

Владелец подтвердил следующие Telegram результаты; дополнительные свойства этим проверкам не приписываются:

- **Test 10 PASS:** два `set_quantity` в одном сообщении; подтвержденная итоговая корзина — 48,50 EUR.
- **Test 11 PASS:** `add + remove + add` с неизвестной пиццей Маргарита; корзина не изменилась.
- **Test 12 PASS:** `remove + add + set_quantity` с неизменяемым лимонадом; подтвержденная итоговая корзина — 96,00 EUR.
- **Test 13 PASS по ответу бота:** конфликт двух операций над лососем был безопасно отклонен. Отдельная повторная проверка корзины владельцем не выполнялась, поэтому неизменность cart в этом live test не заявляется.
- **Test 14 PASS после quantity fix:** превышение 20 порций получило order-specific validation response; корзина не изменилась.

Итог: **M5.2-A2.2 LIVE ACCEPTED**. Quantity validation closure также принят. M5 в целом остается **IN PROGRESS**, поскольку design scope A2 выполнен не полностью, а A3/A4/A5 не начинались. M6 не начинался.

### Полнота A2 относительно `M5_2_DESIGN_HANDOFF.md`

| Пункт | Статус | Подтверждение и пробел |
|---|---|---|
| `pending_clarification` и продолжение | **NOT IMPLEMENTED** | `ClarifyOutcome` немедленно адаптируется в transient `OrderIntentProposal(action=unknown, clarification=...)`. В `AgentState`/contracts нет generic pending clarification/session/allowed resolutions; следующий ответ не разрешается deterministic с сохраненным clarification. Existing `PendingBatch` относится только к dish disambiguation и не закрывает semantic clarification. |
| Ссылка на ранее упомянутое блюдо | **PARTIAL** | State/context сохраняют bounded `last_referenced_dish_name`, semantic history и очищают referent при new order; dialog обновляет label после trusted dish resolution. Нет server-side `last_referenced_dish_id`, отдельного referent resolver и regression для «этого блюда»/allergens after reference; корректность пока зависит от повторной интерпретации public label LLM. |
| Вопрос о предыдущем заказе | **PARTIAL** | `OrderContext` передает только `has_confirmed_snapshot`/bounded count, а confirmed snapshot остается immutable. Но в `LlmAction`/dialog нет typed previous-order query/status action и нет truthful response/test для «Что с предыдущим заказом?». M5 не может сообщать business/Calendar/delivery status. |
| Контекст checkout | **PARTIAL** | `expected_input`, stage-specific allowlist, deterministic contact guards и sequential first-missing-field flow реализованы; уже введенные contacts сохраняются при menu/view/FAQ responses. Нет `checkout_suspended`, `ui_view`/navigation return context и автоматического возврата к ожидаемому полю после будущего каталога — это dependency A3/A4. |
| Состояние при FAQ/order switching | **IMPLEMENTED** в текущем text flow | `test_faq_during_checkout_preserves_order_state` подтверждает сохранение stage/cart/contacts; FAQ graph update меняет response/history, не draft. Contact-stage FAQ/navigation guards не сохраняют вопрос как contact. Catalog/summary session switching из design A3/A4 сюда не включается и еще не реализован. |
| Семантика `expected_input` | **PARTIAL** | Pure `expected_input_for_stage()` покрывает все текущие `OrderStage`, значение входит в bounded `OrderContext`; contact stages используют deterministic bypass. Оно полностью выводится только из `order_stage` и не моделирует pending clarification, `ui_view`, checkout suspension или summary session, поэтому не достаточно для полного design state priority. |

### Рекомендуемый следующий инкремент

Перед A3 рекомендуется отдельный **M5.2-A2.3 Semantic Continuation Closure**: typed bounded `pending_clarification` с deterministic resolution/reset; server-side dish referent (ID остается backend-only, public label — в LLM context); truthful previous-order query, ограниченный checkpoint facts; regression для «этого блюда», retrospective/previous-order и continuation. Не включать UI catalog, summary session, images или M6.

Обоснование: A3 использует dialog/navigation context и callback-only catalog, а A4 зависит от A3 для checkout suspension и от устойчивой semantic continuation. Переход к A3 сейчас оставит обязательные A2 acceptance scenarios 10–12 из design matrix без реализации и увеличит риск смешения semantic clarification с будущими UI sessions. A4 summary session остается отдельной от A1 `confirmation_id`, `draft_revision` и будущего clarification session.

Риски/зависимости A2.3: backward-compatible optional checkpoint state; bounded/PII-safe choices; stale/reset semantics; отсутствие ложного business order status до M6; referent invalidation при new order/cart/context changes; сохранение A1/replay/allowlist guarantees. Реализация следующего инкремента не начиналась и требует отдельного согласования.

Documentation-only review: application code, tests, dependencies и DB schema не менялись; automated tests **NOT RUN** как не требуемые для docs-only изменения. `git diff --check` PASS. Report/snapshot: `history/037_M5_2_A2_2_acceptance_closure_report.md` / `history/037_M5_2_A2_2_acceptance_closure_snapshot.zip`.

## M5.2-A2.3 — Semantic Continuation Closure

**Статус:** IMPLEMENTED / TARGETED TESTED / LIVE ACCEPTANCE PENDING.

- Добавлен optional typed `PendingClarification`: fixed reason (`order_intent`/`dish_referent`), 1–3 allowlisted choices, return stage и independent 12-character session ID. Состояние JSON-safe и сохраняется existing LangGraph checkpointer; raw message, prompt, PII и unbounded history не сохраняются.
- «Заказ» с непустой корзиной создает persistent clarification. Telegram callback проверяется по current `draft_revision`, session ID и server-side choice; свободный ответ использует deterministic aliases либо narrowed `OrderContext.allowed_actions`. Replay возвращает прежний result, stale callback не продолжает dialog. FAQ/topic switch, cancel и new order инвалидируют pending; A1 destructive stages остаются приоритетными.
- Existing numbered dish ambiguity не дублировалась: `dish_referent` только собирает название, затем передает его в прежний catalog resolver. `last_referenced_dish_id` backend-only, устанавливается после authoritative resolution; LLM получает только public label. «Какие аллергены у этого блюда?» вызывает fresh `get_dish(UUID)`, а без ID создает clarification.
- Добавлен typed `previous_order`. Ответ использует только current/latest archived `ConfirmedOrder`, не выводит contacts и прямо сообщает, что checkpoint snapshot не является размещенным business order; статусы приготовления/доставки до M6 недоступны. При отсутствии snapshot ограничение сообщается честно. Никаких mutations/Calendar/DB rows.
- `expected_input=clarification_choice` при active pending; allowlist сужается до saved choices. Старые checkpoints без optional keys продолжают использовать stage-derived semantics.
- Проверки: changed-file Ruff PASS; affected dialog/context/replay/Telegram **118 PASS**; Python compile PASS. Полный suite **FAIL during collection** в текущем host environment: system Python 3.10 не поддерживает project Python 3.12 APIs и не имеет части dependencies; project `.venv` ссылается на отсутствующий `/tmp/restaurant_uv_python/...`. Docker/integration/live Telegram NOT RUN. Global Ruff отдельно видит pre-existing unrelated unused `UniqueConstraint` в user-owned `app/db/models.py`.
- M5 остается **IN PROGRESS**, M6 не начинался. Следующий шаг — один bounded live Telegram acceptance pass A2.3; затем отдельное согласование A3. Commit/push не выполнялись.

## M5.2-A2.3 — Deterministic Checkout & PII Isolation analysis

**Статус:** ANALYSIS COMPLETE / IMPLEMENTATION NOT STARTED / OWNER PLAN APPROVAL PENDING.

- Live root cause подтверждается текущим кодом. В `collecting_name` строка «Продолжим оформление заказа» проходит `_validate_name()`, потому что состоит из Unicode letters/spaces и имеет допустимую длину. После перехода к `collecting_phone` вопрос с `?` проходит `_looks_like_faq()` и route `faq`; FAQ retrieval отправляет raw question в embeddings, а при найденном context — в FAQ ChatOpenAI. Следующая фраза детерминированно отклоняется phone validator. Поэтому summary сохраняет ранее принятую фразу как recipient.
- Contextual order extractor уже обходится для обычного contact value, phone/time validation deterministic, edit flow и explicit summary confirmation существуют. Но deterministic/exact actions, contact navigation и FAQ guard выполняются до contact capture, поэтому полной checkout isolation сейчас нет.
- `ContextBuilder` не принимает contacts, raw checkpoint, response/replay cache или confirmed snapshot bodies; semantic history хранит только typed `contact_input`. После следующего normal message старый contact text не попадает в order LLM context. Однако official checkpoints сохраняют `user_message`, `contacts`, summary `response` и replay responses; это разрешенное state storage, но отдельный retention risk. Central redactor маскирует secrets, а не произвольную PII, поэтому защита logs должна доказываться отсутствием PII-valued log fields.
- Согласованный minimal plan: ранний contact-stage gate до deterministic/FAQ/pending logic; defensive empty LLM allowlist для contact stages; отдельный callback-only `cancel_checkout`; optional 12-character checkout session token с revision/stage/session validation; cart preservation + full unconfirmed contacts/quote clear; сохранение existing edit/summary/A1/A2.2 behavior.
- Предлагаемые validators: Unicode NFC name с letters/combining marks и ограниченными separators, 2–100 chars, без digits/control/emoji; existing phone normalization и 7–15 digits сохранить; address NFC 5–300 chars, минимум одна Unicode letter, allowlisted punctuation, полный запрет control/format chars. Никакой validator не проверяет реальность имени/адреса.
- Конфликт документации: прежние FAQ/navigation-in-checkout требования в architecture/design/ADR-021 и tests должны быть явно superseded ADR-024 при implementation. A3/A4/A5/M6, schema, MCP и dependencies не требуются.
- Read-only evidence: focused existing tests **2 PASS** и synthetic validator probe подтвердил `name_phrase_accepted=true`, `phone_text_rejected=true`, `address_controls_currently_collapsed=true`. Application code/tests не менялись. `git diff --check` выполняется после отчетности.
- Полный план и PII audit: `history/039_M5_2_A2_3_deterministic_checkout_analysis_report.md`. Следующий шаг только после approval — реализация минимального checkout gate/session/callback и regressions. Commit/push не выполнялись.

## M5.2-A2.3 — Deterministic Checkout & PII Isolation implementation

**Статус:** **ACCEPTED (LIVE acceptance)**. Дата приемки: **2026-11-19**.

- `OrderDialog.classify()` теперь обрабатывает callback первым, затем немедленно направляет любой ordinary text на восьми initial/edit contact stages в deterministic field handler. FAQ heuristics/retrieval/embeddings, contextual extractor, semantic pending continuation и text-команды не вызываются. `OrderContext.allowed_actions` на contact stages пуст.
- Новый checkout один раз показывает согласованное предупреждение и вопрос «Как вас зовут?». При следующих полях и при edit warning не повторяется. Name/address используют Unicode NFC и allowlisted categories/punctuation; phone принимает optional leading `+` и 7–15 ASCII digits, control characters отклоняются; time flow не изменен.
- Добавлен callback-only `cancel_checkout` на всех contact stages. Он сохраняет cart, очищает unconfirmed contacts/quote/pending clarification/checkout session, возвращает `idle` и фиксированный ответ. A1 destructive cancel/confirmation и cart semantics не изменены.
- Optional 12-character `checkout_session_id` хранится в existing JSON-safe checkpoint state. Time/cancel callbacks проверяются по action, current stage, `draft_revision` и session. Старые/wrong-session/old-stage buttons fail safe; replay идет через existing processed-update guard; checkpoint restart с active session покрыт regression.
- PII boundary подтверждена spies: contact text не вызывает extractor/FAQ; после cancel subsequent LLM получает только новое сообщение и allowlisted `OrderContext` без name/phone/address; semantic history содержит только `contact_input`; synthetic sentinel values отсутствуют в diagnostic logs. Необходимые contacts и raw historical turns по-прежнему могут находиться в official checkpoint versions.
- Отложенная задача: определить retention/pruning и безопасное удаление PII из исторических checkpoint versions. Текущая очистка state не удаляет старые versions; в A2.3 это намеренно не реализовано.
- Automated verification реализации: changed-file Ruff **PASS**; targeted dialog/context/replay/Telegram unit/regression **166 PASS**; `compileall` **PASS**. Полный test suite в среде Codex не выполнялся из-за Python 3.10 вместо project 3.12 и отсутствующих project dependencies.
- Owner-confirmed LIVE acceptance: name/phone/address validation **PASS**; deterministic checkout text handling **PASS**; pre-checkout notice **PASS**; summary и name edit **PASS**; checkout cancellation с сохранением cart **PASS**; повторный checkout после cancellation **PASS**; stale callback protection **PASS**; restart recovery **PASS**; cancel callback после recovery **PASS**. В диагностических логах 29 записей: во всех `llm_called=false`, `final_route=order`.
- Границы подтверждения: cancellation непосредственно из contact edit mode отдельно не проверялась. Наблюдение диагностических логов не доказывает отсутствие любых внешних вызовов. Retention/удаление PII из historical PostgreSQL checkpoint versions остается отдельной production-задачей.
- DB schema/migrations, MCP contracts, OpenAI model/schema, dependencies, LangGraph topology, FAQ business logic и cart bounds не менялись. M5 остается **IN PROGRESS**; A3/A4/A5/M6 не начаты. A2.3 закрыт; следующий шаг — отдельное планирование следующего инкремента без автоматического начала реализации. Commit/push не выполнялись.
