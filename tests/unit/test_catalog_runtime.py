from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from telegram.error import TimedOut

from app.agent.graph import build_agent_graph
from app.agent.service import AgentService
from app.bot.catalog_runtime import run_catalog_update
from app.catalog_ui.models import CatalogScreen, CatalogUiState
from app.menu.contracts import CartInspection, CategoryList, CategorySummary


class FakeMenu:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def inspect_cart(self, items):
        del items
        return CartInspection(
            items=[], available_subtotal_minor=0, has_unavailable_items=False
        )

    async def list_categories(self):
        return CategoryList(
            categories=[
                CategorySummary(id="soups", name="Супы", available_dish_count=1)
            ]
        )


def runtime():
    graph = build_agent_graph(None, checkpointer=InMemorySaver())
    return graph, AgentService(graph, persistence_required=True)


async def test_recovery_prepare_ack_and_duplicate_delivery_protection() -> None:
    graph, service = runtime()
    config = {"configurable": {"thread_id": "thread"}}
    await graph.aupdate_state(
        config,
        {
            "cart": [],
            "contacts": {"name": "Synthetic"},
            "draft_revision": 7,
            "quote_snapshot": {"total_minor": 1000},
        },
    )
    bot = SimpleNamespace(
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=42)),
        edit_message_text=AsyncMock(return_value=SimpleNamespace(message_id=42)),
    )
    args = dict(
        service=service,
        menu_factory=FakeMenu,
        bot=bot,
        chat_id=1,
        conversation_id="thread",
        update_id=10,
        recovery=True,
    )
    assert await run_catalog_update(**args) == "delivered"
    assert await run_catalog_update(**args) == "replay"
    assert bot.send_message.await_count == 1
    assert bot.edit_message_text.await_count == 0
    values = (await graph.aget_state(config)).values
    ui = CatalogUiState.from_checkpoint(values["catalog_ui"])
    assert ui.active_screen is CatalogScreen.HOME
    assert ui.pending_render is None and ui.active_message_id == 42
    assert values["cart"] == []
    assert values["contacts"] == {"name": "Synthetic"}
    assert values["draft_revision"] == 7
    assert values["quote_snapshot"] == {"total_minor": 1000}
    assert values["catalog_transition"] is None

    payload = ui.pending_render
    assert payload is None
    open_categories = ui.active_actions.index(
        next(action for action in ui.active_actions if action.kind == "open_categories")
    )
    callback_data = f"cat:1:{ui.active_token}:{open_categories}"
    assert (
        await run_catalog_update(
            service=service,
            menu_factory=FakeMenu,
            bot=bot,
            chat_id=1,
            conversation_id="thread",
            update_id=11,
            payload=callback_data,
            callback_message_id=42,
        )
        == "delivered"
    )
    updated = CatalogUiState.from_checkpoint(
        (await graph.aget_state(config)).values["catalog_ui"]
    )
    assert updated.active_screen is CatalogScreen.CATEGORIES
    assert bot.edit_message_text.await_count == 1


async def test_timeout_keeps_pending_without_acknowledgement() -> None:
    graph, service = runtime()
    bot = SimpleNamespace(
        send_message=AsyncMock(side_effect=TimedOut("synthetic")),
        edit_message_text=AsyncMock(),
    )
    assert (
        await run_catalog_update(
            service=service,
            menu_factory=FakeMenu,
            bot=bot,
            chat_id=1,
            conversation_id="timeout-thread",
            update_id=20,
            recovery=True,
        )
        == "unknown"
    )
    values = (
        await graph.aget_state({"configurable": {"thread_id": "timeout-thread"}})
    ).values
    ui = CatalogUiState.from_checkpoint(values["catalog_ui"])
    assert ui.pending_render is not None
    assert ui.active_token is None


async def test_explicit_menu_recovery_sends_new_home_instead_of_editing_old_ui() -> (
    None
):
    graph, service = runtime()
    config = {"configurable": {"thread_id": "deleted-history-thread"}}
    previous = CatalogUiState(
        active_message_id=777,
        active_token="oldtoken1",
    )
    await graph.aupdate_state(
        config,
        {
            "catalog_ui": previous.to_checkpoint(),
            "cart": [],
            "contacts": {"name": "Synthetic"},
            "draft_revision": 8,
        },
    )
    bot = SimpleNamespace(
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=778)),
        edit_message_text=AsyncMock(return_value=SimpleNamespace(message_id=777)),
    )
    assert (
        await run_catalog_update(
            service=service,
            menu_factory=FakeMenu,
            bot=bot,
            chat_id=1,
            conversation_id="deleted-history-thread",
            update_id=40,
            recovery=True,
        )
        == "delivered"
    )
    bot.send_message.assert_awaited_once()
    bot.edit_message_text.assert_not_awaited()
    ui = CatalogUiState.from_checkpoint(
        (await graph.aget_state(config)).values["catalog_ui"]
    )
    assert ui.active_screen is CatalogScreen.HOME
    assert ui.active_message_id == 778
    assert ui.active_token != "oldtoken1"


async def test_checkpoint_failures_bound_telegram_delivery() -> None:
    graph, service = runtime()
    bot = SimpleNamespace(
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=50)),
        edit_message_text=AsyncMock(),
    )
    original_commit = service.commit_catalog_unlocked
    service.commit_catalog_unlocked = AsyncMock(side_effect=RuntimeError("checkpoint"))
    with pytest.raises(RuntimeError, match="checkpoint"):
        await run_catalog_update(
            service=service,
            menu_factory=FakeMenu,
            bot=bot,
            chat_id=1,
            conversation_id="before-failure",
            update_id=30,
            recovery=True,
        )
    bot.send_message.assert_not_awaited()

    service.commit_catalog_unlocked = original_commit
    service.acknowledge_catalog_unlocked = AsyncMock(
        side_effect=RuntimeError("ack checkpoint")
    )
    with pytest.raises(RuntimeError, match="ack checkpoint"):
        await run_catalog_update(
            service=service,
            menu_factory=FakeMenu,
            bot=bot,
            chat_id=1,
            conversation_id="after-failure",
            update_id=31,
            recovery=True,
        )
    assert bot.send_message.await_count == 1
    ui = CatalogUiState.from_checkpoint(
        (
            await graph.aget_state({"configurable": {"thread_id": "after-failure"}})
        ).values["catalog_ui"]
    )
    assert ui.pending_render is not None and ui.active_token is None
