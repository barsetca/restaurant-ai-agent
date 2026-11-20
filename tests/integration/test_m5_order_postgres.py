import os
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import urlparse
from uuid import uuid4

import pytest
import httpx
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.agent.checkpoint import open_checkpoint_saver, setup_checkpoint_schema
from app.agent.graph import build_agent_graph
from app.agent.service import AgentService
from app.db.models import MenuItem, RestaurantSetting
from app.mcp.client import MenuMcpClient
from app.bot.handlers import safe_send_reply
from app.orders.contracts import ActionOutcome, ContextualOutcome
from app.orders.dialog import CONFIRMED_MESSAGE, OrderDialog
from app.orders.settings import DELIVERY_FEE_KEY, OrderSettingsRepository


def _database_url() -> str:
    url = os.getenv("TEST_MCP_DATABASE_URL", "")
    if not url:
        pytest.skip("TEST_MCP_DATABASE_URL is not set")
    database_name = urlparse(
        url.replace("postgresql+asyncpg", "postgresql")
    ).path.lower()
    if "test" not in database_name:
        pytest.fail("TEST_MCP_DATABASE_URL must use a database containing 'test'")
    return url


class ScriptedExtractor:
    def __init__(self, dish_name: str) -> None:
        self.dish_name = dish_name
        self.messages: list[str] = []

    async def extract(self, message: str, context: object) -> ContextualOutcome:
        del context
        self.messages.append(message)
        if message == "добавь тестовое блюдо":
            return ActionOutcome(
                action="add", items=[{"name": self.dish_name, "quantity": 2}]
            )
        return ActionOutcome(action="faq")


class Faq:
    async def answer(self, question: str) -> str:
        del question
        return "Синтетический FAQ-ответ"


@pytest.mark.integration
async def test_m5_real_mcp_settings_checkpoint_and_replay_without_side_effects() -> (
    None
):
    database_url = _database_url()
    engine = create_async_engine(database_url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    item_id = uuid4()
    marker = uuid4().hex
    thread_id = f"m5-test:{marker}"
    previous_setting: str | None = None

    async with sessions() as session:
        previous_setting = await session.scalar(
            select(RestaurantSetting.value).where(
                RestaurantSetting.key == DELIVERY_FEE_KEY
            )
        )
        session.add(
            MenuItem(
                id=item_id,
                source_row_id=f"m5-test-{marker}",
                name=f"M5 синтетическое блюдо {marker[:8]}",
                description="Только для изолированного теста",
                ingredients=["тест"],
                allergens=[],
                dietary_tags=["vegan"],
                category="test",
                price_minor=1000,
                currency="EUR",
                available=True,
            )
        )
        await session.merge(RestaurantSetting(key=DELIVERY_FEE_KEY, value="350"))
        await session.commit()

    extractor = ScriptedExtractor(f"M5 синтетическое блюдо {marker[:8]}")

    def menu_factory():
        return MenuMcpClient(database_url)

    dialog = OrderDialog(menu_factory, OrderSettingsRepository(engine), extractor)
    await setup_checkpoint_schema(database_url)
    try:
        async with open_checkpoint_saver(database_url) as saver:
            graph = build_agent_graph(Faq(), checkpointer=saver, order_dialog=dialog)
            service = AgentService(graph, persistence_required=True)
            assert "Добавлено" in await service.handle_message(
                "добавь тестовое блюдо", thread_id, update_id=700
            )
            assert "Добавлено" in await service.handle_message(
                "добавь тестовое блюдо", thread_id, update_id=700
            )
            assert "Корзина" in await service.handle_message(
                "корзина", thread_id, update_id=701
            )
            assert "Как вас зовут" in await service.handle_message(
                "давай оформим", thread_id, update_id=702
            )
            await service.handle_message("Иван Тестов", thread_id, update_id=703)
            await service.handle_message("+34600000001", thread_id, update_id=704)
            await service.handle_message("Calle Sintética 10", thread_id, update_id=705)
            summary = await service.handle_message_result(
                "как можно скорее", thread_id, update_id=706
            )
            assert "23,50 EUR" in summary.text
            initial_revision = summary.draft_revision
            assert initial_revision is not None

            edit = await service.handle_message_result(
                "Изменить заказ",
                thread_id,
                update_id=707,
                callback_action="edit",
                callback_revision=initial_revision,
            )
            assert edit.order_stage == "awaiting_edit_choice"
            edit_name = await service.handle_message_result(
                "",
                thread_id,
                update_id=708,
                callback_action="edit_name",
                callback_revision=initial_revision,
            )
            assert edit_name.order_stage == "collecting_edit_name"
            revised = await service.handle_message_result(
                "Мария Тестова", thread_id, update_id=709
            )
            assert revised.order_stage == "awaiting_confirmation"
            assert revised.draft_revision == initial_revision + 1

            stale = await service.handle_message_result(
                "Подтверждаю",
                thread_id,
                update_id=710,
                callback_action="confirm",
                callback_revision=initial_revision,
            )
            assert stale.error_code == "stale_callback"

            cancel = await service.handle_message_result(
                "Отмена",
                thread_id,
                update_id=711,
                callback_action="cancel",
                callback_revision=revised.draft_revision,
            )
            assert cancel.order_stage == "awaiting_cancel_confirmation"
            assert cancel.destructive_confirmation_id is not None
            back = await service.handle_message_result(
                "",
                thread_id,
                update_id=712,
                callback_action="cancel_back",
                callback_revision=revised.draft_revision,
                callback_confirmation_id=cancel.destructive_confirmation_id,
            )
            assert back.order_stage == "awaiting_confirmation"
            confirmed = await service.handle_message_result(
                "Подтверждаю",
                thread_id,
                update_id=713,
                callback_action="confirm",
                callback_revision=revised.draft_revision,
            )
            assert confirmed.text == CONFIRMED_MESSAGE

            state = await graph.aget_state({"configurable": {"thread_id": thread_id}})
            assert state.values["cart"][0]["quantity"] == 2
            assert state.values["confirmed_order"]["total_minor"] == 2350
            assert state.values["confirmed_order"]["customer_name"] == "Мария Тестова"
            first_confirmed = deepcopy(state.values["confirmed_order"])

            failed_target = SimpleNamespace(
                reply_text=AsyncMock(side_effect=httpx.ConnectError("synthetic"))
            )
            assert (
                await safe_send_reply(
                    failed_target, confirmed.text, operation="integration_confirm"
                )
                is False
            )
            after_failed_send = await graph.aget_state(
                {"configurable": {"thread_id": thread_id}}
            )
            assert after_failed_send.values["confirmed_order"] == first_confirmed

            assert (
                await service.handle_message("Подтверждаю", thread_id, update_id=713)
                == CONFIRMED_MESSAGE
            )
            replayed = await graph.aget_state(
                {"configurable": {"thread_id": thread_id}}
            )
            assert replayed.values["confirmed_order"] == first_confirmed

            restarted_graph = build_agent_graph(
                Faq(), checkpointer=saver, order_dialog=dialog
            )
            restarted_service = AgentService(restarted_graph, persistence_required=True)
            assert (
                await restarted_service.handle_message(
                    "Подтверждаю", thread_id, update_id=714
                )
                == CONFIRMED_MESSAGE
            )
            after_restart = await restarted_graph.aget_state(
                {"configurable": {"thread_id": thread_id}}
            )
            assert after_restart.values["confirmed_order"] == first_confirmed

            assert "Новый заказ начат" in await restarted_service.handle_message(
                "Новый заказ", thread_id, update_id=715
            )
            assert "Добавлено" in await restarted_service.handle_message(
                "добавь тестовое блюдо", thread_id, update_id=716
            )
            await restarted_service.handle_message(
                "давай оформим", thread_id, update_id=717
            )
            await restarted_service.handle_message(
                "Петр Тестов", thread_id, update_id=718
            )
            await restarted_service.handle_message(
                "+34600000002", thread_id, update_id=719
            )
            await restarted_service.handle_message(
                "Calle Sintética 20", thread_id, update_id=720
            )
            await restarted_service.handle_message(
                "как можно скорее", thread_id, update_id=721
            )
            assert (
                await restarted_service.handle_message(
                    "Подтверждаю", thread_id, update_id=722
                )
                == CONFIRMED_MESSAGE
            )
            second_state = await restarted_graph.aget_state(
                {"configurable": {"thread_id": thread_id}}
            )
            assert second_state.values["confirmed_order_archive"] == [first_confirmed]
            assert (
                second_state.values["confirmed_order"]["confirmation_key"]
                != (first_confirmed["confirmation_key"])
            )
            assert (
                second_state.values["confirmed_order"]["customer_name"] == "Петр Тестов"
            )
            assert "Иван Тестов" not in extractor.messages
            assert "+34600000001" not in extractor.messages
            await saver.adelete_thread(thread_id)
    finally:
        async with sessions() as session:
            await session.execute(delete(MenuItem).where(MenuItem.id == item_id))
            if previous_setting is None:
                await session.execute(
                    delete(RestaurantSetting).where(
                        RestaurantSetting.key == DELIVERY_FEE_KEY
                    )
                )
            else:
                await session.merge(
                    RestaurantSetting(key=DELIVERY_FEE_KEY, value=previous_setting)
                )
            await session.commit()
        await engine.dispose()
