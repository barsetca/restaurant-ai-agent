# Project Handoff — контекст для нового чата

## Что это

Учебный Restaurant AI Agent. M1, M2, M3.1, M3.2 и M4 приняты. M4 добавил отдельный read-only MCP stdio server/client и deterministic menu/cart subtotal tools без подключения к LangGraph. Владелец ранее подтвердил ручной Telegram E2E M3.2. Исходные DOCX остаются в ChatGPT Sources.

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

Владелец выполнил commit и push реализации M1–M3.2. Текущий commit ветки `main` — `b087c31`; M4 implementation и последующие documentation updates остаются незакоммиченными. Историческое предупреждение `history/011_HANDOFF_report.md` о 56 entries не является актуальным снимком; перед дальнейшей работой всегда проверять фактический `git status`.
