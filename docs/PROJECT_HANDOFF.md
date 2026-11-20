# Project Handoff — контекст для нового чата

**M5.2-A3.1 ACCEPTED:** решение владельца от 2026-11-20, документальное закрытие — отчет 047. Owner LIVE acceptance отдельно не подтвержден. Реализация остается в текущем рабочем дереве; commit/push не выполнялись. A3.2 — NOT STARTED; перед следующим этапом отдельно согласовать scope и Codex prompt. ADR-025 и A3 design handoff сохраняются; отчеты 043–047 фиксируют реализацию и проверки.

**Актуализация D04/D05:** `browse_menu.category` обязателен и непустой; protocol и real PostgreSQL integration PASS, full unit 270 PASS. Общий Ruff format check выявил 16 ранее неформатированных файлов вне scope. Статус PARTIALLY VERIFIED, owner acceptance не проводился; см. отчет 045.

## Что это

Учебный Restaurant AI Agent. M1–M4 приняты. M5 остается IN PROGRESS: M5.2-A2.3 прошел owner LIVE acceptance, M5.2-A3.1 read-only MCP catalog принят владельцем без отдельного подтверждения LIVE acceptance. A3.2+ и M6 не начинались. Исходные DOCX остаются в ChatGPT Sources.

## Прежде чем продолжать

1. Прочитать все `docs/*.md` и `history/010_M3_2_acceptance_report.md`, если local history доступна.
2. Проверить `git status`/`git diff`: commit/push после M2/M2.1 не выполнялись.
3. Не начинать следующий этап без отдельного задания.
4. Считать M3.1 и M3.2 READY; общий M3 остаётся IN PROGRESS.

## Текущий статус

**M3.2 READY**. M1/M2/M3.1 также READY; общий M3 остаётся IN PROGRESS. В M3.2 реализовано:

- official `AsyncPostgresSaver` + Psycopg async pool на lifespan;
- explicit `scripts.setup_checkpoints`, no startup DDL, advisory lock;
- official tables `checkpoint_migrations`, `checkpoints`, `checkpoint_blobs`, `checkpoint_writes` вне Alembic/ORM;
- versioned HMAC-SHA256 Telegram thread identity с отдельным secret;
- partial state updates и `turn_count` для доказуемого восстановления;
- per-thread serialization без блокировки других threads;
- fail-closed startup/readiness и отсутствие in-memory fallback;
- strict primitive-only checkpoint deserialization policy.

Automated/live PASS: 82 offline tests + 6 integrations; setup twice; isolation; restart recovery; concurrent same-thread updates; Docker health; real FAQ 12:30; order/unknown; secret log scan. Владелец затем настроил постоянный HMAC secret, запустил backend и подтвердил `/start`, FAQ, order placeholder и unknown fallback — PASS. Codex эти ручные сценарии повторно не выполнял.

Постоянный `THREAD_ID_HMAC_SECRET` хранится только в ignored `.env` по подтверждению владельца. Не менять secret после появления persistent dialogs без отдельного плана ротации.

Открыто PERF-001: первый FAQ-запрос по наблюдению владельца занимает примерно 5–10 секунд, последующие быстрее; это повторялось на M3.1/M3.2. Причина не установлена. На будущем техническом этапе нужны безопасные duration metrics для LangGraph, embeddings, Qdrant retrieval, answer generation, checkpoint и Telegram send; не считать cold start/external connections доказанной причиной.

До M3.1 также прошло:

- read-only Google Sheets source и repeated live sync;
- OpenAI embeddings и grounded ChatOpenAI answers;
- Qdrant update `faq-001` без duplicate/re-embedding остальных;
- deactivate `faq-008` с удалением только одного FAQ point;
- active FAQ=9, metadata point=1;
- known/paraphrased/unknown threshold calibration;
- prompt injection/conflict/fallback;
- empty-source protection и failure recovery;
- unit/Qdrant/PostgreSQL/Docker regressions.

Владелец подтвердил Telegram E2E: bot отвечает на `/start` и FAQ. M2.3 сохранил включённый polling и сторонние HTTP logs, но централизованно удаляет token/API keys/Authorization/URL passwords из `msg`, `args`, object args и traceback. Live audit текущего container подтвердил `/getMe`, `/deleteWebhook`, `/getUpdates` status 200, timing fields и ноль secret hits.

## M2.1 решения

- `FaqSyncService.sync(allow_empty_source=False)` блокирует пустой source при непустом index.
- `python -m scripts.sync_faq --allow-empty-source` — единственный explicit opt-in полной очистки; metadata сохраняется.
- Stable UUID5/content hashes и upsert-before-delete остаются; tests доказывают recovery после embeddings/upsert/delete failures.
- Threshold `0.30` принят для текущего synthetic dataset (ADR-013), не универсален.
- `secrets/` игнорируется Git и Docker build context; credential mount read-only.

## Фактическое live состояние данных

- Google Sheets: `faq-001` answer = `12:30–23:00`; `faq-008 active=FALSE`.
- Qdrant collection `faq`: 9 FAQ points + 1 metadata point.
- `faq-008` отсутствует по source ID и в retrieval.
- Opening answer: «Ресторан открывается ежедневно в 12:30.»
- Deactivated reservation question: fixed fallback.
- Delivery FAQ продолжает отвечать по context.

Google Sheets изменял только пользователь. Codex использовал read-only adapter и самостоятельно строки не редактировал.

## Основные файлы

- `app/rag/sync_service.py` — guard/recovery algorithm.
- `scripts/sync_faq.py` — `--allow-empty-source`.
- `tests/unit/test_faq_sync.py` — fault injection.
- `tests/integration/test_faq_qdrant.py` — metadata/empty-source integration.
- `app/core/config.py` / `.env.example` — threshold `0.30`.
- `README.md` и ADR-012/013 — operator runbook/решения.

## Проверки

- Full offline: 45 PASS, 5 external skips.
- Qdrant integration: 3 PASS.
- PostgreSQL/Alembic: 2 PASS on dedicated test DB.
- Compose/health/CLI/mount/secret scans: PASS.
- Live Sheets/OpenAI/Qdrant: PASS.
- Telegram Bot API `getMe`: PASS на host и Docker.
- Telegram polling/chat E2E: PASS, подтверждено владельцем.
- Secure logging unit/live/error path: PASS; Qdrant integration 3 PASS.

Точные результаты реализации — `history/009_M3_2_report.md`; acceptance — `docs/DEVELOPMENT_STATUS.md` и `history/010_M3_2_acceptance_report.md`.

## Следующий безопасный шаг

1. M4 закрыт как READY; `search_menu`, `get_dish`, `calculate_cart`, backend adapter, migration `0002` и real stdio acceptance PASS.
2. Основная development-БД `restaurant` уже обновлена штатной командой `alembic upgrade head`: revision `0002 (head)`, точная схема `dietary_tags` и сохранность таблиц/counts проверены. Не повторять миграцию без отдельной причины.
3. Не подключать MCP к LangGraph автоматически. Следующий этап — M5 cart/confirmation, только по отдельному заданию владельца.
4. M7 должен добавить explicit `dietary_tags` в Sheets Menu sync; не выводить теги из ingredients/allergens.
5. PERF-001 перед оптимизацией требует измерений; orders/Calendar/admin sync остаются будущими этапами.
6. Основная development-БД уже содержит 15 synthetic DEV menu rows; повторное применение выполняется только явной командой `python -m scripts.seed_menu`.

## M4 handoff

- Dependency: `mcp==2.0.0`, lock 103 packages. Для этой версии adapter намеренно передаёт в `Client` explicit `stdio_client(StdioServerParameters(...))`.
- Runtime boundary: `app/menu` contracts/repository/service → `mcp_server` stdio tools → `app/mcp/MenuMcpClient`.
- PostgreSQL tool transactions выполняют `SET TRANSACTION READ ONLY`; price calculations — только integers/EUR.
- Limits: search 20 results; 10 tags; cart 20 lines; per-dish quantity 20 after duplicate aggregation; total 100 units.
- Real acceptance: isolated PostgreSQL, tools/list + all calls, safe failure, subprocess exit and no row-count side effect PASS. Позднее согласованный upgrade working development DB `0001 → 0002` также PASS; 13 таблиц и все row counts сохранены, `menu_items` оставалась пустой.
- M3.2 regression: 100 offline tests final, checkpoint/Qdrant/migrations integrations, Docker health/live FAQ/log scan PASS. Manual Telegram messages в M4 не отправлялись.

## DEV menu seed handoff

- `scripts.seed_menu` содержит 15 позиций европейского меню со стабильными UUID5 и owned source IDs `dev-menu-001`…`dev-menu-015`. Upsert atomic, сериализован PostgreSQL advisory lock и fail-closed при конфликте UUID/source ownership; произвольные строки не удаляются.
- Working DB `restaurant`: 15 seed rows, 15 distinct UUID, 15 distinct source IDs; categories `3/3/5/2/2`, одна unavailable позиция. Alembic остаётся `0002 (head)`.
- Идемпотентность фактически проверена повторным запуском: после первоначальных 15 inserts последующие runs возвращают `created=0`, `updated=15`, `total=15`.
- Existing `MenuMcpClient` smoke по working DB PASS для всех трёх tools, включая filters, deterministic subtotals, duplicate aggregation и safe unavailable rejection.
- M5 не начат: seed не подключает MCP к LangGraph, не создаёт cart/order/Calendar и не реализует Sheets Menu sync.

## Состояние Git после handoff

Владелец выполнил commit и push M4 и DEV Menu Seed. До M5 analysis ветка `main` была чистой; local HEAD, tracking `origin/main` и фактический remote SHA совпадали: `c638a91`. После analysis изменены только обязательные status/handoff docs и history artifacts; application code, schema и working data не менялись.

## M5 analysis handoff

- M5 implementation не начата. Предлагается stateful deterministic order dialog поверх existing LangGraph + official PostgreSQL checkpointer; отдельный persistent cart/table в M5 не нужен.
- State должен содержать только JSON-safe values: `{dish_id: str, quantity: int}` cart rows, dialog step, bounded candidates, contacts, quote/confirmed snapshots, revision, confirmation key, `last_processed_update_id` и cached response. UUID/datetime сохранять строками, деньги — integer minor units.
- Menu lookup/details/quote идут через `MenuMcpClient`; `calculate_cart` обязателен для view/summary/confirm. Cart/contact mutation не выполняется при MCP failure. Перед confirm повторно читать quote + delivery fee и требовать reconfirm при изменении.
- M5 не пишет `orders`, `order_items`, `calendar_jobs`; confirmed draft живёт в checkpoint и пригоден для M6. Повторный confirm возвращает тот же result/key; edit после summary/confirm инвалидирует quote/confirmation и создаёт новую draft revision.
- Переключение в FAQ сохраняет cart/dialog state; после FAQ следующий order action продолжает workflow. Confirmation/cancel/contact capture должны быть deterministic, без LLM. Свободный ambiguous input приводит к clarification, не к mutation.
- Blocking owner decisions перед реализацией: разрешить optional/empty query для existing `search_menu` ради full-menu browse; определить `delivery_fee_minor` и его DEV configuration; подтвердить delivery/contact/requested-time semantics; подтвердить хранение PII в checkpoint и lifecycle очистки; подтвердить deterministic-only M5 parser.
- Полный analysis: `history/016_M5_analysis_report.md`. Tests в analysis task NOT RUN; Git/remote и DB diagnostics read-only PASS.

## M5 implementation handoff

- `app/orders` теперь содержит typed contracts, limited structured OpenAI intent extractor, pure cart operations, delivery/time/contact validation, read-only settings repository и orchestration через existing MCP client.
- `app/agent` хранит M5 draft в official checkpoint. Telegram `update_id` проходит в graph; cache/high-watermark защищают exact duplicate и older/out-of-order update, entries ограничены 32. HMAC derivation и same-thread lock не изменены.
- `search_menu.query` optional; empty browse возвращает до 20 available dishes deterministic order. Три tool names и M4 bounds/filters сохранены.
- Main development DB: Alembic `0002`, 15 DEV menu rows, `restaurant_settings.delivery_fee_minor=350`; `orders=0`, `order_items=0`, `calendar_jobs=0`. Setting — runtime data, не migration/default.
- M5 confirmation создаёт только immutable checkpoint snapshot с stable key. Exact user phrase обязательна; fresh MCP quote + fee сравниваются перед confirmation. M6 должен снова проверить и durable-save order.
- Automated: unit 113 PASS; full host 113 PASS/8 external skips; isolated Docker PostgreSQL integration 5 PASS; live structured OpenAI intent PASS; Docker frozen build, lock, health and startup log checks PASS. Итоговые команды и snapshot описаны в `history/017_M5_report.md`.
- M5 не READY до manual Telegram E2E владельца. Рекомендуемые сценарии: menu/search/detail; natural multi-item add; ambiguous selection; increment/set/decrement/remove/clear; FAQ во время cart и contact flow; invalid phone/address; vague/past and exact time; backend restart recovery; changed quote reconfirm; exact/repeated confirmation; cancel clears unconfirmed PII.
- До production остаются открыты checkpoint PII retention/pruning и distributed coordination; deployment должен оставаться single backend/poller. PERF-001 также открыт.
- Не начинать M6, не создавать business order/Calendar side effects и не выполнять commit/push без отдельного разрешения.

## M5 manual defects diagnostic handoff

- Manual Telegram E2E открыл DEF-M5-001…005; M5 переведён в **IN PROGRESS**. Fixes не реализовывались.
- Blocker DEF-M5-001 вызван unconditional immutable guard после `confirmed`, отсутствием `new_order` action и тем, что `/start` не проходит через AgentService. Для нескольких confirmed заказов без business order table требуется bounded checkpoint queue/archive; размер и `/start` semantics должен подтвердить владелец.
- DEF-M5-002 воспроизведён live read-only: extractor → `брускетты`, MCP search → zero; `рататуй` найден. Рекомендуется typed canonical `lookup_query` + existing substring search + PostgreSQL Russian FTS fallback, а approximate/ambiguous candidates всегда подтверждать. PostgreSQL diagnostic показал canonical query + Russian FTS для тестовых `брускетта`, `греческий салат`, `рататуй`, `креветка` — 4/4 match. Не добавлять dish-specific aliases.
- Batch add уже откладывает mutation до resolution всех items, но pending clarification теряет другие элементы multi-item proposal; fix должен хранить bounded pending batch и применять его атомарно только после всех уточнений.
- Time parser принимает документированные exact forms и корректно отклоняет vague/past; конкретный неудачный manual input не известен. Рекомендуется structured parse result, optional «в», ясные error reasons и DST-aware validation; решение по time-only input требует владельца.
- Vague FAQ «Как работает ресторан?» корректно route в FAQ и retrieve `faq-001`, но grounded answerer safe-fallback из-за семантической неоднозначности. Рекомендуется clarification, а не reinterpretation; изменение ADR-011 fallback semantics требует явного разрешения.
- Repeat confirm сохраняет existing snapshot/key по коду; новые tests должны доказать deep equality для duplicate update, same text/new update и restart.
- Диагностика: targeted 24 tests PASS; application code/schema/data unchanged. Полный отчет: `history/018_M5_defects_analysis_report.md`. M6 не начинать.

## M5 defect fixes handoff

- `new_order` поддерживает «Новый заказ», «Оформить еще один заказ», «Хочу заказать еще». Current confirmed snapshot валидируется и переносится в FIFO `confirmed_order_archive`; active draft очищается. Максимум пять archive/current snapshots на thread. `/start` не меняет checkpoint; cancel не удаляет confirmed snapshots.
- Для M6 FIFO handoff — `confirmed_order_archive` oldest-first, затем текущий `confirmed_order`. Confirmation keys и snapshot contents не переписываются. Durable `orders`/`order_items` по-прежнему отсутствуют.
- Resolver сохраняет original Russian form и canonical lookup query, использует substring/exact search и PostgreSQL Russian FTS. Full pending batch переживает несколько numbered clarifications; mutation происходит только после resolution всех items.
- Delivery UI: inline «Как можно скорее» и «Выбрать дату и время»; ручной ввод только `ДД.ММ.ГГГГ ЧЧ:ММ`, future `Europe/Madrid`. Calendar/time-slot picker и сложный NLP отложены.
- FAQ clarification разрешён ADR-011 только при relevant context + ambiguous meaning. Threshold и grounded fallback не ослаблены.
- Automated final: unit 128 PASS; host full 128 PASS/8 skips; isolated Docker full 136 PASS, включая real MCP stdio/PostgreSQL/checkpoint/Qdrant; live OpenAI morphology 4/4 и live FAQ clarification PASS; build/health/lock/diff PASS. Main DB invariants: `0/0/0/15/350` для orders/order_items/calendar_jobs/menu_items/delivery fee.
- M5 остаётся **IN PROGRESS** до owner manual Telegram E2E. Не начинать M6, не commit/push без разрешения.

## M5 manual E2E round 2 diagnostic handoff

- DEF-M5-006: `awaiting_confirmation` сейчас не перехватывает confirm/edit/cancel до LLM; «нет» отсутствует среди deterministic aliases и может route в FAQ. Рекомендуется stage-local mapping: «нет»/«изменить заказ» → edit, «отмена» → cancel, плюс inline buttons. Confirm должен продолжать использовать existing fresh quote/fingerprint logic.
- DEF-M5-007: для «Убери одну брускету» LLM предложил decrement с canonical `брускета`, но MCP/FTS не нашли dish. Dialog создал `dish_not_found` clarification без изменения cart; именно его Telegram send упал с `ConnectError`. Minimal typo support следует ограничить блюдами active cart и всегда требовать numbered confirmation — никогда не применять uncertain fuzzy match молча.
- DEF-M5-008: первый confirm durable-сохранился до сбоя `sendMessage`; повтор вернул тот же confirmed result. Replay/checkpoint logic менять без причины не следует. Нужны centralized safe send, bounded retry только для однозначно pre-delivery connect failures, PTB error handler и safe delivery telemetry; durable outbox/order side effects остаются вне M5.
- Recommended edit UX: после «Изменить» выбрать `Корзина / Имя / Телефон / Адрес / Время / Вернуться к итогу`; менять только выбранное поле, инвалидировать quote и после любой mutation формировать новую summary с explicit confirmation. Confirmed archive не затрагивается.
- Targeted existing regression: 29 PASS. Application code/schema/data не менялись в diagnostic task. Полный evidence и acceptance plan: `history/020_M5_round2_diagnostics_report.md`.
- M5 **IN PROGRESS**. Ждать owner approval на fixes; M6, commit и push запрещены.

## M5 round 2 fixes handoff

- Summary callbacks encode only `order:<action>:<draft_revision>`. Handler forwards them through `AgentService.handle_message_result`; same per-thread lock/checkpoint/replay path applies. `OrderDialog` rejects mismatched stage/revision before mutation and returns controls for current state.
- Edit stages: choice → cart or one validated contact/time field → fresh summary. Cart edits retain contacts and expose «Вернуться к итогу»; real mutation bumps revision. Cancel requires `cancel_yes`; `cancel_back` restores unchanged summary. Confirm retains fresh MCP quote/fee/fingerprint logic and price change now bumps revision before reconfirm.
- Stage-local text aliases: `Подтверждаю`/`подтвердить`, `нет`/`изменить заказ`, `отмена`/`отменить`; no LLM/FAQ at `awaiting_confirmation`. «нет» outside that stage remains ordinary classified text.
- Typo fallback is cart-only, strict and bounded. It never auto-selects: a close unique item becomes numbered `PendingBatch` clarification; weak/tied strings return no match. Menu-wide fuzzy/pg_trgm/aliases were not added.
- `safe_send_reply` is used by text, `/start`, callbacks. It retries only chained `httpx.ConnectError` once (two attempts total), never retries ambiguous timeout, never rolls back checkpoint, and logs no message/chat/PII/token. PTB error handler is registered. No durable outbox.
- Automated final: targeted 43 PASS; host 142 PASS/8 skips; isolated Docker 150 PASS; build/health/lock/diff/log/secret checks PASS. Development invariants remain `15/350/0/0/0`. Full report: `history/021_M5_round2_fixes_report.md`.
- M5 remains **IN PROGRESS** pending owner manual Telegram round 3. Do not start M6 or commit/push without separate approval.

## M5.2-A1 handoff

- `new_order`, `clear` и `cancel` при непустом unconfirmed cart теперь только создают optional typed `pending_destructive_action` с return stage и переходят в соответствующую confirmation stage. Cart, contacts, quote, revision и confirmed archive в этот момент не меняются.
- Telegram показывает revision-bound confirm/decline callbacks. Для нового заказа это `confirm_new_order` / `keep_current_order`; clear и cancel используют свои deterministic confirmations. Confirm вызывает существующую mutation ровно после актуального callback, decline восстанавливает return stage. Старые callbacks отклоняются, duplicate Telegram update обслуживается existing bounded replay cache.
- Confirmed state не считается active draft: штатный `new_order` после confirmation по-прежнему архивирует immutable current snapshot и открывает пустой draft. Legacy checkpoints без нового optional key читаются через `state.get()` и получают защиту при первом таком действии.
- Safe exact rules: «Заказ» → clarification без mutation; «Хочу оформить заказ» → checkout; «Покажи мой заказ» → view cart; «Новый заказ» при active draft → confirmation barrier.
- M5.2-A1 verification: targeted 52 PASS; all unit 151 PASS; host 151 PASS/8 external skips; compile/diff PASS. Host `uv lock --check` NOT RUN (`uv` отсутствует); lockfile не менялся. Integration/external services NOT RUN. ContextBuilder/history/new LLM contract/order status/catalog/M6 остаются вне A1 и относятся к отдельному M5.2-A2 либо будущим задачам.
- M5 всё ещё IN PROGRESS. Следующий шаг — owner manual Telegram acceptance A1; не commit/push и не начинать M6 без отдельного разрешения.

## M5.2-A1 confirmation session fix handoff

- `PendingDestructiveAction` теперь обязательно содержит 12-character URL-safe `confirmation_id`. Новый pending request всегда генерирует новый ID без изменения `draft_revision`.
- Destructive callback data имеет формат `order:<action>:<revision>:<confirmation_id>`; остальные callbacks сохраняют старый трехчастный формат. `AgentService` передает callback ID в optional state key и возвращает current pending ID для построения Telegram markup.
- `OrderDialog._validate_callback()` для `confirm_new_order`, `confirm_clear`, `keep_current_order`, `cancel_yes`, `cancel_back` требует совпадения action/stage/revision/session. Pending без session (legacy in-flight confirmation) не выполняется и безопасно возвращает `stale_callback`; старые checkpoints вне такой transient stage совместимы без migration.
- Текст «Начать новый заказ» / «Очистить корзину» / «Да, отменить» в confirmation stage не подтверждает mutation: destructive confirm/decline выполняются только callback-кнопкой.
- Test evidence: request1→decline→request2 с одинаковыми stage/revision отклоняет старую session и принимает новую для all three actions; duplicate update выполняется once; rebuilt graph + same saver восстанавливает pending session; targeted 61 PASS, unit/full host 160 PASS, 8 external skips.
- Не менялись MCP, DB/schema/config/dependencies. M5 remains IN PROGRESS; M5.2-A2/M6 не начинать, commit/push запрещены без отдельного разрешения.

## M5.2-A2 analysis handoff

- Read-only analysis выполнен на `main` / `db0fbca3cd86e9e3a0a0c16c6e5279b9430271a4`; local `origin/main` совпадал с HEAD. `docs/M5_2_DESIGN_HANDOFF.md` найден как untracked owner-provided design input и не редактировался.
- Current checkpoint не содержит conversation history: только последнее `user_message`, transient proposal/callback, order data и до 32 replay responses. Replay responses нельзя давать LLM: они могут содержать summary PII и не содержат user/callback semantics.
- Existing extractor получает только current raw message. Validated contacts/full checkpoint ему не передаются, но это не гарантирует отсутствие PII, если пользователь сам вводит имя/адрес/телефон в arbitrary free text. Regex-only redaction недостаточен.
- Минимальная граница A2: pure allowlist `ContextBuilder` в orders boundary, typed `ExpectedInput`, trusted public last-dish label/server-side ID, максимум 10 semantic events / 5 exchanges без raw text, discriminated `action/clarify/unsupported` outcome и stage-specific LLM action allowlist. Dish IDs/callback actions/state mutations остаются backend-only.
- Generic `pending_clarification` должен иметь отдельный short session, allowed resolutions, return stage и draft revision. Он не заменяет `PendingBatch`, не сосуществует с actionable A1 destructive pending и не использует будущий A4 summary session.
- Required current gaps: «Покажи салаты» при сборе имени сейчас может сохраниться как имя; «этого блюда» не имеет referent; retrospective phrase может ошибочно стать add; previous-order query не имеет truthful typed action; FAQ interleaving распознается узким heuristic.
- Вариант B A4 сохранен как future constraint: `summary_session_id` независим от `draft_revision`, A1 `confirmation_id` и A2 clarification session. A2 не реализует `ui_view`, CatalogController, summary session или images.
- Рекомендуемые increments: A2.1 contracts/context/history/typed extractor, затем A2.2 clarification/required scenarios/Telegram choices. До реализации владелец должен подтвердить PII policy, history bounds, clarification buttons, previous-order wording и formal A1 manual status.
- Fresh checks: targeted unit 68 PASS; all unit 160 PASS. PostgreSQL/MCP/Qdrant/Docker/live OpenAI/Telegram NOT RUN. Полный отчет: `history/025_M5_2_A2_analysis_report.md`.
- Следующий безопасный шаг — review/owner decisions и отдельный implementation prompt только для A2 increment. M5 остается IN PROGRESS; M6/M7 не начинать.

## M5.2-A2.1 implementation handoff

- `app/orders/context.py` теперь строит `OrderContext` только из explicit `ContextSource`: stage/expected input, cart counts, LLM action allowlist, typed recent events, trusted public dish label и confirmed counts. Full checkpoint, contacts, UUID/IDs, callback/session values, quote/snapshot bodies и replay responses не передаются.
- `LlmOrderIntentExtractor.extract(message, context)` возвращает strict `ActionOutcome | ClarifyOutcome | UnsupportedOutcome`. Callback-only/confirm/edit actions исключены из LLM allowlist; adapter создает legacy `OrderIntentProposal` только после schema, PII-output и stage allowlist validation.
- Contact stages полностью обходят contextual extractor. Exact actions и safe category/FAQ guards выполняются до field capture; «Покажи салаты» сохраняет `collecting_name`, а реальное имя после этого принимается штатно. Obvious-sensitive question на contact stage не отправляется в FAQ/LLM.
- Semantic history — до 10 JSON-safe FIFO events `{source, semantic, outcome, optional trusted dish_name}`. Raw text/response/PII/IDs отсутствуют. Graph добавляет event в тот же terminal checkpoint update, replay не добавляет duplicate; callbacks представлены смыслом без payload. New order reset удаляет старую transient history/referent.
- Existing A1 destructive sessions, replay, pending batch atomicity, cart/MCP bounds/pricing, confirmation and immutable archive unchanged. No migration/dependency/callback protocol/business rows/Calendar changes.
- Automated: compile PASS; A2.1 unit 12 PASS; all unit/full host 172 PASS, external integrations 8 SKIP because `TEST_*` URLs absent; changed-file Ruff and diff check PASS. Live OpenAI/Telegram NOT RUN by task. Real structured schema compatibility therefore remains owner acceptance risk.
- A2.2 retains persistent clarification, previous-order and stable referent resolution. A3/A4/A5/M6 remain forbidden without separate prompt. Full report: `history/026_M5_2_A2_1_report.md`.

## M5.2-A2.1 Live E2E preparation handoff

- Safe live startup не выполнялся. Configured `.env` указывает на non-test Compose PostgreSQL/Qdrant, а все `TEST_*` URLs отсутствуют. Использовать working database для acceptance запрещено; runtime business data не читались и не менялись.
- Docker CLI сейчас непригоден: `docker --version`, `docker compose version/config/ps` завершаются `snap-confine` capability error о недоступном `cap_dac_override`. До исправления этой host-проблемы Compose smoke невозможен.
- Config-only readiness: `.env` валидируется `Settings`; Telegram/OpenAI/HMAC/PostgreSQL/Qdrant values присутствуют без раскрытия значений; required Python packages установлены. `.env` permissions обнаружены как `0664` и должны быть ужесточены владельцем до live использования.
- Offline structured-output precheck PASS: `ContextualIntentResult` генерирует strict schema с discriminator и тремя variants; `ChatOpenAI.with_structured_output(..., method="json_schema", strict=True)` binding создается с synthetic key без network. Реальная OpenAI API compatibility остается NOT VERIFIED.
- Fresh diagnostic tests: `tests/unit/test_order_context.py` — 12 PASS; `test_order_dialog.py + test_agent_replay.py` — 46 PASS; `git diff --check` PASS. `pip check` NOT RUN (нет module `pip`), `uv lock --check` NOT RUN (`uv` отсутствует).
- Correct runtime entrypoint: `docker compose up --build -d backend` (dependencies start automatically) либо host `uv run uvicorn app.main:app --host 0.0.0.0 --port 8000`. MCP не запускается отдельным daemon: backend создает `python -m mcp_server` через stdio при menu operations.
- Перед E2E создать test-only environment, isolated Compose project/volumes, применить Alembic/checkpoint setup/menu seed/delivery setting только к test DB, исключить одновременный Telegram poller, затем проверить health/logs и manual scenarios. Не использовать `down -v` без отдельного решения об удалении test volumes.
- Матрица READY/BLOCKED/NOT VERIFIED, точные команды запуска/остановки, log runbook и Telegram checklist находятся в `history/027_M5_2_A2_1_live_e2e_preparation_report.md`. M5 остается IN PROGRESS; M6 не начинать.

## M5.2-A2.1 routing diagnostic handoff

- Pre-extractor FAQ для исследуемой фразы возможен в `collecting_*` из-за `_looks_like_faq()` (`?` + буква). В `idle`/`awaiting_confirmation` вызывается contextual extractor.
- Valid fake `ActionOutcome(view_cart)` проходит adapter/dialog; action есть во всех relevant allowlists. Offline: idle/order, awaiting_confirmation/order, collecting_name/FAQ with zero extractor calls.
- Предыдущий exact ответ на «Заказ» кодом не воспроизводится в contact stage, поэтому live route не доказан. Тот же fallback также возвращают FAQ, unknown и graph/agent failures.
- Proposed fix не реализован: cart-aware deterministic retrospective `view_cart` guard до contact FAQ heuristic; добавить graph sequence и route/error regressions.
- Application code/tests/schema/dependencies не менялись; 56 tests PASS через system Python. Отчет: `history/028_M5_2_A2_1_message_routing_diagnostics_report.md`. Не начинать fix/M6 и не commit/push без approval.

## M5.2-A2.1 targeted live diagnostic handoff

- Existing logs cannot establish the old event: no local logs, Docker CLI blocked by `snap-confine`, and prior records lacked outcome/route correlation.
- Temporary enum-only logs are installed: `llm_called`, `llm_result`, `llm_error`, `final_route`, `fallback_reason`. No message, prompt, free-form output, PII, IDs or secrets are recorded.
- Business behavior unchanged. Compile/diff PASS; 65 targeted tests PASS. Python 3.10 cannot collect existing logging tests (`datetime.UTC`); project 3.12 interpreter symlink is broken.
- Restart backend and repeat the target Telegram message once. Inspect only five diagnostic message names documented in `history/029_M5_2_A2_1_targeted_live_diagnostics_report.md`.
- Do not implement a fix or commit/push until the fresh diagnostic tuple is reviewed. Remove temporary instrumentation after diagnosis.

## M5.2-A2.1 OpenAI invalid-request handoff

- Live result proves failure inside `self._structured.ainvoke()` before outcome: `OpenAIInvalidRequestError` → fail-closed unknown → unsupported fallback.
- Runtime config: `gpt-4.1-mini`, default Chat Completions endpoint, `json_schema`, `strict=True`, discriminated `ContextualIntentResult`; locked SDK versions `langchain-openai 1.8.0`, `openai 3.52.0`, `langchain-core 1.8.1`.
- Static schema inspection found no conclusive strict-schema violation. Prior logs discarded API status/code/type/param, so concrete rejection is not yet known.
- Temporary safe fields now expose only `llm_http_status`, allowlisted `llm_api_code/type/param`; never message/body/request/response/IDs.
- Rebuild and repeat once; inspect only `Order intent extraction failed`. 65 targeted tests and diff PASS. Full report: `history/030_M5_2_A2_1_openai_invalid_request_diagnostics_report.md`.
- Do not change prompt/routing/business logic or commit/push before reviewing the API fields.

## M5.2-A2.1 Structured Outputs compatibility handoff

- Root cause confirmed at OpenAI response schema: actual bound+SDK schema for domain discriminated union contained rejected `oneOf/discriminator/default` and length/item/range keywords; live API returned HTTP 400 at `response_format`.
- Extractor now binds a flat constraint-free `ApiContextualIntentResult` under the same `gpt-4.1-mini` / `json_schema` / `strict=True` configuration.
- DTO fields are all required and objects have `additionalProperties=false`; nullable fields use supported `anyOf`. API DTO is immediately mapped into existing strict domain outcomes, preserving all bounds/action rules and fail-closed behavior.
- New regression extracts on-wire schema from the real binding via SDK conversion and checks forbidden keywords plus strict required properties. Domain revalidation rejects out-of-range values.
- Targeted 67 PASS; context 14 PASS; Ruff/compile/diff PASS. Full host unit unavailable because project Python 3.12 interpreter is missing and system 3.10 lacks required packages/features.
- Rebuild backend and send the target Telegram phrase once; expect no invalid-request record and structured result. Full report: `history/031_M5_2_A2_1_structured_outputs_compatibility_fix_report.md`. No commit/push.

## M5.2-A2.1 ValidationError diagnostic handoff

- Live quantity-change request reached LLM but raised Pydantic `ValidationError` before outcome; previous record lacks field/rule.
- Current adapter accepts synthetic `set_quantity` quantity 2. Null quantity fails root `value_error` from domain model validator; quantity 21 fails `items.0.quantity/less_than_equal`.
- Temporary log field `llm_validation_errors` contains only bounded `{loc,type}`; Pydantic input/ctx/url/message and user/LLM data are excluded.
- Targeted 67 PASS; Ruff/compile/diff PASS. Rebuild, repeat once, and inspect only `Order intent extraction failed`.
- No functional fix until loc/type confirms the exact rule. Report: `history/032_M5_2_A2_1_validation_error_diagnostics_report.md`. No commit/push.

## M5.2-A2.1 bounded validation recovery handoff

- Prior root ValidationError is most consistent with a model-level `ActionOutcome` rule, but the old run had no pre-domain marker, so API parse vs domain conversion and the payload/rule are not proven. Safe pre-domain diagnostics now emit typed kind/action, items count, aggregate quantity flags and a shared fixed rule code only.
- Shared rule classifier is also used by the existing domain validator, preserving behavior/messages and avoiding diagnostic drift.
- Extractor retries at most once only for four repairable parsed-DTO rules: missing items, wrong single-item count, missing set quantity, missing search selector. Retry sends the same safe context/message plus only `VALIDATION_RULE`; never the invalid payload.
- Result is revalidated through domain contracts, output PII guard and stage allowlist. API/network errors, quantity bounds and second failures do not retry and fail closed. No side effect occurs before success; replay remains exactly once.
- Tests: 22 context PASS, 77 affected PASS, 95 expanded PASS; Ruff/compile/diff PASS. One live Telegram test remains. Report: `history/033_M5_2_A2_1_validation_recovery_report.md`. No commit/push.

## M5.2-A2.1 DTO normalization/debug handoff

- Confirmed live failure was valid `add` plus irrelevant selectors. Adapter now normalizes by action before domain validation: item payload vs menu selectors vs parameterless payload. It never rewrites action/items/quantity or fills missing values.
- Irrelevant fields no longer trigger validation/retry. Required data, quantity bounds, unknown/contradictory DTO, stage allowlist and PII remain fail closed; bounded retry still handles only true missing/count rules.
- Raw DTO PII gate runs before normalization. API field descriptions and prompt document field ownership plus add increment vs set final quantity without changing model/strict schema.
- `LLM_DEBUG_RESPONSE_JSON` is off by default, effective only in development/dev/local, and logs max-4000 parsed redacted DTO per attempt. Production is forced off; prompts/raw responses/user text/PII/secrets are excluded.
- Tests: 31 context PASS; 113 expanded affected PASS; Ruff/compile/diff PASS. Live acceptance pending. Report: `history/034_M5_2_A2_1_dto_normalization_debug_report.md`. No commit/push.

## M5.2-A2.2 multi-action handoff

- OpenAI API contract теперь имеет backward-compatible single `kind=action` и отдельный flat `kind=actions` с ordered `operations`; bound schema остается strict-compatible, без API discriminated union/unsupported constraints. Multi-action domain scope: 2–8 операций `add`, `set_quantity`, `decrement`, `remove`; остальные actions остаются single.
- `safe_extract()` возвращает `OrderActionPlan` только после validation каждой operation, raw DTO/output PII guards и current-stage allowlist. Existing bounded repair retry не расширен: только прежние fixed missing/count rules, максимум два LLM calls.
- Dialog разрешает все items authoritative menu/MCP path до mutation. Pending ambiguity хранит весь plan и nested resolved IDs. Unknown/ambiguous/unavailable/limit/conflict result не идет в FAQ и не меняет cart. Cross-operation повтор одной dish отклоняется; unchanged/unmentioned dishes сохраняются.
- Valid plan применяется ordered к local candidate cart и публикуется одним terminal graph update; single actions, A1 destructive confirmation, replay, FAQ, MCP contracts, DB schema, Telegram callback/UI и OpenAI model не изменены. Semantic history записывает `cart_batch` без raw text.
- Persistence fault injection подтверждает old-or-complete terminal state, никогда промежуточную operation. Official LangGraph pending write может восстановить весь batch после final `aput` failure; strict rollback-to-old требует отдельной checkpointer transaction architecture и не входит в A2.2.
- Verification: targeted 89 PASS; available host unit subset 161 PASS; Ruff/format/compile/diff PASS. Full suite collection blocked system Python 3.10/missing locked packages; Docker CLI по-прежнему blocked `snap-confine`. Live OpenAI/Telegram NOT RUN. Report/snapshot: `history/035_M5_2_A2_2_report.md` / `history/035_M5_2_A2_2_snapshot.zip`.
- Manual next step after rebuild: prepare cart with salmon ×2, lemonade ×3, salad ×1; send «Лосося оставь одну порцию вместо двух, а лимонадов сделай четыре. Салат не трогай» once. Expect salmon ×1, lemonade ×4, salad ×1 and no FAQ fallback. Do not start M6 or commit/push automatically.

## M5.2-A2.2 quantity validation handoff

- Root cause: recognized `quantity_out_of_range` was diagnostic-only. Adapter still constructed bounded domain items, `ValidationError(less_than_equal)` escaped extractor, and `safe_extract()` returned unknown without clarification; graph selected unknown/FAQ fallback.
- `api_result_to_outcome()` now returns typed backend `ValidationOutcome(quantity_out_of_range)` only when a parsed single/multi item explicitly exceeds 20. Raw DTO PII guard runs first. No API schema/prompt/model change and no retry.
- `safe_extract()` maps that outcome to an order clarification with exact safe message. Because clarification is present, `OrderDialog.classify()` selects `order`; handle returns response/error only, so original cart/revision remain unchanged and MCP is not called.
- Domain `ProposedItem.quantity ge=1/le=20` remains unchanged. Quantity 20 PASS; 21/25 return specific response. Below-minimum and unrelated Pydantic errors keep generic fail-closed behavior.
- Verification: affected 94 PASS; available unit subset 166 PASS; Ruff/format/compile/diff PASS. Live retry pending. Report/snapshot: `history/036_M5_2_A2_2_quantity_validation_report.md` / `history/036_M5_2_A2_2_quantity_validation_snapshot.zip`. No commit/push.

## M5.2-A2.2 acceptance closure / next increment handoff

- Owner-confirmed live Telegram: Test 10 two `set_quantity` PASS, cart total 48,50 EUR; Test 11 `add+remove+add` with unknown Margherita PASS, cart unchanged; Test 12 `remove+add+set_quantity` with untouched lemonade PASS, cart total 96,00 EUR; Test 13 conflict on salmon PASS by bot response only, cart was not separately rechecked; Test 14 quantity >20 PASS after `ValidationOutcome`, cart unchanged and order-specific response.
- A2.2 status is now **LIVE ACCEPTED**, including quantity validation closure. Do not infer additional live verification beyond the five statements above.
- Overall M5 remains **IN PROGRESS**. M6 was not started. A3/A4/A5 were not started.
- A2 design review: generic `pending_clarification` **NOT IMPLEMENTED**; previous dish referent **PARTIAL** (trusted public label/history, no backend referent ID/resolver); previous-order questions **PARTIAL** (snapshot facts/count only, no typed action/handler); checkout context **PARTIAL** (stage/expected input/contact preservation, no suspension/navigation context); FAQ/order state preservation **IMPLEMENTED for current text flow**; `expected_input` **PARTIAL** (stage-derived only).
- Code/test evidence: `ClarifyOutcome` is immediately mapped to one-turn unknown clarification; `AgentState` has no pending clarification. `ContextBuilder` includes stage-derived expected input, bounded semantic history, last public dish label and confirmed snapshot facts. `test_retrospective_clarification_does_not_mutate_cart`, `test_contact_navigation_and_faq_never_reach_contextual_extractor`, `test_faq_during_checkout_preserves_order_state`, semantic-history/replay and new-order reset tests cover current boundaries. No regression exists for «этого блюда» or truthful previous-order response.
- Recommended next increment: **M5.2-A2.3 Semantic Continuation Closure** before A3. Scope only typed pending clarification/continuation/reset, backend-only dish referent with safe LLM label, truthful checkpoint-bounded previous-order query and required regressions. Do not mix A3 catalog/UI, A4 summary/checkout suspension, A5 images or M6 persistence.
- Dependency rationale: A3 will add `ui_view`/navigation context; A4 depends on it for checkout suspension and separately requires summary session variant B. Stable semantic continuation/referent state should be established first so clarification, catalog view and summary sessions remain independent from `draft_revision` and A1 `confirmation_id`.
- Known persistence limit unchanged: final saver failure may recover old state or the entire terminal pending write, never a per-operation partial batch; guaranteed rollback-to-old requires separate checkpointer transaction architecture.
- Documentation-only task: no application/tests/dependencies/schema changes; tests NOT RUN; `git diff --check` PASS. Report/snapshot: `history/037_M5_2_A2_2_acceptance_closure_report.md` / `history/037_M5_2_A2_2_acceptance_closure_snapshot.zip`. No commit/push.

## M5.2-A2.3 semantic continuation handoff

- Implemented typed optional `pending_clarification` in official checkpoint state. It contains only fixed reason, bounded allowlisted choices, return stage and 12-character session ID. `OrderContext.expected_input` becomes `clarification_choice`, and LLM actions are restricted to stored choices.
- Mandatory «Заказ» continuation now supports Telegram buttons and free text. Callback reuses existing revision/session envelope and is protected by replay/stale checks. Topic switch/FAQ/cancel/new order clear semantic pending; A1 destructive confirmation remains higher priority.
- Stable `last_referenced_dish_id` is backend-only and is set after catalog resolution. Details/allergens use fresh `get_dish`; missing referent asks for a dish name and then reuses existing dish ambiguity resolver.
- `previous_order` handles the three agreed phrases deterministically and is also in the contextual action allowlist. It reports only confirmed checkpoint facts without contacts and explicitly denies knowledge of placement/preparation/delivery before M6.
- No DB schema, dependencies, MCP contracts, OpenAI model, cart bounds, A1 confirmations or A2.2 atomic execution changes. A3/A4/A5/M6 not started; M5 remains IN PROGRESS.
- Verification: changed-file Ruff PASS; affected unit/regression 118 PASS; compile PASS. Full host suite cannot collect under available Python 3.10/missing dependencies; project Python 3.12 venv symlink is broken. Docker, PostgreSQL/MCP integration, live OpenAI and Telegram NOT RUN.
- Next: rebuild/fix Python 3.12 runtime, run full host suite, then one short live acceptance sequence for clarification callback/free text, referent, previous-order truthfulness and A1/A2.2 smoke. No commit/push.

## M5.2-A2.3 deterministic checkout analysis handoff

- Owner accepted a new boundary: every ordinary text update in `collecting_name/phone/address/time` and `collecting_edit_*` is only the expected field value. No contextual extractor, FAQ embeddings/answerer or other LLM call is permitted. Only allowlisted Telegram callbacks can control checkout.
- Current root cause is code-confirmed: name validator accepts the all-letter continuation phrase; question heuristic then routes the next phone-stage question into FAQ; a later phrase fails phone validation. Existing tests explicitly require FAQ/navigation during checkout and must be reversed under ADR-024.
- Current PII boundary is partly sound: `ContextBuilder` excludes contacts, raw state, responses/replay and snapshot bodies; semantic history stores only `contact_input`. But FAQ currently receives checkout questions, and checkpoint versions retain raw `user_message`, contacts and PII-bearing summary/replay responses. The latter is storage/retention, not an observed LLM path.
- Minimal implementation plan: contact-stage routing gate before all text classifiers; empty contact-stage LLM allowlist; callback-only `cancel_checkout`; checkpointed checkout session ID reused in the existing four-part callback envelope; validate action + stage + revision + session; cancellation preserves cart, clears all unconfirmed contacts/quote, returns idle, and is distinct from destructive A1 cancel.
- Existing phone/time validation, edit buttons/return-to-summary, explicit confirm, replay/per-thread locking, A1 barriers and A2.2 atomicity remain. Name/address validators need deterministic Unicode/control-character hardening only; no semantic name/address verification.
- Conflicts to update at implementation: architecture contact guards, `M5_2_DESIGN_HANDOFF` FAQ/navigation checkout rows and acceptance #7, ADR-021 wording, plus old FAQ/navigation checkout tests. Do not start A3/A4/A5/M6.
- Analysis evidence: 2 focused existing tests PASS; probe confirms the continuation phrase is currently accepted as name, free text rejected as phone, and newline currently collapsed into an address. No application/test/schema/dependency changes. Await owner approval of the plan before implementation; no commit/push.

## M5.2-A2.3 deterministic checkout acceptance handoff

**Статус:** **ACCEPTED (LIVE acceptance)**, 2026-11-19.

- ADR-024 реализован. В `collecting_name/phone/address/time` и `collecting_edit_*` ordinary text рассматривается только как ожидаемое поле. Contact gate расположен до deterministic intent, pending continuation, FAQ и contextual extractor; contact-stage LLM allowlist пуст.
- Новый checkout один раз показывает согласованное PII notice, затем existing sequence. Edit warning не повторяет. Unicode NFC validators поддерживают international names/addresses; phone ограничен ASCII 7–15 digits с optional leading `+`; semantic verification не выполняется.
- Telegram показывает session-bound «Отменить оформление» на всех восьми contact stages; time buttons используют тот же checkout session. Cancel сохраняет cart, очищает contacts/quote/pending/session, возвращает idle и не вызывает A1 destructive cancellation.
- `checkout_session_id` — optional 12-character JSON-safe checkpoint field. Callback validation требует current stage + revision + exact session; old/reentered/wrong-session callbacks stale, duplicate update replay-safe, current session переживает restart. Legacy checkpoint text flow совместим; legacy sessionless checkout callback fail safe.
- PII не проецируется в `OrderContext`, semantic history, FAQ/embedding или subsequent LLM prompt. Regression с synthetic sentinels проверяет extractor context и logs. Official checkpoint historical versions все еще могут содержать raw turns/contacts/summary; retention/pruning зарегистрирован как отдельная privacy-задача.
- Automated verification: changed-file Ruff PASS; affected unit/regression 166 PASS; compile PASS. Full test suite was not run in the Codex environment because the available Python 3.10 runtime and installed dependencies do not match the project runtime.
- Owner-confirmed LIVE PASS: name/phone/address validation; deterministic text handling; notice; summary and name edit; cancellation with preserved cart; checkout re-entry; stale callback; restart recovery; cancel after recovery. All 29 observed diagnostic records had `llm_called=false` and `final_route=order`.
- Acceptance limits: cancellation directly from contact edit mode was not separately verified; these diagnostic records do not prove absence of every possible external call; historical PostgreSQL checkpoint PII retention remains a separate production task.
- No schema/MCP/model/dependency/topology/cart/A1/A2.2 changes. M5 remains IN PROGRESS; A3/A4/A5/M6 have not started. A2.3 is closed and the project is ready for separate planning of the next increment. No commit/push.

## M5.2-A3.1 read-only MCP catalog handoff

**Актуальное решение:** A3.1 ACCEPTED 2026-11-20. Нижеследующие записи сохраняют исторический контекст до closure. D04 исправил protocol fake, D05 сделал category обязательной и непустой. Ранее: targeted 20 PASS, PostgreSQL stdio 1 PASS, full unit 270 PASS (включая OrderDialog 98), lint/scoped format/compile/lock/diff PASS. Приняты риски: 16 baseline format замечаний как technical debt; full regression NOT RUN как residual verification risk; внешние CI/branch protection неизвестны, проверить перед merge/deployment при необходимости. См. `history/047_M5_2_A3_1_owner_acceptance_closure_report.md`.

**Статус:** **IMPLEMENTED / TARGETED TESTED / OWNER ACCEPTANCE NOT RUN**, 2026-11-20.

- Existing read-only chain now exposes six tools: `search_menu`, `list_categories`, `browse_menu`, `get_dish`, `inspect_cart`, `calculate_cart`. New server/client structured DTOs are in `app/menu/contracts.py`; no migration or dependency change.
- Categories use the existing string as both ID/name, exclude categories without available dishes and include current available count. Browse is category-only, available-only, SQL paginated with default 5/max 20 and stable `lower(name), UUID` ordering; catalog snapshots are intentionally absent.
- Cart inspection accepts an external bounded array and does one batch lookup. It preserves each input line/order, reports known unavailable with current price, reports missing with null prices, excludes both from `available_subtotal_minor`, and has no `AgentState`, quote, checkout, or mutation semantics. Existing EUR and cart bounds remain authoritative.
- Compatibility boundary: existing `search_menu`, `get_dish`, `calculate_cart` code paths and response models remain intact. Order/graph/Telegram/checkout/confirmed snapshot code was not touched. A3.2+ and M6 were not started.
- Verification available on host: menu service 13 PASS; changed-scope Ruff/format, compile and diff checks PASS. MCP client/protocol and integration commands FAIL during collection because only Python 3.10 is executable (`typing.Self` unavailable), the project 3.12 venv target is missing, and MCP dependencies are unavailable to system Python. The test-only PostgreSQL body and Docker are NOT RUN; Docker CLI fails in `snap-confine`. Re-run in restored locked/test-only runtime before owner acceptance.
- Decision record: ADR-025. Mandatory report/snapshot: `history/043_M5_2_A3_1_readonly_mcp_catalog_report.md` and matching ZIP. Next work requires separate approval; no commit/push.

## M5.2-A3.1 environment recovery handoff

**Статус:** **PARTIALLY VERIFIED / OWNER ACCEPTANCE NOT RUN**, 2026-11-20.

- Изолированный CPython 3.12.12 и frozen environment из `uv.lock` восстановлены в `/tmp`; MCP SDK 2.0.0 доступен. Existing broken `.venv` оставлен без изменений. Docker по-прежнему блокируется host `snap-confine`.
- Отдельный PostgreSQL 17 `C.UTF-8` cluster и test-only database позволили выполнить real stdio MCP integration: **1 PASS**; migration 0001/0002, synthetic fixtures, cleanup и process exit прошли. Production DB не использовалась.
- MCP client **4 PASS**, MenuService **13 PASS**, OrderDialog **98 PASS**, full unit **269 PASS / 1 FAIL**. Единственный FAIL локализован в `test_mcp_protocol.py`: fake service получает raw whitespace и возвращает dish, тогда как production `MenuService` нормализует category и real integration возвращает empty page. Это reproducible **TEST DEFECT, LOW**, не application defect.
- Discovery публикует шесть tools. `list_categories` не требует аргументов; `inspect_cart.items` допускает empty array; browse bounds/default корректны. `browse_menu.category` остается optional в DTO/schema. Missing/empty/whitespace/unknown category fail closed пустой страницей и не раскрывает весь catalog; это **CONTRACT DEVIATION, LOW**, требующее owner decision о required schema.
- Ruff check/format, compileall, frozen lock check и diff check PASS. Код/контракты/тесты в recovery-задании не менялись. Следующий шаг до acceptance: отдельно разрешить исправление fake service/test и принять решение по required/optional category, затем повторить protocol suite. Отчет: `history/044_M5_2_A3_1_environment_recovery_verification_report.md`. A3.2 и M6 не начинать.
