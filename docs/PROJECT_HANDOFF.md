# Project Handoff — контекст для нового чата

## Что это

Учебный Restaurant AI Agent. M1, M2, M3.1 и M3.2 приняты. Владелец настроил постоянный `THREAD_ID_HMAC_SECRET`, запустил backend и подтвердил ручной Telegram E2E M3.2 без изменения поведения M3.1. Исходные DOCX остаются в ChatGPT Sources.

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

1. M3.2 закрыт как READY; дополнительных действий по acceptance не требуется.
2. Следующий этап — M4 MCP Tools, NOT STARTED. Начинать только по отдельному заданию.
3. PERF-001 перед оптимизацией требует измерений; MCP/orders/Calendar остаются вне M3.2.
