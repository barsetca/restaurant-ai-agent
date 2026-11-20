from types import SimpleNamespace
from unittest.mock import AsyncMock

from telegram.error import BadRequest, TimedOut

from app.bot.catalog_delivery import (
    CatalogDeliveryOutcome,
    deliver_catalog_screen,
)
from app.catalog_ui.models import CatalogUiState
from app.catalog_ui.rendering import RenderData, prepare_render, render_screen


def pending():
    state = CatalogUiState()
    return prepare_render(
        state, render_screen(state, RenderData()), token="token001"
    ).pending_render


async def test_edit_success_uses_confirmed_message_id() -> None:
    bot = SimpleNamespace(
        edit_message_text=AsyncMock(return_value=SimpleNamespace(message_id=42)),
        send_message=AsyncMock(),
    )
    result = await deliver_catalog_screen(
        bot, chat_id=1, pending=pending(), active_message_id=42
    )
    assert result.outcome is CatalogDeliveryOutcome.SUCCESS
    assert result.message_id == 42
    bot.send_message.assert_not_awaited()


async def test_definitive_edit_rejection_allows_one_send_fallback() -> None:
    bot = SimpleNamespace(
        edit_message_text=AsyncMock(
            side_effect=BadRequest("Message to edit not found")
        ),
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=99)),
    )
    result = await deliver_catalog_screen(
        bot, chat_id=1, pending=pending(), active_message_id=42
    )
    assert result.outcome is CatalogDeliveryOutcome.SUCCESS
    assert (result.operation, result.message_id) == ("send", 99)


async def test_not_modified_and_timeout_are_unknown_without_fallback() -> None:
    for error in (BadRequest("Message is not modified"), TimedOut("synthetic")):
        bot = SimpleNamespace(
            edit_message_text=AsyncMock(side_effect=error), send_message=AsyncMock()
        )
        result = await deliver_catalog_screen(
            bot, chat_id=1, pending=pending(), active_message_id=42
        )
        assert result.outcome is CatalogDeliveryOutcome.UNKNOWN
        bot.send_message.assert_not_awaited()


async def test_send_requires_actual_message_id() -> None:
    bot = SimpleNamespace(
        edit_message_text=AsyncMock(), send_message=AsyncMock(return_value=True)
    )
    result = await deliver_catalog_screen(
        bot, chat_id=1, pending=pending(), active_message_id=None
    )
    assert result.outcome is CatalogDeliveryOutcome.UNKNOWN
