from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import datetime
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from app.agent.graph import build_agent_graph
from app.agent.service import AgentService
from app.menu.contracts import (
    CartLine,
    CartQuote,
    DishDetail,
    DishSummary,
    MenuSearchResult,
)
from app.orders.cart import CartOperationError, add_items
from app.orders.contracts import (
    ActionOutcome,
    ActionsOutcome,
    CartStateItem,
    OrderActionPlan,
    OrderIntentProposal,
)
from app.orders.dialog import (
    CHECKOUT_NOTICE,
    CONFIRMED_MESSAGE,
    OrderDialog,
    _parse_requested_at,
    _validate_address,
    _validate_name,
    _validate_phone,
)
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
            values = [
                item for item in values if query.casefold() in item.name.casefold()
            ]
        return MenuSearchResult(
            items=[
                DishSummary(
                    **item.model_dump(
                        exclude={"description", "ingredients", "allergens", "available"}
                    )
                )
                for item in values
            ]
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
        return CartQuote(
            items=lines, subtotal_minor=sum(line.line_total_minor for line in lines)
        )


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


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("José María", "José María"),
        ("O’Connor", "O’Connor"),
        ("Анна-Мария", "Анна-Мария"),
        ("李 雷", "李 雷"),
        ("A", None),
        ("Anne2", None),
        ("🙂 Anna", None),
        ("Anna\nMaria", None),
        ("Anna\u2028Maria", None),
        ("Anna--Maria", None),
        ("-Anna", None),
    ],
)
def test_checkout_name_validation_is_unicode_safe(raw: str, expected: str | None) -> None:
    assert _validate_name(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("+34 (600) 000-001", "+34600000001"),
        ("1234567", "1234567"),
        ("１２３４５６７", None),
        ("+123456", None),
        ("+1234567890123456", None),
        ("12+34567", None),
        ("+1234567 ext 2", None),
        ("+123\n4567", None),
        ("+123\u20284567", None),
    ],
)
def test_checkout_phone_validation_uses_ascii_bounds(
    raw: str, expected: str | None
) -> None:
    assert _validate_phone(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Calle Peña #10 (B)", "Calle Peña #10 (B)"),
        ("пр-т Мира, 7", "пр-т Мира, 7"),
        ("12345", None),
        ("A 1", None),
        ("Calle\nNueva 20", None),
        ("Calle\u2028Nueva 20", None),
        ("Calle 🏠 20", None),
    ],
)
def test_checkout_address_validation_is_unicode_safe(
    raw: str, expected: str | None
) -> None:
    assert _validate_address(raw) == expected


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
            items=[
                {"name": "Брускетта", "quantity": 2},
                {"name": "Греческий салат", "quantity": 1},
            ],
        ).model_dump(mode="json"),
    }

    state = apply(state, await order.handle(state))
    assert [item["quantity"] for item in state["cart"]] == [2, 1]
    assert state["last_referenced_dish_name"] == "Греческий салат"
    state["action_proposal"] = OrderIntentProposal(
        action="add", items=[{"name": "Брускетта", "quantity": 1}]
    ).model_dump(mode="json")
    state = apply(state, await order.handle(state))
    assert state["cart"][0]["quantity"] == 3
    assert state["draft_revision"] == 2


@pytest.mark.asyncio
async def test_action_plan_applies_two_set_quantities_atomically() -> None:
    salmon, lemonade, salad = (
        dish("Лосось с овощами"),
        dish("Домашний лимонад"),
        dish("Греческий салат"),
    )
    order = dialog(FakeMenu([salmon, lemonade, salad]))
    original = [
        {"dish_id": str(salmon.dish_id), "quantity": 2},
        {"dish_id": str(salad.dish_id), "quantity": 1},
        {"dish_id": str(lemonade.dish_id), "quantity": 3},
    ]
    result = await order.handle(
        {
            "order_stage": "idle",
            "cart": original,
            "draft_revision": 4,
            "action_proposal": OrderActionPlan(
                operations=[
                    {
                        "action": "set_quantity",
                        "items": [{"name": "Лосось с овощами", "quantity": 1}],
                    },
                    {
                        "action": "set_quantity",
                        "items": [{"name": "Домашний лимонад", "quantity": 4}],
                    },
                ]
            ).model_dump(mode="json"),
        }
    )
    quantities = {item["dish_id"]: item["quantity"] for item in result["cart"]}
    assert quantities[str(salmon.dish_id)] == 1
    assert quantities[str(lemonade.dish_id)] == 4
    assert quantities[str(salad.dish_id)] == 1
    assert result["draft_revision"] == 5
    assert "Изменения применены" in result["response"]


@pytest.mark.asyncio
async def test_action_plan_add_remove_and_set_quantity_preserves_other_lines() -> None:
    old, existing, unchanged, new = (
        dish("Старое блюдо"),
        dish("Изменяемое блюдо"),
        dish("Неизменяемое блюдо"),
        dish("Новое блюдо"),
    )
    order = dialog(FakeMenu([old, existing, unchanged, new]))
    result = await order.handle(
        {
            "order_stage": "idle",
            "cart": [
                {"dish_id": str(old.dish_id), "quantity": 1},
                {"dish_id": str(existing.dish_id), "quantity": 2},
                {"dish_id": str(unchanged.dish_id), "quantity": 3},
            ],
            "draft_revision": 2,
            "action_proposal": OrderActionPlan(
                operations=[
                    {
                        "action": "remove",
                        "items": [{"name": "Старое блюдо"}],
                    },
                    {
                        "action": "add",
                        "items": [{"name": "Новое блюдо", "quantity": 2}],
                    },
                    {
                        "action": "set_quantity",
                        "items": [{"name": "Изменяемое блюдо", "quantity": 4}],
                    },
                ]
            ).model_dump(mode="json"),
        }
    )
    quantities = {item["dish_id"]: item["quantity"] for item in result["cart"]}
    assert str(old.dish_id) not in quantities
    assert quantities[str(new.dish_id)] == 2
    assert quantities[str(existing.dish_id)] == 4
    assert quantities[str(unchanged.dish_id)] == 3


@pytest.mark.asyncio
async def test_invalid_middle_operation_leaves_action_plan_cart_unchanged() -> None:
    existing, new = dish("Существующее блюдо"), dish("Новое блюдо")
    order = dialog(FakeMenu([existing, new]))
    original = [{"dish_id": str(existing.dish_id), "quantity": 2}]
    state = {
        "order_stage": "idle",
        "cart": original,
        "draft_revision": 7,
        "action_proposal": OrderActionPlan(
            operations=[
                {
                    "action": "add",
                    "items": [{"name": "Новое блюдо", "quantity": 1}],
                },
                {
                    "action": "remove",
                    "items": [{"name": "Космический десерт"}],
                },
            ]
        ).model_dump(mode="json"),
    }
    original_state = deepcopy(state)
    result = await order.handle(state)
    assert result["error_code"] == "batch_dish_not_found"
    assert "Корзина не изменена" in result["response"]
    assert "cart" not in result
    assert state == original_state


@pytest.mark.asyncio
async def test_action_plan_ambiguity_resolves_before_any_mutation() -> None:
    salad_a, salad_b, new = (
        dish("Салат греческий"),
        dish("Салат теплый"),
        dish("Новое блюдо"),
    )
    order = dialog(FakeMenu([salad_a, salad_b, new]))
    state = {
        "order_stage": "idle",
        "cart": [
            {"dish_id": str(salad_a.dish_id), "quantity": 1},
            {"dish_id": str(salad_b.dish_id), "quantity": 1},
        ],
        "draft_revision": 2,
        "action_proposal": OrderActionPlan(
            operations=[
                {
                    "action": "add",
                    "items": [{"name": "Новое блюдо", "quantity": 1}],
                },
                {"action": "remove", "items": [{"name": "салат"}]},
            ]
        ).model_dump(mode="json"),
    }
    first = await order.handle(state)
    assert first["error_code"] == "batch_dish_ambiguous"
    assert "cart" not in first
    assert first["pending_batch"]["plan"]["operations"][0]["action"] == "add"
    state = apply(state, first)
    state["user_message"] = "1"
    completed = await order.handle(state)
    quantities = {item["dish_id"]: item["quantity"] for item in completed["cart"]}
    assert quantities[str(new.dish_id)] == 1
    assert str(salad_a.dish_id) not in quantities
    assert quantities[str(salad_b.dish_id)] == 1


@pytest.mark.asyncio
async def test_action_plan_limits_and_conflicts_fail_without_cart_update() -> None:
    existing, new = dish("Существующее блюдо"), dish("Новое блюдо")
    order = dialog(FakeMenu([existing, new]))
    original = [{"dish_id": str(existing.dish_id), "quantity": 20}]
    over_limit = await order.handle(
        {
            "order_stage": "idle",
            "cart": original,
            "draft_revision": 1,
            "action_proposal": OrderActionPlan(
                operations=[
                    {
                        "action": "add",
                        "items": [{"name": "Существующее блюдо", "quantity": 1}],
                    },
                    {
                        "action": "add",
                        "items": [{"name": "Новое блюдо", "quantity": 1}],
                    },
                ]
            ).model_dump(mode="json"),
        }
    )
    assert over_limit["error_code"] == "batch_cart_validation"
    assert "cart" not in over_limit

    conflict = await order.handle(
        {
            "order_stage": "idle",
            "cart": [{"dish_id": str(existing.dish_id), "quantity": 2}],
            "draft_revision": 1,
            "action_proposal": OrderActionPlan(
                operations=[
                    {
                        "action": "decrement",
                        "items": [{"name": "Существующее блюдо"}],
                    },
                    {
                        "action": "remove",
                        "items": [{"name": "Существующее блюдо"}],
                    },
                ]
            ).model_dump(mode="json"),
        }
    )
    assert conflict["error_code"] == "batch_conflict"
    assert "cart" not in conflict

    full_cart_dishes = [dish(f"Полная корзина {index}") for index in range(5)]
    extra_a, extra_b = dish("Дополнение A"), dish("Дополнение B")
    full_order = dialog(FakeMenu([*full_cart_dishes, extra_a, extra_b]))
    units_over_limit = await full_order.handle(
        {
            "order_stage": "idle",
            "cart": [
                {"dish_id": str(item.dish_id), "quantity": 20}
                for item in full_cart_dishes
            ],
            "draft_revision": 3,
            "action_proposal": OrderActionPlan(
                operations=[
                    {
                        "action": "add",
                        "items": [{"name": "Дополнение A", "quantity": 1}],
                    },
                    {
                        "action": "add",
                        "items": [{"name": "Дополнение B", "quantity": 1}],
                    },
                ]
            ).model_dump(mode="json"),
        }
    )
    assert units_over_limit["error_code"] == "batch_cart_validation"
    assert "cart" not in units_over_limit


@pytest.mark.asyncio
async def test_action_plan_replay_does_not_repeat_mutation() -> None:
    existing, new = dish("Существующее блюдо"), dish("Новое блюдо")
    menu = FakeMenu([existing, new])

    class PlanExtractor:
        def __init__(self) -> None:
            self.calls = 0

        async def extract(self, message, context):
            del message, context
            self.calls += 1
            return ActionsOutcome(
                operations=[
                    {
                        "action": "remove",
                        "items": [{"name": "Существующее блюдо"}],
                    },
                    {
                        "action": "add",
                        "items": [{"name": "Новое блюдо", "quantity": 2}],
                    },
                ]
            )

    extractor = PlanExtractor()

    @asynccontextmanager
    async def factory():
        yield menu

    order = OrderDialog(factory, FakeSettings(), extractor)
    saver = InMemorySaver()
    graph = build_agent_graph(None, checkpointer=saver, order_dialog=order)
    config = {"configurable": {"thread_id": "multi-action-replay"}}
    await graph.aupdate_state(
        config,
        {
            "order_stage": "idle",
            "cart": [{"dish_id": str(existing.dish_id), "quantity": 1}],
            "draft_revision": 1,
        },
    )
    service = AgentService(graph, persistence_required=True)
    first = await service.handle_message_result(
        "Синтетический составной запрос",
        "multi-action-replay",
        update_id=100,
    )
    replay = await service.handle_message_result(
        "Другой текст повторной доставки",
        "multi-action-replay",
        update_id=100,
    )
    state = await graph.aget_state(config)
    assert first.text == replay.text
    assert state.values["cart"] == [{"dish_id": str(new.dish_id), "quantity": 2}]
    assert state.values["draft_revision"] == 2
    assert extractor.calls == 1
    assert state.values["recent_interactions"][-1]["semantic"] == "cart_batch"


@pytest.mark.asyncio
async def test_action_plan_checkpoint_failure_never_exposes_partial_batch() -> None:
    existing, new = dish("Существующее блюдо"), dish("Новое блюдо")
    menu = FakeMenu([existing, new])

    class PlanExtractor:
        async def extract(self, message, context):
            del message, context
            return ActionsOutcome(
                operations=[
                    {
                        "action": "remove",
                        "items": [{"name": "Существующее блюдо"}],
                    },
                    {
                        "action": "add",
                        "items": [{"name": "Новое блюдо", "quantity": 2}],
                    },
                ]
            )

    class FailingFinalSaver(InMemorySaver):
        fail_enabled = False

        async def aput(self, config, checkpoint, metadata, new_versions):
            cart = checkpoint.get("channel_values", {}).get("cart")
            if self.fail_enabled and cart == [
                {"dish_id": str(new.dish_id), "quantity": 2}
            ]:
                raise ConnectionError("synthetic checkpoint failure")
            return await super().aput(config, checkpoint, metadata, new_versions)

    @asynccontextmanager
    async def factory():
        yield menu

    order = OrderDialog(factory, FakeSettings(), PlanExtractor())
    saver = FailingFinalSaver()
    graph = build_agent_graph(None, checkpointer=saver, order_dialog=order)
    config = {"configurable": {"thread_id": "multi-action-save-failure"}}
    original_cart = [{"dish_id": str(existing.dish_id), "quantity": 1}]
    await graph.aupdate_state(
        config,
        {
            "order_stage": "idle",
            "cart": original_cart,
            "draft_revision": 1,
        },
    )
    saver.fail_enabled = True
    service = AgentService(graph, persistence_required=True)
    result = await service.handle_message_result(
        "Синтетический составной запрос",
        "multi-action-save-failure",
        update_id=101,
    )
    saver.fail_enabled = False
    state = await graph.aget_state(config)
    assert result.error_code == "agent_error"
    completed_cart = [{"dish_id": str(new.dish_id), "quantity": 2}]
    assert state.values["cart"] in (original_cart, completed_cart)
    if state.values["cart"] == original_cart:
        assert state.values["draft_revision"] == 1
    else:
        assert state.values["draft_revision"] == 2


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
async def test_checkout_notice_is_shown_once_and_not_for_contact_edit() -> None:
    current = dish("Рататуй")
    order = dialog(FakeMenu([current]))
    state = {
        "order_stage": "idle",
        "cart": [{"dish_id": str(current.dish_id), "quantity": 1}],
        "contacts": {},
        "draft_revision": 1,
        "action_proposal": {"action": "checkout"},
    }
    state = apply(state, await order.handle(state))
    assert state["response"] == f"{CHECKOUT_NOTICE}\n\nКак вас зовут?"

    state.update(user_message="José María", action_proposal=None)
    name_result = await order.handle(state)
    assert name_result["response"] == "Укажите номер телефона."
    assert CHECKOUT_NOTICE not in name_result["response"]

    edit_result = await order.handle(
        {
            "order_stage": "awaiting_edit_choice",
            "cart": state["cart"],
            "contacts": {
                "customer_name": "José María",
                "phone": "+34600000001",
                "address": "Calle Sintética 10",
                "requested_at": None,
            },
            "draft_revision": 2,
            "callback_action": "edit_name",
            "callback_revision": 2,
            "action_proposal": {"action": "edit_name"},
        }
    )
    assert edit_result["response"] == "Укажите новое имя получателя."
    assert CHECKOUT_NOTICE not in edit_result["response"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stage",
    [
        "collecting_name",
        "collecting_phone",
        "collecting_address",
        "collecting_time",
        "collecting_edit_name",
        "collecting_edit_phone",
        "collecting_edit_address",
        "collecting_edit_time",
    ],
)
async def test_cancel_checkout_preserves_cart_and_clears_checkout_data(stage: str) -> None:
    current = dish("Рататуй")
    order = dialog(FakeMenu([current]))
    cart = [{"dish_id": str(current.dish_id), "quantity": 2}]
    result = await order.handle(
        {
            "order_stage": stage,
            "cart": deepcopy(cart),
            "contacts": {
                "customer_name": "José María",
                "phone": "+34600000001",
                "address": "Calle Sintética 10",
                "requested_at": None,
            },
            "quote_snapshot": {"synthetic": True},
            "draft_revision": 4,
            "checkout_session_id": "session00001",
            "pending_clarification": {
                "reason": "order_intent",
                "choices": [
                    {"action": "view_cart", "label": "Показать корзину"},
                    {"action": "checkout", "label": "Перейти к оформлению"},
                ],
                "return_stage": stage,
                "clarification_id": "clarify00001",
            },
            "callback_action": "cancel_checkout",
            "callback_revision": 4,
            "callback_confirmation_id": "session00001",
            "action_proposal": {"action": "cancel_checkout"},
        }
    )
    assert result["response"] == "Оформление отменено. Корзина сохранена."
    assert result["order_stage"] == "idle"
    assert result["contacts"] == {}
    assert result["quote_snapshot"] is None
    assert result["pending_clarification"] is None
    assert result["checkout_session_id"] is None
    assert result["draft_revision"] == 5
    assert "cart" not in result
    assert cart == [{"dish_id": str(current.dish_id), "quantity": 2}]


@pytest.mark.asyncio
async def test_old_checkout_session_is_stale_after_cancel_and_reentry() -> None:
    current = dish("Рататуй")
    order = dialog(FakeMenu([current]))
    base = {
        "order_stage": "idle",
        "cart": [{"dish_id": str(current.dish_id), "quantity": 1}],
        "contacts": {},
        "draft_revision": 1,
        "action_proposal": {"action": "checkout"},
    }
    first = apply(base, await order.handle(base))
    old_session = first["checkout_session_id"]
    cancelled = apply(
        first,
        await order.handle(
            {
                **first,
                "callback_action": "cancel_checkout",
                "callback_revision": 1,
                "callback_confirmation_id": old_session,
                "action_proposal": {"action": "cancel_checkout"},
            }
        ),
    )
    restarted_input = {
        **cancelled,
        "callback_action": None,
        "callback_revision": None,
        "callback_confirmation_id": None,
        "action_proposal": {"action": "checkout"},
    }
    restarted = apply(restarted_input, await order.handle(restarted_input))
    assert restarted["checkout_session_id"] != old_session

    stale = await order.handle(
        {
            **restarted,
            "callback_action": "cancel_checkout",
            "callback_revision": 1,
            "callback_confirmation_id": old_session,
            "action_proposal": {"action": "cancel_checkout"},
        }
    )
    assert stale["error_code"] == "stale_callback"
    assert "contacts" not in stale


@pytest.mark.asyncio
async def test_checkout_session_survives_checkpoint_restart_for_current_callback() -> None:
    current = dish("Рататуй")
    saver = InMemorySaver()
    config = {"configurable": {"thread_id": "checkout-session-restart"}}
    first_graph = build_agent_graph(
        None, checkpointer=saver, order_dialog=dialog(FakeMenu([current]))
    )
    await first_graph.aupdate_state(
        config,
        {
            "order_stage": "collecting_time",
            "cart": [{"dish_id": str(current.dish_id), "quantity": 1}],
            "contacts": {
                "customer_name": "José María",
                "phone": "+34600000001",
                "address": "Calle Sintética 10",
                "requested_at": None,
            },
            "draft_revision": 3,
            "checkout_session_id": "session00001",
        },
    )
    restarted_graph = build_agent_graph(
        None, checkpointer=saver, order_dialog=dialog(FakeMenu([current]))
    )
    result = await AgentService(
        restarted_graph, persistence_required=True
    ).handle_message_result(
        "Как можно скорее",
        "checkout-session-restart",
        update_id=1,
        callback_action="time_asap",
        callback_revision=3,
        callback_confirmation_id="session00001",
    )
    assert result.order_stage == "awaiting_confirmation"
    assert result.error_code is None
    assert result.checkout_session_id is None


@pytest.mark.asyncio
async def test_cancel_checkout_callback_replay_does_not_repeat_state_change() -> None:
    current = dish("Рататуй")
    saver = InMemorySaver()
    graph = build_agent_graph(
        None, checkpointer=saver, order_dialog=dialog(FakeMenu([current]))
    )
    config = {"configurable": {"thread_id": "checkout-cancel-replay"}}
    cart = [{"dish_id": str(current.dish_id), "quantity": 2}]
    await graph.aupdate_state(
        config,
        {
            "order_stage": "collecting_phone",
            "cart": cart,
            "contacts": {"customer_name": "José María"},
            "draft_revision": 2,
            "checkout_session_id": "session00001",
        },
    )
    service = AgentService(graph, persistence_required=True)
    kwargs = {
        "update_id": 7,
        "callback_action": "cancel_checkout",
        "callback_revision": 2,
        "callback_confirmation_id": "session00001",
    }
    first = await service.handle_message_result(
        "Отменить оформление", "checkout-cancel-replay", **kwargs
    )
    replay = await service.handle_message_result(
        "Отменить оформление", "checkout-cancel-replay", **kwargs
    )
    state = await graph.aget_state(config)
    assert replay.text == first.text
    assert state.values["draft_revision"] == 3
    assert state.values["cart"] == cart
    assert state.values["contacts"] == {}
    assert state.values["checkout_session_id"] is None


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
        "contacts": {
            "customer_name": "Иван",
            "phone": "+34600000001",
            "address": "Test 10",
        },
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
        callback_confirmation_id=prompted["pending_destructive_action"][
            "confirmation_id"
        ],
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
    assert (
        _parse_requested_at("19.11.2026 19:30", now)[1] == "2026-11-19T19:30:00+01:00"
    )
    assert _parse_requested_at("31.02.2027 19:30", now) == (False, None)


@pytest.mark.asyncio
async def test_deterministic_guards_and_safe_llm_failure() -> None:
    assert deterministic_intent("Подтверждаю!").action == "confirm"
    for phrase in ("Новый заказ", "Оформить еще один заказ", "Хочу заказать ещё"):
        assert deterministic_intent(phrase).action == "new_order"

    class BrokenExtractor:
        async def extract(self, message, context):
            del message, context
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
async def test_new_order_archives_confirmed_snapshot_and_start_is_non_destructive() -> (
    None
):
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
async def test_new_order_with_active_draft_requires_revision_bound_confirmation() -> (
    None
):
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
    assert (
        prompted["pending_destructive_action"]["return_stage"] == "collecting_address"
    )
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
async def test_order_clarification_persists_and_callback_continues_cart() -> None:
    current = dish("Рататуй")
    order = dialog(FakeMenu([current]))
    saver = InMemorySaver()
    graph = build_agent_graph(None, checkpointer=saver, order_dialog=order)
    config = {"configurable": {"thread_id": "clarification-restart"}}
    await graph.aupdate_state(
        config,
        {
            "order_stage": "idle",
            "cart": [{"dish_id": str(current.dish_id), "quantity": 1}],
            "draft_revision": 3,
        },
    )
    service = AgentService(graph, persistence_required=True)
    prompted = await service.handle_message_result(
        "Заказ", "clarification-restart", update_id=1
    )
    assert prompted.clarification_id is not None

    # A new compiled graph over the same saver simulates process restoration.
    restored = AgentService(
        build_agent_graph(None, checkpointer=saver, order_dialog=order),
        persistence_required=True,
    )
    result = await restored.handle_message_result(
        "Показать корзину",
        "clarification-restart",
        update_id=2,
        callback_action="clarify_view_cart",
        callback_revision=3,
        callback_confirmation_id=prompted.clarification_id,
    )
    assert result.text.startswith("Корзина:")
    assert result.clarification_id is None
    replay = await restored.handle_message_result(
        "Показать корзину",
        "clarification-restart",
        update_id=2,
        callback_action="clarify_view_cart",
        callback_revision=3,
        callback_confirmation_id=prompted.clarification_id,
    )
    assert replay.text == result.text


@pytest.mark.asyncio
async def test_order_clarification_free_text_continues_checkout() -> None:
    current = dish("Рататуй")
    order = dialog(FakeMenu([current]))
    state = {
        "order_stage": "idle",
        "cart": [{"dish_id": str(current.dish_id), "quantity": 1}],
        "draft_revision": 1,
        "pending_clarification": {
            "reason": "order_intent",
            "choices": [
                {"action": "view_cart", "label": "Показать корзину"},
                {"action": "checkout", "label": "Перейти к оформлению"},
            ],
            "return_stage": "idle",
            "clarification_id": "session00001",
        },
        "user_message": "Оформление",
    }
    route, proposal = await order.classify(state)
    assert route == "order"
    assert proposal["action"] == "checkout"
    result = await order.handle({**state, "action_proposal": proposal})
    assert result["order_stage"] == "collecting_name"


@pytest.mark.asyncio
async def test_contact_capture_precedes_semantic_clarification() -> None:
    current = dish("Рататуй")
    order = dialog(FakeMenu([current]))
    state = {
        "order_stage": "collecting_name",
        "cart": [{"dish_id": str(current.dish_id), "quantity": 1}],
        "contacts": {},
        "draft_revision": 1,
        "user_message": "Заказ",
    }
    route, proposal = await order.classify(state)
    result = await order.handle({**state, "action_proposal": proposal})
    assert route == "order"
    assert result["contacts"]["customer_name"] == "Заказ"
    assert result["order_stage"] == "collecting_phone"
    assert "pending_clarification" not in result


@pytest.mark.asyncio
async def test_faq_topic_switch_invalidates_pending_without_losing_checkout() -> None:
    class FakeFaq:
        async def answer(self, question):
            raise AssertionError(question)

    order = dialog(FakeMenu([]))
    saver = InMemorySaver()
    graph = build_agent_graph(FakeFaq(), checkpointer=saver, order_dialog=order)
    config = {"configurable": {"thread_id": "clarification-faq"}}
    contacts = {"customer_name": "Иван"}
    await graph.aupdate_state(
        config,
        {
            "order_stage": "collecting_phone",
            "contacts": contacts,
            "draft_revision": 2,
            "pending_clarification": {
                "reason": "order_intent",
                "choices": [
                    {"action": "view_cart", "label": "Показать корзину"},
                    {"action": "checkout", "label": "Перейти к оформлению"},
                ],
                "return_stage": "collecting_phone",
                "clarification_id": "session00001",
            },
        },
    )
    result = await AgentService(graph, persistence_required=True).handle_message_result(
        "До скольких вы работаете?", "clarification-faq", update_id=1
    )
    state = await graph.aget_state(config)
    assert result.text == "Укажите корректный номер телефона (7–15 цифр)."
    assert state.values["pending_clarification"] is None
    assert state.values["order_stage"] == "collecting_phone"
    assert state.values["contacts"] == contacts


@pytest.mark.asyncio
async def test_clarification_stale_callback_cannot_continue() -> None:
    current = dish("Рататуй")
    order = dialog(FakeMenu([current]))
    result = await order.handle(
        {
            "order_stage": "idle",
            "cart": [{"dish_id": str(current.dish_id), "quantity": 1}],
            "draft_revision": 4,
            "pending_clarification": {
                "reason": "order_intent",
                "choices": [
                    {"action": "view_cart", "label": "Показать корзину"},
                    {"action": "checkout", "label": "Перейти к оформлению"},
                ],
                "return_stage": "idle",
                "clarification_id": "session00001",
            },
            "action_proposal": {"action": "clarify_checkout"},
            "callback_action": "clarify_checkout",
            "callback_revision": 3,
            "callback_confirmation_id": "session00001",
        }
    )
    assert result["error_code"] == "stale_callback"
    assert "contacts" not in result


@pytest.mark.asyncio
async def test_referenced_dish_uses_authoritative_id_and_fresh_details() -> None:
    current = dish("Рататуй")
    current.allergens = ["сельдерей"]
    order = dialog(FakeMenu([current]))
    result = await order.handle(
        {
            "order_stage": "idle",
            "last_referenced_dish_id": str(current.dish_id),
            "last_referenced_dish_name": current.name,
            "action_proposal": {"action": "referenced_dish_info"},
        }
    )
    assert "сельдерей" in result["response"]
    assert result["last_referenced_dish_id"] == str(current.dish_id)


@pytest.mark.asyncio
async def test_missing_referent_creates_typed_clarification() -> None:
    order = dialog(FakeMenu([]))
    result = await order.handle(
        {
            "order_stage": "idle",
            "action_proposal": {"action": "referenced_dish_info"},
        }
    )
    assert result["pending_clarification"]["reason"] == "dish_referent"
    assert result["pending_clarification"]["choices"][0]["action"] == "dish_info"


@pytest.mark.asyncio
async def test_previous_order_reports_only_checkpoint_facts() -> None:
    order = dialog(FakeMenu([]))
    absent = await order.handle(
        {"order_stage": "idle", "action_proposal": {"action": "previous_order"}}
    )
    assert "нет сохраненного" in absent["response"]
    assert "не могу подтвердить размещение" in absent["response"]

    snapshot = confirmed_snapshot("previous")
    snapshot["items"] = [{"name": "Рататуй", "quantity": 2}]
    present = await order.handle(
        {
            "order_stage": "idle",
            "confirmed_order": snapshot,
            "action_proposal": {"action": "previous_order"},
        }
    )
    assert "Рататуй × 2" in present["response"]
    assert "еще не размещен" in present["response"]
    assert "готовится" not in present["response"]


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
async def test_clear_with_active_cart_requires_confirmation_and_can_be_declined() -> (
    None
):
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
        callback_confirmation_id=prompted["pending_destructive_action"][
            "confirmation_id"
        ],
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
        callback_confirmation_id=prompted["pending_destructive_action"][
            "confirmation_id"
        ],
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
    assert (
        result["confirmed_order_archive"]
        if "confirmed_order_archive" in result
        else archive
    )
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
                items=[
                    {
                        "name": original,
                        "lookup_query": canonical,
                        "quantity": quantity,
                    }
                ],
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
                items=[
                    {
                        "name": "брускетту",
                        "lookup_query": "брускетта",
                        "quantity": 1,
                    }
                ],
            ).model_dump(mode="json"),
        }
    )
    assert result["cart"][0]["quantity"] == 1


@pytest.mark.asyncio
async def test_confirmation_aliases_are_stage_local_and_skip_llm() -> None:
    class FailingExtractor:
        async def extract(self, message, context):
            del context
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
        async def extract(self, message, context):
            del message, context
            return ActionOutcome(action="faq")

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

    stale = await order.handle(
        {
            **state,
            "callback_action": "edit",
            "callback_revision": 2,
            "action_proposal": {"action": "edit"},
        }
    )
    assert stale == {
        "response": "Эта кнопка устарела. Используйте действия из последнего сообщения.",
        "error_code": "stale_callback",
    }

    state = apply(
        state,
        await order.handle(
            {
                **state,
                "callback_action": "edit",
                "callback_revision": 3,
                "action_proposal": {"action": "edit"},
            }
        ),
    )
    assert state["order_stage"] == "awaiting_edit_choice"
    assert state["quote_snapshot"] is not None

    state = apply(
        state,
        await order.handle(
            {
                **state,
                "callback_action": "return_summary",
                "callback_revision": 3,
                "action_proposal": {"action": "return_summary"},
            }
        ),
    )
    assert state["order_stage"] == "awaiting_confirmation"

    state = apply(
        state,
        await order.handle(
            {
                **state,
                "callback_action": "cancel",
                "callback_revision": 3,
                "action_proposal": {"action": "cancel"},
            }
        ),
    )
    assert state["order_stage"] == "awaiting_cancel_confirmation"
    cancel_confirmation_id = state["pending_destructive_action"]["confirmation_id"]

    back = await order.handle(
        {
            **state,
            "callback_action": "cancel_back",
            "callback_revision": 3,
            "callback_confirmation_id": cancel_confirmation_id,
            "action_proposal": {"action": "cancel_back"},
        }
    )
    assert back["order_stage"] == "awaiting_confirmation"
    assert "cart" not in back

    cancelled = await order.handle(
        {
            **state,
            "callback_action": "cancel_yes",
            "callback_revision": 3,
            "callback_confirmation_id": cancel_confirmation_id,
            "action_proposal": {"action": "cancel_yes"},
        }
    )
    assert cancelled["cart"] == []
    assert cancelled["contacts"] == {}
    assert (
        cancelled["confirmed_order_archive"]
        if "confirmed_order_archive" in cancelled
        else state["confirmed_order_archive"]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "stage", "value", "field", "expected"),
    [
        (
            "edit_name",
            "collecting_edit_name",
            "Мария Тестова",
            "customer_name",
            "Мария Тестова",
        ),
        (
            "edit_phone",
            "collecting_edit_phone",
            "+34 611 111 111",
            "phone",
            "+34611111111",
        ),
        (
            "edit_address",
            "collecting_edit_address",
            "Calle Nueva 20",
            "address",
            "Calle Nueva 20",
        ),
        (
            "edit_time",
            "collecting_edit_time",
            "19.11.2026 19:30",
            "requested_at",
            "2026-11-19T19:30:00+01:00",
        ),
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
    result = await order.handle(
        {
            "order_stage": "idle",
            "cart": [{"dish_id": str(bruschetta.dish_id), "quantity": 2}],
            "draft_revision": 1,
            "action_proposal": OrderIntentProposal(
                action="decrement", items=[{"name": "совсем другое", "quantity": 1}]
            ).model_dump(mode="json"),
        }
    )
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
async def test_edit_time_asap_callback_invalidates_quote_and_preserves_contacts() -> (
    None
):
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
    checkout_session_id = state["checkout_session_id"]
    state.update(
        user_message="Как можно скорее",
        callback_action="time_asap",
        callback_revision=2,
        callback_confirmation_id=checkout_session_id,
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
