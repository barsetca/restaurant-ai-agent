import os
from urllib.parse import urlparse
from uuid import uuid4

import pytest

from app.agent.checkpoint import open_checkpoint_saver, setup_checkpoint_schema
from app.agent.graph import build_agent_graph
from app.catalog_ui.callbacks import resolve_callback
from app.catalog_ui.models import CatalogScreen, CatalogUiState
from app.catalog_ui.rendering import (
    RenderData,
    acknowledge_render,
    prepare_render,
    render_screen,
)


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
