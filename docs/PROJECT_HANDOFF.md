# Project Handoff — контекст для нового чата

## Что это

Учебный Restaurant AI Agent. M1–M4 приняты. M5 cart/confirmation, DEF-M5-001–008 и M5.2-A1 active-draft protection с unique confirmation session реализованы, automated acceptance PASS, но статус READY ожидает ручного Telegram E2E владельца. Владелец ранее подтвердил M3.2 E2E. Исходные DOCX остаются в ChatGPT Sources.

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
