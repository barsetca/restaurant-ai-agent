import os
from types import SimpleNamespace
from urllib.parse import urlparse
from uuid import uuid4

import pytest
from telegram.error import TimedOut

from app.agent.checkpoint import open_checkpoint_saver, setup_checkpoint_schema
from app.agent.graph import build_agent_graph
from app.agent.service import AgentService
from app.bot.catalog_runtime import run_catalog_update
from app.catalog_ui.callbacks import resolve_callback
from app.catalog_ui.models import CatalogScreen, CatalogUiState
from app.catalog_ui.rendering import (
    RenderData,
    acknowledge_render,
    prepare_render,
    render_screen,
)
from app.menu.contracts import CartInspection


class _HomeMenu:
    async def __aenter__(self) -> "_HomeMenu":
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def inspect_cart(self, items: object) -> CartInspection:
        del items
        return CartInspection(
            items=[], available_subtotal_minor=0, has_unavailable_items=False
        )


class _FailingAcknowledgementGraph:
    """Test-only proxy: prepare uses real saver; only acknowledgement write fails."""

    def __init__(self, graph: object) -> None:
        self._graph = graph

    async def ainvoke(self, *args: object, **kwargs: object) -> object:
        return await self._graph.ainvoke(*args, **kwargs)  # type: ignore[attr-defined]

    async def aget_state(self, *args: object, **kwargs: object) -> object:
        return await self._graph.aget_state(*args, **kwargs)  # type: ignore[attr-defined]

    async def aupdate_state(self, *args: object, **kwargs: object) -> object:
        del args, kwargs
        raise RuntimeError("synthetic acknowledgement checkpoint failure")


@pytest.mark.integration
async def test_catalog_checkpoint_restart() -> None:
    url = os.getenv("TEST_CHECKPOINT_DATABASE_URL", "")
    if not url:
        pytest.skip("TEST_CHECKPOINT_DATABASE_URL is not set")
    assert (
        "test" in urlparse(url.replace("postgresql+asyncpg", "postgresql")).path.lower()
    )
    thread = f"catalog-test:{uuid4().hex}"
    config = {"configurable": {"thread_id": thread}}
    await setup_checkpoint_schema(url)
    try:
        async with open_checkpoint_saver(url) as saver:
            graph = build_agent_graph(None, checkpointer=saver)
            await graph.aupdate_state(config, {"cart": [], "draft_revision": 3})
        async with open_checkpoint_saver(url) as saver:
            graph = build_agent_graph(None, checkpointer=saver)
            legacy = (await graph.aget_state(config)).values
            assert (
                CatalogUiState.from_checkpoint(legacy.get("catalog_ui"))
                == CatalogUiState()
            )
            assert legacy["cart"] == [] and legacy["draft_revision"] == 3
            ui = CatalogUiState(
                active_screen=CatalogScreen.DISH_LIST,
                requested_screen=CatalogScreen.DISH_LIST,
                category_id="synthetic",
                offset=5,
                categories_offset=20,
            )
            ui = acknowledge_render(
                prepare_render(
                    ui,
                    render_screen(ui, RenderData()),
                    token="token001",
                    draft_revision=3,
                ),
                token="token001",
                message_id=42,
            )
            ui = ui.model_copy(update={"requested_screen": CatalogScreen.CATEGORIES})
            ui = prepare_render(
                ui, render_screen(ui, RenderData()), token="token002", draft_revision=3
            )
            await graph.aupdate_state(config, {"catalog_ui": ui.to_checkpoint()})
        async with open_checkpoint_saver(url) as saver:
            graph = build_agent_graph(None, checkpointer=saver)
            restored = CatalogUiState.from_checkpoint(
                (await graph.aget_state(config)).values["catalog_ui"]
            )
            assert restored == ui
            pending = restored.pending_render
            assert (
                pending is not None
                and restored.active_screen == CatalogScreen.DISH_LIST
            )
            assert (
                resolve_callback(restored, pending.rendered.rows[0][0].callback_data)
                is None
            )
            old_payload = "cat:1:token001:0"
            with pytest.raises(ValueError):
                acknowledge_render(restored, token="wrong000")
            acknowledged = acknowledge_render(restored, token="token002", message_id=43)
            assert acknowledged.active_screen == CatalogScreen.CATEGORIES
            assert (
                acknowledged.pending_render is None
                and resolve_callback(acknowledged, old_payload) is None
            )
            with pytest.raises(ValueError):
                acknowledge_render(acknowledged, token="token001")
            await graph.aupdate_state(
                config, {"catalog_ui": acknowledged.to_checkpoint()}
            )
        async with open_checkpoint_saver(url) as saver:
            graph = build_agent_graph(None, checkpointer=saver)
            assert (
                CatalogUiState.from_checkpoint(
                    (await graph.aget_state(config)).values["catalog_ui"]
                )
                == acknowledged
            )
    finally:
        async with open_checkpoint_saver(url) as saver:
            await saver.adelete_thread(thread)


@pytest.mark.integration
async def test_catalog_prepare_replay_and_ack_use_official_checkpoint() -> None:
    url = os.getenv("TEST_CHECKPOINT_DATABASE_URL", "")
    if not url:
        pytest.skip("TEST_CHECKPOINT_DATABASE_URL is not set")
    assert (
        "test" in urlparse(url.replace("postgresql+asyncpg", "postgresql")).path.lower()
    )
    thread = f"catalog-lifecycle-test:{uuid4().hex}"
    config = {"configurable": {"thread_id": thread}}
    await setup_checkpoint_schema(url)
    try:
        async with open_checkpoint_saver(url) as saver:
            graph = build_agent_graph(None, checkpointer=saver)
            service = AgentService(graph, persistence_required=True)
            await graph.aupdate_state(
                config,
                {
                    "cart": [],
                    "contacts": {"name": "Synthetic"},
                    "draft_revision": 5,
                },
            )
            pending = prepare_render(
                CatalogUiState(),
                render_screen(CatalogUiState(), RenderData()),
                token="token101",
                draft_revision=5,
            )
            async with service.thread_lifecycle(thread):
                committed = await service.commit_catalog_unlocked(
                    thread, 100, pending.to_checkpoint()
                )
            assert committed.replayed is False

        async with open_checkpoint_saver(url) as saver:
            graph = build_agent_graph(None, checkpointer=saver)
            service = AgentService(graph, persistence_required=True)
            values = (await graph.aget_state(config)).values
            restored = CatalogUiState.from_checkpoint(values["catalog_ui"])
            assert restored.pending_render is not None
            assert values["highest_update_id"] == 100
            assert values["cart"] == [] and values["draft_revision"] == 5
            replacement = prepare_render(
                CatalogUiState(),
                render_screen(CatalogUiState(), RenderData()),
                token="token102",
                draft_revision=5,
            )
            async with service.thread_lifecycle(thread):
                replay = await service.commit_catalog_unlocked(
                    thread, 100, replacement.to_checkpoint()
                )
                assert replay.replayed is True
                still_pending = CatalogUiState.from_checkpoint(
                    (await service.checkpoint_values_unlocked(thread))["catalog_ui"]
                )
                assert still_pending.pending_render.token == "token101"
                acknowledged = acknowledge_render(
                    still_pending, token="token101", message_id=77
                )
                await service.acknowledge_catalog_unlocked(
                    thread, acknowledged.to_checkpoint()
                )

        async with open_checkpoint_saver(url) as saver:
            graph = build_agent_graph(None, checkpointer=saver)
            values = (await graph.aget_state(config)).values
            restored = CatalogUiState.from_checkpoint(values["catalog_ui"])
            assert restored.pending_render is None
            assert restored.active_message_id == 77
            assert restored.active_navigation is not None
            assert values["contacts"] == {"name": "Synthetic"}
    finally:
        async with open_checkpoint_saver(url) as saver:
            await saver.adelete_thread(thread)


@pytest.mark.integration
async def test_acknowledgement_checkpoint_failure_keeps_pending_after_restart() -> None:
    url = os.getenv("TEST_CHECKPOINT_DATABASE_URL", "")
    if not url:
        pytest.skip("TEST_CHECKPOINT_DATABASE_URL is not set")
    assert (
        "test" in urlparse(url.replace("postgresql+asyncpg", "postgresql")).path.lower()
    )
    thread = f"catalog-ack-failure-test:{uuid4().hex}"
    config = {"configurable": {"thread_id": thread}}
    bot = SimpleNamespace(
        send_message=pytest.importorskip("unittest.mock").AsyncMock(
            return_value=SimpleNamespace(message_id=901)
        ),
        edit_message_text=pytest.importorskip("unittest.mock").AsyncMock(),
    )
    business = {
        "cart": [{"dish_id": "11111111-1111-1111-1111-111111111111", "quantity": 1}],
        "contacts": {"name": "Synthetic"},
        "draft_revision": 13,
        "quote_snapshot": {"total_minor": 1234},
    }
    await setup_checkpoint_schema(url)
    try:
        async with open_checkpoint_saver(url) as saver:
            graph = build_agent_graph(None, checkpointer=saver)
            await graph.aupdate_state(config, business)
            service = AgentService(
                _FailingAcknowledgementGraph(graph), persistence_required=True
            )
            with pytest.raises(RuntimeError, match="acknowledgement checkpoint"):
                await run_catalog_update(
                    service=service,
                    menu_factory=_HomeMenu,
                    bot=bot,
                    chat_id=1,
                    conversation_id=thread,
                    update_id=801,
                    recovery=True,
                )
            assert bot.send_message.await_count == 1

        async with open_checkpoint_saver(url) as saver:
            graph = build_agent_graph(None, checkpointer=saver)
            values = (await graph.aget_state(config)).values
            ui = CatalogUiState.from_checkpoint(values["catalog_ui"])
            assert ui.pending_render is not None
            assert ui.active_token is None
            assert values["highest_update_id"] == 801
            assert any(
                entry.get("update_id") == 801 for entry in values["processed_updates"]
            )
            for key, value in business.items():
                assert values[key] == value
            assert bot.send_message.await_count == 1
    finally:
        async with open_checkpoint_saver(url) as saver:
            await saver.adelete_thread(thread)


@pytest.mark.integration
async def test_restart_recovery_replaces_pending_token_and_rejects_old_callback() -> (
    None
):
    url = os.getenv("TEST_CHECKPOINT_DATABASE_URL", "")
    if not url:
        pytest.skip("TEST_CHECKPOINT_DATABASE_URL is not set")
    assert (
        "test" in urlparse(url.replace("postgresql+asyncpg", "postgresql")).path.lower()
    )
    thread = f"catalog-recovery-test:{uuid4().hex}"
    config = {"configurable": {"thread_id": thread}}
    business = {
        "cart": [{"dish_id": "22222222-2222-2222-2222-222222222222", "quantity": 1}],
        "contacts": {"name": "Synthetic"},
        "draft_revision": 21,
    }
    first_bot = SimpleNamespace(
        send_message=pytest.importorskip("unittest.mock").AsyncMock(
            side_effect=TimedOut("synthetic")
        ),
        edit_message_text=pytest.importorskip("unittest.mock").AsyncMock(),
    )
    second_bot = SimpleNamespace(
        send_message=pytest.importorskip("unittest.mock").AsyncMock(
            return_value=SimpleNamespace(message_id=902)
        ),
        edit_message_text=pytest.importorskip("unittest.mock").AsyncMock(),
    )
    await setup_checkpoint_schema(url)
    try:
        async with open_checkpoint_saver(url) as saver:
            graph = build_agent_graph(None, checkpointer=saver)
            await graph.aupdate_state(config, business)
            service = AgentService(graph, persistence_required=True)
            assert (
                await run_catalog_update(
                    service=service,
                    menu_factory=_HomeMenu,
                    bot=first_bot,
                    chat_id=1,
                    conversation_id=thread,
                    update_id=901,
                    recovery=True,
                )
                == "unknown"
            )
            pending = CatalogUiState.from_checkpoint(
                (await graph.aget_state(config)).values["catalog_ui"]
            )
            assert pending.pending_render is not None
            old_token = pending.pending_render.token
            old_payload = pending.pending_render.rendered.rows[0][0].callback_data

        async with open_checkpoint_saver(url) as saver:
            graph = build_agent_graph(None, checkpointer=saver)
            service = AgentService(graph, persistence_required=True)
            assert (
                await run_catalog_update(
                    service=service,
                    menu_factory=_HomeMenu,
                    bot=second_bot,
                    chat_id=1,
                    conversation_id=thread,
                    update_id=902,
                    recovery=True,
                )
                == "delivered"
            )
            values = (await graph.aget_state(config)).values
            recovered = CatalogUiState.from_checkpoint(values["catalog_ui"])
            assert recovered.pending_render is None
            assert recovered.active_screen is CatalogScreen.HOME
            assert recovered.active_message_id == 902
            assert recovered.active_token != old_token
            assert old_token in recovered.retired_tokens
            assert resolve_callback(recovered, old_payload) is None
            for key, value in business.items():
                assert values[key] == value
            assert first_bot.send_message.await_count == 1
            assert second_bot.send_message.await_count == 1
    finally:
        async with open_checkpoint_saver(url) as saver:
            await saver.adelete_thread(thread)
