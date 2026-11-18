from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import datetime
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from app.agent.graph import build_agent_graph
from app.agent.service import AgentService
from app.menu.contracts import CartLine, CartQuote, DishDetail, DishSummary, MenuSearchResult
from app.orders.cart import CartOperationError, add_items
from app.orders.contracts import CartStateItem, OrderIntentProposal
from app.orders.dialog import CONFIRMED_MESSAGE, OrderDialog, _parse_requested_at
from app.orders.intent import deterministic_intent, safe_extract
from app.orders.settings import OrderSettingsError


def dish(name: str, price: int = 1000, *, available: bool = True) -> DishDetail:
    return DishDetail(
        dish_id=uuid4(),
        name=name,
        category="Тест",
        dietary_tags=[],
        price_minor=price,
        currency="EUR",
        description="Синтетическое блюдо",
        ingredients=["ингредиент"],
        allergens=[],
        available=available,
    )


class FakeMenu:
    def __init__(self, dishes: list[DishDetail]) -> None:
        self.dishes = {item.dish_id: item for item in dishes}

    async def search_menu(self, query=None, category=None, dietary_tags=None):
        del category, dietary_tags
        values = [item for item in self.dishes.values() if item.available]
        if query:
            values = [item for item in values if query.casefold() in item.name.casefold()]
        return MenuSearchResult(
            items=[DishSummary(**item.model_dump(exclude={"description", "ingredients", "allergens", "available"})) for item in values]
        )

    async def get_dish(self, dish_id):
        return self.dishes[dish_id]

    async def calculate_cart(self, items):
        lines = []
        for item in items:
            current = self.dishes[item.dish_id]
            if not current.available:
                raise RuntimeError("unavailable")
            lines.append(
                CartLine(
                    dish_id=item.dish_id,
                    name=current.name,
                    quantity=item.quantity,
                    unit_price_minor=current.price_minor,
                    line_total_minor=current.price_minor * item.quantity,
                )
            )
        return CartQuote(items=lines, subtotal_minor=sum(line.line_total_minor for line in lines))


class FakeSettings:
    def __init__(self, fee: int = 350) -> None:
        self.fee = fee

    async def get_delivery_fee_minor(self):
        if self.fee < 0:
            raise OrderSettingsError
        return self.fee


def dialog(menu: FakeMenu, settings: FakeSettings | None = None) -> OrderDialog:
    @asynccontextmanager
    async def factory():
        yield menu

    return OrderDialog(
        factory,
        settings or FakeSettings(),
        None,
        now=lambda: datetime(2026, 11, 18, 12, 0, tzinfo=ZoneInfo("Europe/Madrid")),
    )


def apply(state, changes):
    return {**state, **changes}


@pytest.mark.asyncio
async def test_multi_item_add_is_atomic_and_repeated_add_increments() -> None:
    first, second = dish("Брускетта"), dish("Греческий салат", 900)
    order = dialog(FakeMenu([first, second]))
    state = {
        "order_stage": "idle",
        "cart": [],
        "draft_revision": 0,
        "action_proposal": OrderIntentProposal(
            action="add",
            items=[{"name": "Брускетта", "quantity": 2}, {"name": "Греческий салат", "quantity": 1}],
        ).model_dump(mode="json"),
    }

    state = apply(state, await order.handle(state))
    assert [item["quantity"] for item in state["cart"]] == [2, 1]
    state["action_proposal"] = OrderIntentProposal(
        action="add", items=[{"name": "Брускетта", "quantity": 1}]
    ).model_dump(mode="json")
    state = apply(state, await order.handle(state))
    assert state["cart"][0]["quantity"] == 3
    assert state["draft_revision"] == 2


@pytest.mark.asyncio
async def test_ambiguous_dish_requires_number_and_does_not_mutate_first() -> None:
    order = dialog(FakeMenu([dish("Салат греческий"), dish("Салат теплый")]))
    state = {
        "order_stage": "idle",
        "cart": [],
        "draft_revision": 0,
        "action_proposal": OrderIntentProposal(
            action="add", items=[{"name": "салат", "quantity": 1}]
        ).model_dump(mode="json"),
    }
    state = apply(state, await order.handle(state))
    assert state["cart"] == []
    assert state["order_stage"] == "awaiting_disambiguation"
    assert len(state["candidates"]) == 2
    state["user_message"] = "2"
    state = apply(state, await order.handle(state))
    assert len(state["cart"]) == 1
    assert state["order_stage"] == "idle"


@pytest.mark.asyncio
async def test_delivery_flow_quote_confirm_and_confirm_is_idempotent() -> None:
    current = dish("Рататуй", 1350)
    order = dialog(FakeMenu([current]))
    state = {
        "order_stage": "idle",
        "cart": [{"dish_id": str(current.dish_id), "quantity": 2}],
        "contacts": {},
        "draft_revision": 1,
        "action_proposal": {"action": "checkout"},
    }
    for message, expected_stage in (
        (None, "collecting_name"),
        ("Иван Тестов", "collecting_phone"),
        ("+34 600 000 001", "collecting_address"),
        ("Calle Sintética 10", "collecting_time"),
        ("как можно скорее", "awaiting_confirmation"),
    ):
        if message is not None:
            state["user_message"] = message
        state = apply(state, await order.handle(state))
        assert state["order_stage"] == expected_stage
    assert state["quote_snapshot"]["subtotal_minor"] == 2700
    assert state["quote_snapshot"]["delivery_fee_minor"] == 350
    assert state["quote_snapshot"]["total_minor"] == 3050
    assert "+34600000001" in state["response"]

    state["user_message"] = "Подтверждаю"
    state["action_proposal"] = {"action": "confirm"}
    state = apply(state, await order.handle(state))
    confirmed_order = deepcopy(state["confirmed_order"])
    assert state["response"] == CONFIRMED_MESSAGE
    state = apply(state, await order.handle(state))
    assert state["confirmed_order"] == confirmed_order


@pytest.mark.asyncio
async def test_price_change_requires_a_new_confirmation() -> None:
    current = dish("Рататуй", 1350)
    order = dialog(FakeMenu([current]))
    contacts = {
        "customer_name": "Иван Тестов",
        "phone": "+34600000001",
        "address": "Calle Sintética 10",
        "requested_at": None,
    }
    state = {
        "order_stage": "idle",
        "cart": [{"dish_id": str(current.dish_id), "quantity": 1}],
        "contacts": contacts,
        "draft_revision": 2,
    }
    state = apply(state, await order._prepare_quote(state, type("C", (), contacts)()))
    current.price_minor = 1450
    state.update(user_message="Подтверждаю", action_proposal={"action": "confirm"})
    state = apply(state, await order.handle(state))
    assert state["order_stage"] == "awaiting_confirmation"
    assert state.get("confirmed_order") is None
    assert state["draft_revision"] == 3
    assert "изменились" in state["response"]


@pytest.mark.asyncio
async def test_missing_delivery_setting_blocks_quote_safely() -> None:
    current = dish("Рататуй")
    order = dialog(FakeMenu([current]), FakeSettings(-1))
    state = {
        "order_stage": "collecting_time",
        "cart": [{"dish_id": str(current.dish_id), "quantity": 1}],
        "contacts": {"customer_name": "Иван", "phone": "+34600000001", "address": "Test 10"},
        "draft_revision": 0,
        "user_message": "как можно скорее",
    }
    result = await order.handle(state)
    assert result["error_code"] == "order_settings_unavailable"
    assert "временно недоступно" in result["response"]


@pytest.mark.asyncio
async def test_cancel_guard_during_contact_clears_unconfirmed_pii() -> None:
    current = dish("Рататуй")
    order = dialog(FakeMenu([current]))
    state = {
        "order_stage": "collecting_address",
        "cart": [{"dish_id": str(current.dish_id), "quantity": 1}],
        "contacts": {"customer_name": "Иван", "phone": "+34600000001"},
        "draft_revision": 2,
        "action_proposal": deterministic_intent("отмена").model_dump(mode="json"),
    }
    prompted = await order.handle(state)
    assert prompted["order_stage"] == "awaiting_cancel_confirmation"
    assert "cart" not in prompted
    assert "contacts" not in prompted
    assert state["cart"] != []
    assert state["contacts"] != {}

    pending_state = apply(state, prompted)
    pending_state.update(
        action_proposal={"action": "cancel_yes"},
        callback_action="cancel_yes",
        callback_revision=2,
        callback_confirmation_id=prompted["pending_destructive_action"]["confirmation_id"],
    )
    result = await order.handle(pending_state)
    assert result["cart"] == []
    assert result["contacts"] == {}
    assert result["order_stage"] == "idle"


def test_cart_and_requested_time_bounds() -> None:
    item = CartStateItem(dish_id=uuid4(), quantity=20)
    with pytest.raises(CartOperationError):
        add_items([item], [CartStateItem(dish_id=item.dish_id, quantity=1)])
    now = datetime(2026, 11, 18, 12, 0, tzinfo=ZoneInfo("Europe/Madrid"))
    assert _parse_requested_at("завтра вечером", now) == (False, None)
    assert _parse_requested_at("2026-11-17 19:00", now) == (False, None)
    assert _parse_requested_at("завтра 19:30", now) == (False, None)
    assert _parse_requested_at("19.11.2026 19:30", now)[1] == "2026-11-19T19:30:00+01:00"
    assert _parse_requested_at("31.02.2027 19:30", now) == (False, None)


@pytest.mark.asyncio
async def test_deterministic_guards_and_safe_llm_failure() -> None:
    assert deterministic_intent("Подтверждаю!").action == "confirm"
    for phrase in ("Новый заказ", "Оформить еще один заказ", "Хочу заказать ещё"):
        assert deterministic_intent(phrase).action == "new_order"

    class BrokenExtractor:
        async def extract(self, message):
            del message
            raise RuntimeError("synthetic secret")

    proposal = await safe_extract(BrokenExtractor(), "естественный запрос")
    assert proposal.action == "unknown"
    assert proposal.confidence == "low"


@pytest.mark.asyncio
async def test_confirmation_requires_exact_phrase_even_when_intent_is_confirm() -> None:
    order = dialog(FakeMenu([]))
    result = await order.handle(
        {
            "order_stage": "awaiting_confirmation",
            "user_message": "Подтверждаю!",
            "action_proposal": {"action": "confirm"},
        }
    )
    assert result["error_code"] == "explicit_confirmation_required"


def confirmed_snapshot(key: str) -> dict[str, object]:
    return {
        "items": [],
        "subtotal_minor": 1000,
        "delivery_fee_minor": 350,
        "total_minor": 1350,
        "currency": "EUR",
        "draft_revision": 1,
        "fingerprint": "a" * 64,
        "customer_name": "Иван Тестов",
        "phone": "+34600000001",
        "address": "Calle Sintética 10",
        "requested_at": None,
        "confirmation_key": key,
        "confirmed_at": "2026-11-18T12:00:00+00:00",
    }


@pytest.mark.asyncio
async def test_new_order_archives_confirmed_snapshot_and_start_is_non_destructive() -> None:
    order = dialog(FakeMenu([]))
    current = confirmed_snapshot("first")
    state = {
        "order_stage": "confirmed",
        "confirmed_order": current,
        "confirmed_order_archive": [],
        "cart": [],
        "contacts": {},
        "draft_revision": 3,
        "action_proposal": {"action": "start"},
    }
    start_result = await order.handle(state)
    assert "сохранен" in start_result["response"]
    assert "confirmed_order" not in start_result

    state["action_proposal"] = {"action": "new_order"}
    result = await order.handle(state)
    assert result["confirmed_order"] is None
    assert result["confirmed_order_archive"] == [current]
    assert result["cart"] == []
    assert result["contacts"] == {}
    assert result["order_stage"] == "idle"


@pytest.mark.asyncio
async def test_new_order_with_active_draft_requires_revision_bound_confirmation() -> None:
    current = dish("Брускетта")
    order = dialog(FakeMenu([current]))
    original_cart = [{"dish_id": str(current.dish_id), "quantity": 2}]
    original_contacts = {"customer_name": "Иван", "phone": "+34600000001"}
    state = {
        "order_stage": "collecting_address",
        "cart": deepcopy(original_cart),
        "contacts": deepcopy(original_contacts),
        "draft_revision": 4,
        "action_proposal": {"action": "new_order"},
    }

    prompted = await order.handle(state)
    assert prompted["order_stage"] == "awaiting_new_order_confirmation"
    assert prompted["pending_destructive_action"]["action"] == "new_order"
    assert prompted["pending_destructive_action"]["return_stage"] == "collecting_address"
    confirmation_id = prompted["pending_destructive_action"]["confirmation_id"]
    assert len(confirmation_id) == 12
    assert "cart" not in prompted
    assert "contacts" not in prompted
    assert "draft_revision" not in prompted
    assert state["cart"] == original_cart
    assert state["contacts"] == original_contacts

    pending_state = apply(state, prompted)
    pending_state.update(
        action_proposal={"action": "confirm_new_order"},
        callback_action="confirm_new_order",
        callback_revision=4,
        callback_confirmation_id=confirmation_id,
    )
    confirmed = await order.handle(pending_state)
    assert confirmed["cart"] == []
    assert confirmed["contacts"] == {}
    assert confirmed["pending_destructive_action"] is None
    assert confirmed["draft_revision"] == 5
    assert confirmed["order_stage"] == "idle"


@pytest.mark.asyncio
async def test_keep_current_order_preserves_active_draft_and_contacts() -> None:
    current = dish("Лимонад")
    order = dialog(FakeMenu([current]))
    original_cart = [{"dish_id": str(current.dish_id), "quantity": 1}]
    original_contacts = {
        "customer_name": "Иван",
        "phone": "+34600000001",
        "address": "Test 10",
    }
    state = {
        "order_stage": "awaiting_new_order_confirmation",
        "cart": deepcopy(original_cart),
        "contacts": deepcopy(original_contacts),
        "draft_revision": 7,
        "pending_destructive_action": {
            "action": "new_order",
            "return_stage": "collecting_time",
            "confirmation_id": "session00001",
        },
        "action_proposal": {"action": "keep_current_order"},
        "callback_action": "keep_current_order",
        "callback_revision": 7,
        "callback_confirmation_id": "session00001",
    }

    result = await order.handle(state)
    assert result["order_stage"] == "collecting_time"
    assert result["pending_destructive_action"] is None
    assert "cart" not in result
    assert "contacts" not in result
    assert "draft_revision" not in result
    assert state["cart"] == original_cart
    assert state["contacts"] == original_contacts


@pytest.mark.asyncio
async def test_stale_new_order_callback_cannot_clear_active_draft() -> None:
    current = dish("Рататуй")
    order = dialog(FakeMenu([current]))
    original_cart = [{"dish_id": str(current.dish_id), "quantity": 1}]
    result = await order.handle(
        {
            "order_stage": "awaiting_new_order_confirmation",
            "cart": original_cart,
            "contacts": {"customer_name": "Иван"},
            "draft_revision": 9,
            "pending_destructive_action": {
                "action": "new_order",
                "return_stage": "idle",
                "confirmation_id": "session00001",
            },
            "action_proposal": {"action": "confirm_new_order"},
            "callback_action": "confirm_new_order",
            "callback_revision": 8,
            "callback_confirmation_id": "session00001",
        }
    )
    assert result["error_code"] == "stale_callback"
    assert "cart" not in result
    assert original_cart[0]["quantity"] == 1


@pytest.mark.asyncio
async def test_safe_order_phrases_are_deterministic_with_active_cart() -> None:
    current = dish("Рататуй")
    order = dialog(FakeMenu([current]))
    base = {
        "order_stage": "idle",
        "cart": [{"dish_id": str(current.dish_id), "quantity": 1}],
        "draft_revision": 1,
    }

    route, proposal = await order.classify({**base, "user_message": "Заказ"})
    assert route == "order"
    assert proposal["action"] == "unknown"
    assert "показать текущую корзину" in proposal["clarification"]

    route, proposal = await order.classify(
        {**base, "user_message": "Хочу оформить заказ"}
    )
    assert route == "order"
    assert proposal["action"] == "checkout"

    route, proposal = await order.classify({**base, "user_message": "Покажи мой заказ"})
    assert route == "order"
    assert proposal["action"] == "view_cart"


@pytest.mark.asyncio
async def test_new_order_with_empty_or_legacy_draft_state_is_safe() -> None:
    order = dialog(FakeMenu([]))
    empty = await order.handle(
        {
            "order_stage": "idle",
            "cart": [],
            "contacts": {},
            "draft_revision": 2,
            "action_proposal": {"action": "new_order"},
        }
    )
    assert empty["order_stage"] == "idle"
    assert empty["cart"] == []
    assert empty["draft_revision"] == 3
    assert empty["pending_destructive_action"] is None

    # Simulates a checkpoint written before pending_destructive_action existed.
    current = dish("Салат")
    legacy_state = {
        "order_stage": "idle",
        "cart": [{"dish_id": str(current.dish_id), "quantity": 1}],
        "contacts": {},
        "draft_revision": 3,
        "action_proposal": {"action": "new_order"},
    }
    protected = await order.handle(legacy_state)
    assert protected["order_stage"] == "awaiting_new_order_confirmation"
    assert "cart" not in protected
    assert legacy_state["cart"][0]["quantity"] == 1


@pytest.mark.asyncio
async def test_clear_with_active_cart_requires_confirmation_and_can_be_declined() -> None:
    current = dish("Салат")
    order = dialog(FakeMenu([current]))
    original_cart = [{"dish_id": str(current.dish_id), "quantity": 2}]
    state = {
        "order_stage": "idle",
        "cart": deepcopy(original_cart),
        "contacts": {"customer_name": "Иван"},
        "draft_revision": 4,
        "action_proposal": {"action": "clear"},
    }
    prompted = await order.handle(state)
    assert prompted["order_stage"] == "awaiting_clear_confirmation"
    assert "cart" not in prompted

    declined_state = apply(state, prompted)
    declined_state.update(
        action_proposal={"action": "keep_current_order"},
        callback_action="keep_current_order",
        callback_revision=4,
        callback_confirmation_id=prompted["pending_destructive_action"]["confirmation_id"],
    )
    declined = await order.handle(declined_state)
    assert declined["order_stage"] == "idle"
    assert "cart" not in declined
    assert state["cart"] == original_cart

    confirmed_state = apply(state, prompted)
    confirmed_state.update(
        action_proposal={"action": "confirm_clear"},
        callback_action="confirm_clear",
        callback_revision=4,
        callback_confirmation_id=prompted["pending_destructive_action"]["confirmation_id"],
    )
    confirmed = await order.handle(confirmed_state)
    assert confirmed["cart"] == []
    assert confirmed["contacts"] == {}
    assert confirmed["draft_revision"] == 5


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("request_action", "confirmation_stage", "decline_action", "confirm_action"),
    [
        (
            "new_order",
            "awaiting_new_order_confirmation",
            "keep_current_order",
            "confirm_new_order",
        ),
        (
            "clear",
            "awaiting_clear_confirmation",
            "keep_current_order",
            "confirm_clear",
        ),
        ("cancel", "awaiting_cancel_confirmation", "cancel_back", "cancel_yes"),
    ],
)
async def test_old_destructive_session_cannot_confirm_repeated_request(
    monkeypatch,
    request_action: str,
    confirmation_stage: str,
    decline_action: str,
    confirm_action: str,
) -> None:
    confirmation_ids = iter(["session00001", "session00002"])
    monkeypatch.setattr(
        "app.orders.dialog.token_urlsafe",
        lambda length: next(confirmation_ids) if length == 9 else "",
    )
    current = dish("Рататуй")
    order = dialog(FakeMenu([current]))
    original_cart = [{"dish_id": str(current.dish_id), "quantity": 2}]
    base = {
        "order_stage": "idle",
        "cart": deepcopy(original_cart),
        "contacts": {"customer_name": "Иван"},
        "draft_revision": 5,
        "action_proposal": {"action": request_action},
    }

    first = await order.handle(base)
    assert first["order_stage"] == confirmation_stage
    assert first["pending_destructive_action"]["confirmation_id"] == "session00001"

    first_pending = apply(base, first)
    declined = await order.handle(
        {
            **first_pending,
            "action_proposal": {"action": decline_action},
            "callback_action": decline_action,
            "callback_revision": 5,
            "callback_confirmation_id": "session00001",
        }
    )
    assert declined["pending_destructive_action"] is None
    assert "cart" not in declined

    second_base = apply(first_pending, declined)
    second_base.update(
        action_proposal={"action": request_action},
        callback_action=None,
        callback_revision=None,
        callback_confirmation_id=None,
    )
    second = await order.handle(second_base)
    assert second["order_stage"] == confirmation_stage
    assert second["pending_destructive_action"]["confirmation_id"] == "session00002"

    second_pending = apply(second_base, second)
    stale = await order.handle(
        {
            **second_pending,
            "action_proposal": {"action": confirm_action},
            "callback_action": confirm_action,
            "callback_revision": 5,
            "callback_confirmation_id": "session00001",
        }
    )
    assert stale["error_code"] == "stale_callback"
    assert "cart" not in stale
    assert second_pending["cart"] == original_cart

    confirmed = await order.handle(
        {
            **second_pending,
            "action_proposal": {"action": confirm_action},
            "callback_action": confirm_action,
            "callback_revision": 5,
            "callback_confirmation_id": "session00002",
        }
    )
    assert confirmed["cart"] == []
    assert confirmed["pending_destructive_action"] is None
    assert confirmed["draft_revision"] == 6


@pytest.mark.asyncio
async def test_destructive_confirmation_survives_dialog_restart() -> None:
    current = dish("Рататуй")
    menu = FakeMenu([current])
    saver = InMemorySaver()
    before_restart = build_agent_graph(
        None,
        checkpointer=saver,
        order_dialog=dialog(menu),
    )
    config = {"configurable": {"thread_id": "restart-destructive"}}
    await before_restart.aupdate_state(
        config,
        {
            "order_stage": "idle",
            "cart": [{"dish_id": str(current.dish_id), "quantity": 1}],
            "contacts": {},
            "draft_revision": 2,
        },
    )
    first_service = AgentService(before_restart, persistence_required=True)
    prompted = await first_service.handle_message_result(
        "Новый заказ", "restart-destructive", update_id=300
    )
    assert prompted.order_stage == "awaiting_new_order_confirmation"
    assert prompted.destructive_confirmation_id is not None

    after_restart = build_agent_graph(
        None,
        checkpointer=saver,
        order_dialog=dialog(menu),
    )
    restarted_service = AgentService(after_restart, persistence_required=True)
    confirmed = await restarted_service.handle_message_result(
        "",
        "restart-destructive",
        update_id=301,
        callback_action="confirm_new_order",
        callback_revision=2,
        callback_confirmation_id=prompted.destructive_confirmation_id,
    )
    assert confirmed.text == "Новый заказ начат. Корзина пуста — выберите блюда."
    restored = await after_restart.aget_state(config)
    assert restored.values["cart"] == []
    assert restored.values["draft_revision"] == 3


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stage", "text"),
    [
        ("awaiting_new_order_confirmation", "Начать новый заказ"),
        ("awaiting_clear_confirmation", "Очистить корзину"),
        ("awaiting_cancel_confirmation", "Да, отменить"),
    ],
)
async def test_text_cannot_confirm_destructive_action(stage: str, text: str) -> None:
    order = dialog(FakeMenu([]))
    route, proposal = await order.classify(
        {
            "order_stage": stage,
            "user_message": text,
            "pending_destructive_action": {
                "action": "new_order",
                "return_stage": "idle",
                "confirmation_id": "session00001",
            },
        }
    )
    assert route == "order"
    assert proposal["action"] == "unknown"
    assert proposal["confidence"] == "low"


@pytest.mark.asyncio
async def test_legacy_pending_confirmation_without_session_fails_safe() -> None:
    current = dish("Рататуй")
    order = dialog(FakeMenu([current]))
    original_cart = [{"dish_id": str(current.dish_id), "quantity": 1}]
    result = await order.handle(
        {
            "order_stage": "awaiting_new_order_confirmation",
            "cart": original_cart,
            "contacts": {},
            "draft_revision": 3,
            "pending_destructive_action": {
                "action": "new_order",
                "return_stage": "idle",
            },
            "action_proposal": {"action": "confirm_new_order"},
            "callback_action": "confirm_new_order",
            "callback_revision": 3,
            "callback_confirmation_id": None,
        }
    )
    assert result["error_code"] == "stale_callback"
    assert "cart" not in result
    assert original_cart[0]["quantity"] == 1


@pytest.mark.asyncio
async def test_new_order_limit_counts_archive_and_current() -> None:
    order = dialog(FakeMenu([]))
    archive = [confirmed_snapshot(f"key-{index}") for index in range(4)]
    current = confirmed_snapshot("current")
    result = await order.handle(
        {
            "order_stage": "confirmed",
            "confirmed_order": current,
            "confirmed_order_archive": archive,
            "action_proposal": {"action": "new_order"},
        }
    )
    assert result["error_code"] == "confirmed_archive_full"
    assert "confirmed_order" not in result
    assert archive[-1]["confirmation_key"] == "key-3"


@pytest.mark.asyncio
async def test_cancel_does_not_delete_confirmed_snapshots() -> None:
    order = dialog(FakeMenu([]))
    archive = [confirmed_snapshot("archived")]
    result = await order.handle(
        {
            "order_stage": "collecting_phone",
            "confirmed_order": None,
            "confirmed_order_archive": archive,
            "cart": [],
            "contacts": {"customer_name": "Иван"},
            "action_proposal": {"action": "cancel"},
        }
    )
    assert result["confirmed_order_archive"] if "confirmed_order_archive" in result else archive
    assert result["confirmed_order"] is None
    assert result["contacts"] == {}


@pytest.mark.asyncio
async def test_wordform_uses_canonical_lookup_without_aliases() -> None:
    bruschetta, ratatouille = dish("Брускетта"), dish("Рататуй")
    order = dialog(FakeMenu([bruschetta, ratatouille]))
    state = {
        "order_stage": "idle",
        "cart": [],
        "draft_revision": 0,
        "action_proposal": OrderIntentProposal(
            action="add",
            items=[
                {"name": "брускетты", "lookup_query": "брускетта", "quantity": 2},
                {"name": "рататуй", "lookup_query": "рататуй", "quantity": 1},
            ],
        ).model_dump(mode="json"),
    }
    result = await order.handle(state)
    assert [item["quantity"] for item in result["cart"]] == [2, 1]


@pytest.mark.asyncio
async def test_ambiguous_multi_item_batch_is_applied_only_after_all_resolve() -> None:
    ratatouille = dish("Рататуй")
    salads = [dish("Салат греческий"), dish("Салат теплый")]
    order = dialog(FakeMenu([ratatouille, *salads]))
    state = {
        "order_stage": "idle",
        "cart": [],
        "draft_revision": 0,
        "action_proposal": OrderIntentProposal(
            action="add",
            items=[
                {"name": "рататуй", "quantity": 1},
                {"name": "салат", "quantity": 2},
            ],
        ).model_dump(mode="json"),
    }
    first = await order.handle(state)
    assert first["order_stage"] == "awaiting_disambiguation"
    assert state["cart"] == []
    assert len(first["pending_batch"]["proposal"]["items"]) == 2
    state = apply(state, first)
    state["user_message"] = "1"
    completed = await order.handle(state)
    assert sorted(item["quantity"] for item in completed["cart"]) == [1, 2]
    assert completed["pending_batch"] is None


@pytest.mark.asyncio
async def test_missing_item_in_batch_never_partially_changes_cart() -> None:
    ratatouille = dish("Рататуй")
    order = dialog(FakeMenu([ratatouille]))
    original = [{"dish_id": str(ratatouille.dish_id), "quantity": 1}]
    result = await order.handle(
        {
            "order_stage": "idle",
            "cart": original,
            "draft_revision": 1,
            "action_proposal": OrderIntentProposal(
                action="add",
                items=[
                    {"name": "рататуй", "quantity": 1},
                    {"name": "несуществующее блюдо", "quantity": 1},
                ],
            ).model_dump(mode="json"),
        }
    )
    assert result["error_code"] == "dish_not_found"
    assert "cart" not in result


@pytest.mark.asyncio
async def test_multiple_ambiguous_items_are_resolved_before_atomic_apply() -> None:
    order = dialog(
        FakeMenu(
            [
                dish("Салат греческий"),
                dish("Салат теплый"),
                dish("Суп овощной"),
                dish("Суп томатный"),
            ]
        )
    )
    state = {
        "order_stage": "idle",
        "cart": [],
        "draft_revision": 0,
        "action_proposal": OrderIntentProposal(
            action="add",
            items=[{"name": "салат"}, {"name": "суп"}],
        ).model_dump(mode="json"),
    }
    state = apply(state, await order.handle(state))
    assert state["pending_batch"]["unresolved_index"] == 0
    assert state["cart"] == []
    state["user_message"] = "1"
    state = apply(state, await order.handle(state))
    assert state["pending_batch"]["unresolved_index"] == 1
    assert state["cart"] == []
    state["user_message"] = "2"
    completed = await order.handle(state)
    assert len(completed["cart"]) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("original", "canonical", "quantity"),
    [
        ("греческий салат", "греческий салат", 1),
        ("греческих салата", "греческий салат", 2),
    ],
)
async def test_russian_number_forms_resolve_through_canonical_query(
    original: str, canonical: str, quantity: int
) -> None:
    salad = dish("Греческий салат")
    order = dialog(FakeMenu([salad]))
    result = await order.handle(
        {
            "order_stage": "idle",
            "cart": [],
            "draft_revision": 0,
            "action_proposal": OrderIntentProposal(
                action="add",
                items=[{
                    "name": original,
                    "lookup_query": canonical,
                    "quantity": quantity,
                }],
            ).model_dump(mode="json"),
        }
    )
    assert result["cart"] == [{"dish_id": str(salad.dish_id), "quantity": quantity}]


@pytest.mark.asyncio
async def test_inflected_remove_from_cart_uses_canonical_query() -> None:
    bruschetta = dish("Брускетта с томатами")
    order = dialog(FakeMenu([bruschetta]))
    result = await order.handle(
        {
            "order_stage": "idle",
            "cart": [{"dish_id": str(bruschetta.dish_id), "quantity": 2}],
            "draft_revision": 1,
            "action_proposal": OrderIntentProposal(
                action="decrement",
                items=[{
                    "name": "брускетту",
                    "lookup_query": "брускетта",
                    "quantity": 1,
                }],
            ).model_dump(mode="json"),
        }
    )
    assert result["cart"][0]["quantity"] == 1


@pytest.mark.asyncio
async def test_confirmation_aliases_are_stage_local_and_skip_llm() -> None:
    class FailingExtractor:
        async def extract(self, message):
            raise AssertionError(f"LLM must not receive confirmation action: {message}")

    current = dish("Рататуй")

    @asynccontextmanager
    async def factory():
        yield FakeMenu([current])

    order = OrderDialog(factory, FakeSettings(), FailingExtractor())
    for phrase, expected in (
        ("Подтверждаю", "confirm"),
        ("подтвердить", "confirm"),
        ("нет", "edit"),
        ("изменить заказ", "edit"),
        ("отмена", "cancel"),
        ("отменить", "cancel"),
    ):
        route, proposal = await order.classify(
            {"order_stage": "awaiting_confirmation", "user_message": phrase}
        )
        assert route == "order"
        assert proposal["action"] == expected

    class FaqExtractor:
        async def extract(self, message):
            del message
            return OrderIntentProposal(action="faq")

    outside = OrderDialog(factory, FakeSettings(), FaqExtractor())
    route, _ = await outside.classify({"order_stage": "idle", "user_message": "нет"})
    assert route == "faq"


@pytest.mark.asyncio
async def test_confirmation_edit_cancel_and_stale_callback_lifecycle() -> None:
    current = dish("Рататуй")
    order = dialog(FakeMenu([current]))
    contacts = {
        "customer_name": "Иван Тестов",
        "phone": "+34600000001",
        "address": "Calle Sintética 10",
        "requested_at": None,
    }
    state = {
        "order_stage": "idle",
        "cart": [{"dish_id": str(current.dish_id), "quantity": 1}],
        "contacts": contacts,
        "draft_revision": 3,
        "confirmed_order_archive": [confirmed_snapshot("archived")],
    }
    state = apply(state, await order._prepare_quote(state, type("C", (), contacts)()))

    stale = await order.handle({
        **state,
        "callback_action": "edit",
        "callback_revision": 2,
        "action_proposal": {"action": "edit"},
    })
    assert stale == {
        "response": "Эта кнопка устарела. Используйте действия из последнего сообщения.",
        "error_code": "stale_callback",
    }

    state = apply(state, await order.handle({
        **state,
        "callback_action": "edit",
        "callback_revision": 3,
        "action_proposal": {"action": "edit"},
    }))
    assert state["order_stage"] == "awaiting_edit_choice"
    assert state["quote_snapshot"] is not None

    state = apply(state, await order.handle({
        **state,
        "callback_action": "return_summary",
        "callback_revision": 3,
        "action_proposal": {"action": "return_summary"},
    }))
    assert state["order_stage"] == "awaiting_confirmation"

    state = apply(state, await order.handle({
        **state,
        "callback_action": "cancel",
        "callback_revision": 3,
        "action_proposal": {"action": "cancel"},
    }))
    assert state["order_stage"] == "awaiting_cancel_confirmation"
    cancel_confirmation_id = state["pending_destructive_action"]["confirmation_id"]

    back = await order.handle({
        **state,
        "callback_action": "cancel_back",
        "callback_revision": 3,
        "callback_confirmation_id": cancel_confirmation_id,
        "action_proposal": {"action": "cancel_back"},
    })
    assert back["order_stage"] == "awaiting_confirmation"
    assert "cart" not in back

    cancelled = await order.handle({
        **state,
        "callback_action": "cancel_yes",
        "callback_revision": 3,
        "callback_confirmation_id": cancel_confirmation_id,
        "action_proposal": {"action": "cancel_yes"},
    })
    assert cancelled["cart"] == []
    assert cancelled["contacts"] == {}
    assert cancelled["confirmed_order_archive"] if "confirmed_order_archive" in cancelled else state["confirmed_order_archive"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "stage", "value", "field", "expected"),
    [
        ("edit_name", "collecting_edit_name", "Мария Тестова", "customer_name", "Мария Тестова"),
        ("edit_phone", "collecting_edit_phone", "+34 611 111 111", "phone", "+34611111111"),
        ("edit_address", "collecting_edit_address", "Calle Nueva 20", "address", "Calle Nueva 20"),
        ("edit_time", "collecting_edit_time", "19.11.2026 19:30", "requested_at", "2026-11-19T19:30:00+01:00"),
    ],
)
async def test_editing_one_contact_field_preserves_others_and_refreshes_summary(
    action: str, stage: str, value: str, field: str, expected: str
) -> None:
    current = dish("Рататуй")
    order = dialog(FakeMenu([current]))
    original = {
        "customer_name": "Иван Тестов",
        "phone": "+34600000001",
        "address": "Calle Sintética 10",
        "requested_at": None,
    }
    state = {
        "order_stage": "awaiting_edit_choice",
        "cart": [{"dish_id": str(current.dish_id), "quantity": 1}],
        "contacts": original,
        "draft_revision": 5,
        "callback_action": action,
        "callback_revision": 5,
        "action_proposal": {"action": action},
    }
    state = apply(state, await order.handle(state))
    assert state["order_stage"] == stage
    state.update(
        user_message=value,
        callback_action=None,
        callback_revision=None,
        action_proposal={"action": "unknown", "confidence": "low"},
    )
    result = await order.handle(state)
    assert result["contacts"][field] == expected
    for key, old_value in original.items():
        if key != field:
            assert result["contacts"][key] == old_value
    assert result["order_stage"] == "awaiting_confirmation"
    assert result["draft_revision"] == 6
    assert result["quote_snapshot"]["draft_revision"] == 6


@pytest.mark.asyncio
async def test_cart_typo_requires_explicit_choice_before_decrement() -> None:
    bruschetta = dish("Брускетта с томатами")
    other = dish("Рататуй")
    order = dialog(FakeMenu([bruschetta, other]))
    original = [
        {"dish_id": str(bruschetta.dish_id), "quantity": 2},
        {"dish_id": str(other.dish_id), "quantity": 1},
    ]
    state = {
        "order_stage": "idle",
        "cart": original,
        "draft_revision": 1,
        "action_proposal": OrderIntentProposal(
            action="decrement",
            items=[{"name": "брускету", "lookup_query": "брускета", "quantity": 1}],
        ).model_dump(mode="json"),
    }
    first = await order.handle(state)
    assert first["order_stage"] == "awaiting_disambiguation"
    assert len(first["candidates"]) == 1
    assert "cart" not in first
    state = apply(state, first)
    state["user_message"] = "1"
    completed = await order.handle(state)
    assert completed["cart"][0]["quantity"] == 1


@pytest.mark.asyncio
async def test_weak_cart_typo_does_not_offer_or_apply_candidate() -> None:
    bruschetta = dish("Брускетта с томатами")
    order = dialog(FakeMenu([bruschetta]))
    result = await order.handle({
        "order_stage": "idle",
        "cart": [{"dish_id": str(bruschetta.dish_id), "quantity": 2}],
        "draft_revision": 1,
        "action_proposal": OrderIntentProposal(
            action="decrement", items=[{"name": "совсем другое", "quantity": 1}]
        ).model_dump(mode="json"),
    })
    assert result["error_code"] == "dish_not_found"
    assert "cart" not in result


@pytest.mark.asyncio
async def test_edit_cart_keeps_contact_data_and_returns_fresh_summary() -> None:
    current = dish("Рататуй")
    order = dialog(FakeMenu([current]))
    contacts = {
        "customer_name": "Иван Тестов",
        "phone": "+34600000001",
        "address": "Calle Sintética 10",
        "requested_at": None,
    }
    state = {
        "order_stage": "awaiting_edit_choice",
        "cart": [{"dish_id": str(current.dish_id), "quantity": 2}],
        "contacts": contacts,
        "draft_revision": 4,
        "callback_action": "edit_cart",
        "callback_revision": 4,
        "action_proposal": {"action": "edit_cart"},
    }
    state = apply(state, await order.handle(state))
    assert state["order_stage"] == "editing_cart"
    state.update(
        callback_action=None,
        callback_revision=None,
        action_proposal=OrderIntentProposal(
            action="decrement", items=[{"name": "Рататуй", "quantity": 1}]
        ).model_dump(mode="json"),
    )
    state = apply(state, await order.handle(state))
    assert state["order_stage"] == "editing_cart"
    assert state["cart"][0]["quantity"] == 1
    assert state["contacts"] == contacts
    assert state["draft_revision"] == 5
    state.update(
        callback_action="return_summary",
        callback_revision=5,
        action_proposal={"action": "return_summary"},
    )
    result = await order.handle(state)
    assert result["order_stage"] == "awaiting_confirmation"
    assert result["quote_snapshot"]["subtotal_minor"] == 1000


@pytest.mark.asyncio
async def test_edit_time_asap_callback_invalidates_quote_and_preserves_contacts() -> None:
    current = dish("Рататуй")
    order = dialog(FakeMenu([current]))
    contacts = {
        "customer_name": "Иван Тестов",
        "phone": "+34600000001",
        "address": "Calle Sintética 10",
        "requested_at": "2026-11-19T19:30:00+01:00",
    }
    state = {
        "order_stage": "awaiting_edit_choice",
        "cart": [{"dish_id": str(current.dish_id), "quantity": 1}],
        "contacts": contacts,
        "draft_revision": 2,
        "callback_action": "edit_time",
        "callback_revision": 2,
        "action_proposal": {"action": "edit_time"},
    }
    state = apply(state, await order.handle(state))
    state.update(
        user_message="Как можно скорее",
        callback_action="time_asap",
        callback_revision=2,
        action_proposal={"action": "time_asap"},
    )
    result = await order.handle(state)
    assert result["contacts"]["requested_at"] is None
    assert result["contacts"]["customer_name"] == contacts["customer_name"]
    assert result["draft_revision"] == 3
    assert result["order_stage"] == "awaiting_confirmation"


@pytest.mark.asyncio
async def test_unavailable_dish_can_still_be_removed_from_existing_cart() -> None:
    unavailable = dish("Брускетта с томатами", available=False)
    order = dialog(FakeMenu([unavailable]))
    result = await order.handle(
        {
            "order_stage": "idle",
            "cart": [{"dish_id": str(unavailable.dish_id), "quantity": 1}],
            "draft_revision": 1,
            "action_proposal": OrderIntentProposal(
                action="remove",
                items=[{"name": "брускетту", "lookup_query": "брускетта"}],
            ).model_dump(mode="json"),
        }
    )
    assert result["cart"] == []
