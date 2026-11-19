# Restaurant AI Agent — M5.2 Design Handoff

**Дата подготовки:** 2026-11-19  
**Назначение:** передача согласованного UX и архитектурного замысла M5.2 в новый чат ChatGPT / Codex.  
**Статус документа:** design handoff, **не** отчет о реализации и **не** разрешение начинать все перечисленные задачи.  
**Основа:** актуальные проектные документы и `024_M5_2_A1_confirmation_fix_snapshot(1).zip`, плюс решения владельца, согласованные в диалоге.  
**Приоритет:** явные решения владельца → `docs/DECISIONS.md` → `docs/ORIGINAL_REQUIREMENTS.md` → архитектурные документы → этот design handoff. При расхождении с фактическим кодом выявить и зафиксировать расхождение, не подменять молча.

## 1. Цель и границы

M5.2 делает Telegram-заказ контекстно понятным и безопасным, объединяет просмотр меню и выбор блюд в один интерфейс, позволяет свободно переходить между каталогом, корзиной, FAQ и оформлением, не теряя draft. LLM интерпретирует свободный текст, но **не** владеет корзиной, ценами, навигационными callback или необратимыми переходами.

**Важное разграничение:** M5.2-A1 уже реализована; M5.2-A2 (контекст и семантика) и последующие UI-инкременты здесь **спроектированы**, но в приложенном snapshot не реализованы. M5 остается IN PROGRESS до необходимой приемки. M6 (business orders и Calendar), M7 (Sheets Menu/Settings sync), production outbox, distributed locking — вне M5.2. Нельзя сообщать пользователю, что заказ передан ресторану, пока не реализована соответствующая M6 persistence.

**Superseding implementation note (ADR-024, 2026-11-19):** A2.3 deterministic checkout boundary реализована. Во всех `collecting_*` contact stages ordinary text является только значением ожидаемого поля; FAQ, embeddings, contextual LLM, semantic continuation и text navigation там запрещены. Старые строки ниже про FAQ/каталог во время contact collection являются историческим дизайном будущего checkout suspension и не описывают текущую реализацию. Для выхода используется callback «Отменить оформление», который сохраняет cart и очищает unconfirmed contacts/quote/session.

## 2. Подтвержденный baseline реализации

- Python 3.12, FastAPI, `python-telegram-bot`, LangGraph, official PostgreSQL `AsyncPostgresSaver`, OpenAI, Qdrant FAQ, локальный stdio MCP, PostgreSQL, SQLAlchemy/Alembic.
- `AgentState` — `TypedDict(total=False)` с JSON-safe cart, `order_stage`, contacts, quote, revision, pending batch, pending destructive action, confirmed snapshots, replay metadata. Отдельной таблицы корзины нет.
- `OrderDialog` — deterministic business state machine; существующий `OrderIntentProposal` содержит action/items/query/category/dietary tags/confidence/clarification; это **старый контракт**, не новый outcome union.
- MCP `search_menu`, `get_dish`, `calculate_cart` — read-only источник меню и суммы позиций; доставка — read-only `restaurant_settings.delivery_fee_minor`. EUR integer minor units. Лимиты: 20 distinct cart lines, 20 единиц на блюдо, 100 единиц суммарно; поиск до 20 позиций.
- `draft_revision` относится к изменению бизнес-данных. Telegram callbacks сейчас в основном `order:<action>:<revision>`.
- M5.2-A1: при непустом активном неподтвержденном draft `new_order`, `clear`, `cancel` требуют отдельной кнопки. Каждый запрос получает новый `confirmation_id` (12 URL-safe chars); destructive callback `order:<action>:<revision>:<confirmation_id>` проверяется по action/stage/revision/session. Отказ не меняет cart/contacts/quote; старые sessions fail safe. Текст не заменяет destructive callback.
- `/start` не очищает draft. Подтвержденные snapshots immutable; current + archive — максимум 5 на thread. После M5 confirm только checkpoint snapshot, не `orders` row.
- Последний зафиксированный отчет: 160 host tests PASS, 8 external integration skips; A1 manual acceptance в документах еще отмечена как pending. Владелец позднее сообщил «будем считать, что проверки прошли»; перед формальным READY нужно согласованно обновить статус документации, не выдавая неисполненные external tests за PASS.
- Snapshot показывает, что полноценного `CatalogController`, `ui_view`, `conversation_context`, `ContextBuilder`, outcome `action/clarify/unsupported` и image handling пока нет.

## 3. Неизменяемые архитектурные инварианты

1. **PostgreSQL/MCP — источник истины для меню, доступности и цен.** LLM не придумывает dish ID, allergens, цены или итог.
2. **Checkpoint — источник истины для текущего draft и диалога.** Telegram markup — отображение, а не хранилище корзины.
3. **Навигация не меняет бизнес-данные.** Просмотр категории, карточки, FAQ и переходы назад не повышают `draft_revision`.
4. **Бизнес-изменение** (cart/contacts/time) повышает `draft_revision`, инвалидирует quote и требует новой summary перед confirm.
5. **Выход из summary инвалидирует ее кнопки независимо от изменения draft** — выбранный владельцем вариант B. Для этого нужен отдельный от `draft_revision` механизм актуальности UI/summary.
6. **Деструктивные действия** над непустым active draft проходят A1 barrier с session ID. Новая UI-логика не должна обходить этот barrier.
7. **Старые Telegram кнопки не могут мутировать state.** Replay `update_id`, per-thread lock и серверные проверки callback сохраняются.
8. **Confirmed snapshots immutable.** Новый выбор после confirmed начинает новый draft, не меняет старый snapshot и не переписывает confirmation key.
9. **FAQ/вопрос посреди заказа не интерпретируется как поле контакта.** Отвечать на вопрос и продолжать прежний сценарий.
10. **PII не передавать LLM как часть ContextBuilder и не логировать.** Хранение валидированных contacts в checkpoint уже согласовано, но retention остается открытым.

## 4. Единый пользовательский каталог — согласованное UX-решение

**Одна кнопка «Меню»** вместо отдельных «Меню» и «Выбор блюд». Не существует отдельного режима «только посмотреть» или кнопки «Начать заказ». Пользователь может изучать меню и добавлять позиции в любой момент. Первое добавление автоматически создает/активирует неподтвержденный draft. `/start` — безопасный вход, без очистки.

### 4.1 Экраны

| Экран | Содержимое | Действия |
|---|---|---|
| Home | Краткое приветствие, «Меню»; пользователь может задавать вопросы текстом | Открыть категории; корзина при наличии |
| Categories | Закуски, салаты, основные блюда, десерты, напитки | Открыть категорию; назад |
| Category list | Название, цена, текущее количество каждой позиции | Открыть карточку; `+` / `−`; пагинация; назад |
| Dish detail | Фото при наличии, описание, ингредиенты, аллергены, цена, доступность | `+` / `−`; назад в прежнюю категорию/страницу; корзина |
| Cart | Состав, количества, рассчитанные цены/суммы | Изменить количество, удалить, продолжить выбор, оформление, очистить/отменить с A1 guard |
| Checkout | Недостающие имя, телефон, адрес, время | Продолжить, открыть меню/корзину, задать вопрос |
| Summary | Полный состав, доставка, итог, contacts/time | Подтвердить, изменить, отменить; любой выход инвалидирует текущую summary |

«Корзина» доступна на экранах каталога **только если cart непустая**. «Продолжить выбор» из корзины возвращает на последнюю страницу каталога (category/page/dish). Для большого меню — небольшие страницы, ориентир ~5 блюд, без жесткого требования менять MCP `search_menu` limit на 5. Не показывать unavailable блюдо как доступное к добавлению; его информационное отображение и поведение фильтра уточнить на UI design pass.

### 4.2 Навигация

- `dish detail → category list (тот же page) → categories → home`.
- `catalog → cart → продолжить выбор` восстанавливает прежний каталог.
- Каталог, корзина и checkout не являются взаимоисключающими бизнес-режимами.
- `ui_view`/`navigation_context` независимы от `order_stage`. Не использовать `order_stage` как единственный идентификатор экрана.
- `CatalogController` — **предлагаемая** отдельная orchestration-компонента для view/category/page/back; не должна дублировать cart/quote/domain logic `OrderDialog`.
- Кнопки навигации не вызывают LLM; свободный текст проходит state-aware intent path.

## 5. Независимые измерения состояния

Предлагаемая модель (имена уточнить после анализа кода):

```text
order_stage          — бизнес-этап draft (idle/contacts/edit/confirmation/confirmed/...)
ui_view              — текущий экран (home/categories/list/detail/cart/checkout/summary/...)
dialog_context       — last_action, expected_input, last_referenced_dish_id, recent_interactions
navigation_context   — category, page, dish_id, return_view
checkout_suspended   — признак временного выхода из checkout, без потери заполненных полей
pending_clarification — ограниченное уточнение, ожидающее явного ответа
summary_session      — текущая уникальная сессия summary (отдельно от destructive confirmation_id)
```

Все значения должны быть bounded, JSON-safe и совместимыми со старыми checkpoints через optional state keys/default handling. Имена и точные typed contracts — предмет первого implementation design review, а не утверждение о существующем коде.

**Приоритет state transitions:** replay/stale validation → pending destructive barrier → deterministic callback/expected-input guards → context-aware classification свободного текста → deterministic domain mutation → state/UI response. Переходы не должны случайно сохранять вопрос «Покажи салаты» как имя клиента.

## 6. M5.2-A2 — ContextBuilder и интерпретация свободного текста

### 6.1 Bounded sanitized context

Строить контекст **из существующего checkpoint**, а не из полного Telegram history и не из произвольных сообщений пользователя. Передавать в LLM только необходимое:

- `order_stage`, `ui_view`, ожидаемый тип ввода (`expected_input`);
- есть ли активная корзина, количество позиций/единиц, при необходимости названия **публичных** блюд и их количества;
- `last_action`, `last_referenced_dish_id`/публичное название, активная категория;
- последние ~3–5 содержательных exchanges (ориентир до 10 turns), включая смысл нажатых кнопок;
- allowlist допустимых действий в текущем контексте.

**Исключить из LLM prompt:** телефон, адрес, имя клиента, raw Telegram/chat IDs, HMAC secrets, full checkpoint, полные confirmed snapshots, технические credentials, произвольные длинные логи. Историю ограничить по числу событий и длине строк; PII redaction до передачи. Публичные названия блюд допустимы. Не хранить необработанные свободные сообщения бесконечно. При новом заказе очистить устаревшие ссылки на блюда и навигационный контекст, не удаляя immutable confirmed archive.

### 6.2 Structured outcome

Заменить/обернуть старый single-proposal контракт типизированным outcome (точная Pydantic-схема после design review):

- `action` — допустимое действие и строго типизированные аргументы;
- `clarify` — безопасный короткий вопрос с allowlisted вариантами ответа;
- `unsupported` — запрос не поддержан или смысл нельзя надежно определить.

Python проверяет outcome, текущую stage, action allowlist, bounds и необходимые параметры. LLM **не** задает произвольные callback actions; кнопки строит backend. `pending_clarification` хранится в checkpoint; callback ответа на уточнение разрешается deterministic без LLM, свободный текст интерпретируется с учетом pending state. Сбрасывать pending при разрешении, cancel и явной смене контекста. Не полагаться только на confidence score.

### 6.3 Обязательная семантика

| Сообщение | Ожидаемое поведение |
|---|---|
| «Заказ» при непустой корзине | Уточнить: показать корзину или перейти к оформлению; не очищать |
| «Хочу оформить заказ» | Продолжить checkout текущего draft |
| «Покажи мой заказ» | Показать cart/current draft, не создать новый |
| «Новый заказ» при active draft | A1 destructive confirmation, не немедленный reset |
| «Я добавлял брускетту и лимонад» | Распознать как сообщение о прошлом/состоянии; не добавлять повторно без явного намерения |
| «Что с предыдущим заказом?» | Отличить вопрос о ранее подтвержденном заказе от команды добавить; отвечать только в пределах реально сохраненного checkpoint и возможностей M5; не выдумывать статус доставки/размещения |
| «Покажи салаты» при сборе имени | **SUPERSEDED ADR-024:** валидировать только как имя; не вызывать каталог/LLM |
| «Какие аллергены у этого блюда?» после карточки | Использовать `last_referenced_dish_id` и MCP details; при неоднозначности уточнить |
| FAQ-вопрос в checkout | **SUPERSEDED ADR-024:** валидировать только как ожидаемое поле; FAQ доступен после callback-отмены checkout |
| «Отмена» при active draft | A1 destructive confirmation, если действие уничтожает непустой draft |

Детерминированные правила для однозначных команд и валидаторов телефона/времени остаются первыми. Не отправлять очевидные button actions в LLM. При сомнении уточнять; не мутировать cart по прошедшему времени или предположению.

## 7. Checkout, вопросы и возврат

**Текущая A2.3 boundary:** initial/edit contact collection полностью детерминирован. Один раз перед первым полем показывается уведомление о сборе контактов и возможности редактирования. На каждой contact stage доступна session-bound кнопка «Отменить оформление»; она сохраняет cart, очищает contacts/quote/checkout session и возвращает `idle`. Следующие пункты про уход в FAQ/каталог относятся к будущим A3/A4 `checkout_suspended`, не к текущей реализации.

- Сбор `name → phone → address → requested_at` остается последовательным для недостающих полей; `requested_at=None` означает ASAP, точный ввод времени в существующем формате `ДД.ММ.ГГГГ ЧЧ:ММ`, Europe/Madrid.
- При уходе в каталог/корзину/FAQ сохранить уже введенные поля и ожидаемый шаг. Возврат продолжает с первого недостающего поля, а не запускает checkout заново.
- Изменение одного поля не стирает другие. Изменение cart/contact/time инвалидирует quote и требует нового итога.
- Информационный вопрос сам по себе не меняет `order_stage` и `draft_revision`.
- Если пользователь просматривает каталог, `checkout_suspended`/аналог помогает отличить временный уход от отказа; это навигационная семантика, не новый заказ.
- После confirmation confirmed snapshot immutable. Добавление блюда из меню должно начинать новый draft с соблюдением существующего лимита confirmed snapshots и правил архива; не мутировать старый snapshot.

## 8. Итоговая сводка: выбран **вариант B**

**Твердое решение владельца:** *любой выход из итоговой сводки инвалидирует старые кнопки, при возврате формируем новую сводку.*

### 8.1 Правила

1. При входе в `summary` создать новый opaque `summary_session_id` (или монотонный `summary_view_revision` при корректной атомарной реализации). Он независим от `draft_revision` и A1 `confirmation_id`.
2. Кнопки `confirm/edit/cancel` данной сводки привязаны к конкретной summary session и текущему draft revision. Размер callback ≤64 bytes; никакого PII.
3. **Любой фактический выход из summary** (каталог, корзина, карточка, FAQ, edit, back и т.п.) закрывает ее session, даже если корзина, contacts, время и цены не изменились. Старые кнопки возвращают `stale_callback` и не меняют state.
4. Возврат на итог всегда строит **новую** summary и **новую** session. Это не требует искусственно повышать `draft_revision`.
5. Даже для текущей session `confirm` повторно проверяет актуальность меню, availability, delivery fee и quote. Если изменились цена/доступность/итог, подтверждение блокируется, пользователь получает актуальный итог и должен подтвердить его заново.
6. Если пользователь нажал старую кнопку `confirm` после ухода из summary, **никакого подтверждения**. Аналогично устаревшие `edit/cancel` не должны применяться как действия старой summary.
7. При повторном Telegram update действует существующий replay guard; при новых updates с тем же старым callback — server-side session validation. Нельзя опираться только на удаление inline keyboard из Telegram сообщения.
8. Переход из summary в A1 destructive confirmation должен инвалидировать summary session; возвращение после отказа создает новую summary, не оживляет прежнюю.

**Критически важно:** `draft_revision` отражает бизнес-данные, `summary_session_id` — актуальность конкретного экрана. Не смешивать их и не менять draft revision ради навигации.

### 8.2 Пограничные сценарии

- Summary → меню → назад без изменения cart: новая summary, старый `confirm` stale.
- Summary → FAQ → возврат: новая summary, даже если ответ FAQ ничего не изменил.
- Summary → edit → отказ от изменения → возврат: новая summary.
- Summary → cart quantity change → возврат: новая summary и повышенная `draft_revision`.
- Два последовательных summary с одинаковой `draft_revision`: только кнопки **последней** session действительны.
- После restart backend текущая session восстанавливается из official checkpoint; старые session IDs не становятся актуальными.

## 9. Telegram callbacks и консистентность UI

- Все callbacks проходят `AgentService`, тот же per-thread lock, checkpoint/replay и domain validation. Никаких прямых mutations в Telegram handler.
- Для UI предложить компактный allowlisted callback protocol с `view/session revision` и `draft_revision` там, где действие меняет корзину. Для summary использовать отдельный session token. Сохранить совместимость/безопасное отклонение legacy callbacks.
- `+`/`−` доступны в списке и карточке; количество на кнопках и отображении берется из server-side cart. При повторном/устаревшем callback нельзя выполнить двойное изменение.
- Разделить `draft_revision` (изменения заказа) и `view_revision`/session (актуальность отображения). Переходы по каталогу не инвалидируют quote сами по себе, но могут инвалидировать старую markup-сессию.
- При быстрых последовательных нажатиях предпочесть безопасное stale/refresh вместо двойного применения; UI должен ясно предложить обновленное состояние.
- По возможности редактировать одно активное сообщение каталога; переход текст ↔ фото может потребовать новое сообщение. Старые кнопки должны стать логически неактуальными даже если Telegram edit/delete не удался.
- Telegram API send/edit failure **не откатывает** уже persisted checkpoint mutation. Сохранять safe sender policy: retry только для однозначного pre-delivery connect error, не повторять ambiguous timeout.

## 10. Фотографии блюд

**Согласованный минимальный дизайн:** optional `menu_items.image_path`, относительный путь вроде `dishes/bruschetta.webp`, локальная директория `static/dishes/` (путь/volume проверить с реальным Docker setup). Фотографии могут отсутствовать; карточка тогда текстовая.

- Путь должен быть относительным, без `..`, absolute paths, traversal и выхода за разрешенный media root; разрешенные расширения/типы валидировать.
- Изображения хранятся вне Git; Docker volume/read-only mount и наличие файлов требуют отдельной проверки. Не переносить binary image в checkpoint или LLM prompt.
- MCP `get_dish` должен отдавать только безопасную ссылку/relative path, а Telegram adapter отправлять фото и caption/keyboard, fallback на текст при отсутствии/ошибке.
- **Не считать поддержку WebP как Telegram `send_photo` доказанной** до маленького live integration test; при несовместимости выбрать JPEG/PNG или конверсию. Не вводить отдельную таблицу изображений для MVP.
- Изменение DB schema — отдельная Alembic migration; будущий M7 Sheets Menu sync должен явно согласовать `image_path` как источник/правило обработки, не менять его молча.

## 11. Рекомендуемая декомпозиция (не автоматическое разрешение реализации)

| Инкремент | Scope | Что должно быть доказано |
|---|---|---|
| **A2** | ContextBuilder, bounded sanitized history, expected_input, structured outcome, semantic clarification, retrospective/previous-order handling | Контакты не попадают в LLM; no unintended mutation; regression M5/A1 |
| **A3** | `ui_view`/navigation context, unified catalog, categories/list/detail, cart `+`/`−`, back/return | Callback-only navigation без LLM; state/cart сохранены; stale/replay guarded |
| **A4** | Checkout suspension, FAQ interleaving, независимый summary session по варианту B | Возврат без потери полей; старые summary buttons stale даже при неизменном draft |
| **A5** | Optional images, Alembic `image_path`, media adapter/volume, fallback | Safe path; фото/текст fallback; isolated tests; live media format probe |
| **Acceptance** | End-to-end Telegram, restart, MCP/PostgreSQL integration, regression, документация | Владелец подтверждает UX; только после этого обсуждать M5 READY и M6 |

Порядок A3/A4 можно скорректировать после dependency analysis, но вариант B и безопасность старых callback обязательны. Не объединять все инкременты в один огромный Codex prompt.

## 12. Acceptance matrix — обязательные сценарии

1. `/start` с непустым draft → корзина сохранена.
2. Открыть меню → категории → список → карточка → назад; order state не меняется.
3. Добавить через `+` в списке и карточке; количество и стоимость совпадают с MCP/cart.
4. Уменьшить `−`, включая переход 1→0; повтор старой кнопки не уменьшает повторно.
5. Cart → продолжить выбор → та же категория/page/dish.
6. FAQ из каталога → ответ и возврат в каталог.
7. FAQ-подобный текст во время сбора телефона → **ADR-024:** FAQ не вызывается; выполняется phone validation, поле остается ожидаемым.
8. Введены имя/телефон → уход в меню → возврат → поля сохранены, продолжение с адреса.
9. «Заказ», «Хочу оформить заказ», «Покажи мой заказ» дают разные корректные результаты без очистки.
10. «Я добавлял…» не выполняет повторный add.
11. «Что с предыдущим заказом?» не создает новый order и не выдумывает статус.
12. «Какие аллергены у этого блюда?» после карточки — MCP details; без referent — clarification.
13. Новый заказ/очистка/отмена с active draft → только A1 session-bound confirmation; старый prompt не действует.
14. Summary → меню → назад без mutation → **новая** summary; старая кнопка confirm stale.
15. Summary → FAQ → возврат → **новая** summary; старая кнопка edit/cancel stale.
16. Summary → изменить quantity/contact/time → fresh quote и новая summary.
17. Price/availability/delivery fee изменены между summary и confirm → old quote не подтверждается; безопасный пересчет и reconfirm.
18. Два summary с одинаковым draft revision → действует только последняя session.
19. Restart между summary и callback → актуальная session восстанавливается, старые остаются stale.
20. Duplicate/out-of-order Telegram updates → cart/confirm mutation не повторяются.
21. Confirmed snapshot immutable; новое добавление начинает отдельный draft, лимит archive соблюдается.
22. Изображение отсутствует/повреждено/путь невалиден → текстовая карточка без утечки filesystem paths.
23. `orders`, `order_items`, `calendar_jobs` до M6 не изменяются.
24. В logs, LLM prompt и callback data нет имени/телефона/адреса/секретов; checkpoint PII policy соблюдена.

Тесты: unit + fake adapters, official saver/restart и PostgreSQL/MCP **только на isolated test DB**, Telegram manual owner E2E; фиксировать PASS/FAIL/SKIP/NOT RUN честно. Рабочую БД не использовать для destructive test.

## 13. Открытые вопросы, которые нельзя решать молча

1. Точный typed schema/длина `recent_interactions`, правила PII-redaction и источник last-action metadata.
2. `ui_view` enum, Telegram callback encoding и политика инвалидирования old catalog keyboards, в особенности быстрых повторных `+`.
3. Переход к следующему draft сразу после confirmed: как совместить автостарт при первом `+` с лимитом 5 snapshots и существующим `new_order` transition; требуется deterministic, не дублирующее архивирование решение.
4. Отображать ли unavailable dishes в каталоге как информационные карточки: текущий `search_menu` возвращает только available; возможно потребуется отдельный read-only контракт. Не расширять MCP без решения.
5. Фото: формат, volume, image_path schema, источник media в будущем M7.
6. Формальная фиксация ручной приемки A1 и M5.2 после тестов; текущий статус docs может отставать от устного решения владельца.
7. `order_status` для предыдущих заказов: M5 может описать только наличие confirmed checkpoint snapshot, не business/Calendar/delivery status.
8. Retention/pruning checkpoint PII, multi-instance locking и durable outbox — отдельно, вне M5.2.

## 14. Инструкция новому Codex

Перед первым заданием:

1. Прочитать `docs/CODEX_RULES.md`, `docs/DEVELOPMENT_STATUS.md`, `docs/ORIGINAL_REQUIREMENTS.md`, `docs/ARCHITECTURE.md`, `docs/DATABASE_ARCHITECTURE.md`, `docs/DECISIONS.md`, `docs/PROJECT_HANDOFF.md` и **этот файл**.
2. Проверить `git status` и HEAD. Владелец сообщил, что Git commit уже выполнен; не полагаться на устаревшие строки handoff про незакоммиченные M4/M5 changes.
3. Сверить действующие state/intent/callback contracts с кодом и зафиксировать найденные расхождения. Не считать design-допущения реализованными.
4. Выполнять только отдельно порученный инкремент; не начинать M6/M7, не менять production/dev data без разрешения.
5. После задания обновить документацию и отчет по `CODEX_RULES.md`, безопасный snapshot при изменении проекта; commit/push только с явным разрешением.

**Рекомендуемое первое задание:** read-only gap analysis для A2 и план минимальных изменений с тестами; затем отдельное разрешение на реализацию. Этот handoff сам по себе не является implementation prompt.
