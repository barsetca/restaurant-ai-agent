import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest

from app.catalog_ui.models import PendingRender, RenderedScreen

logger = logging.getLogger(__name__)


class CatalogDeliveryOutcome(StrEnum):
    SUCCESS = "success"
    DEFINITIVE_REJECTION = "definitive_rejection"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class CatalogDeliveryResult:
    outcome: CatalogDeliveryOutcome
    message_id: int | None = None
    operation: str | None = None


def catalog_markup(screen: RenderedScreen) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(button.label, callback_data=button.callback_data)
                for button in row
            ]
            for row in screen.rows
        ]
    )


def _definitive_edit_rejection(exc: BadRequest) -> bool:
    text = str(exc).casefold()
    if "message is not modified" in text:
        return False
    return any(
        marker in text
        for marker in (
            "message to edit not found",
            "message can't be edited",
            "message can not be edited",
            "message identifier is not specified",
        )
    )


async def deliver_catalog_screen(
    bot: Any,
    *,
    chat_id: int,
    pending: PendingRender,
    active_message_id: int | None,
) -> CatalogDeliveryResult:
    """Deliver once; ambiguous Telegram outcomes never trigger a fallback."""
    screen = pending.rendered
    markup = catalog_markup(screen)
    if active_message_id is not None:
        try:
            edited = await bot.edit_message_text(
                chat_id=chat_id,
                message_id=active_message_id,
                text=screen.text,
                parse_mode=screen.parse_mode,
                reply_markup=markup,
            )
        except BadRequest as exc:
            if not _definitive_edit_rejection(exc):
                logger.warning(
                    "Catalog edit outcome is unknown",
                    extra={
                        "exception_type": type(exc).__name__,
                        "operation": "catalog_edit",
                    },
                )
                return CatalogDeliveryResult(
                    CatalogDeliveryOutcome.UNKNOWN, operation="edit"
                )
        except Exception as exc:
            logger.warning(
                "Catalog edit outcome is unknown",
                extra={
                    "exception_type": type(exc).__name__,
                    "operation": "catalog_edit",
                },
            )
            return CatalogDeliveryResult(
                CatalogDeliveryOutcome.UNKNOWN, operation="edit"
            )
        else:
            message_id = getattr(edited, "message_id", None)
            if not isinstance(message_id, int):
                message_id = active_message_id
            return CatalogDeliveryResult(
                CatalogDeliveryOutcome.SUCCESS, message_id=message_id, operation="edit"
            )

    try:
        sent = await bot.send_message(
            chat_id=chat_id,
            text=screen.text,
            parse_mode=screen.parse_mode,
            reply_markup=markup,
        )
    except Exception as exc:
        logger.warning(
            "Catalog send outcome is unknown",
            extra={"exception_type": type(exc).__name__, "operation": "catalog_send"},
        )
        return CatalogDeliveryResult(CatalogDeliveryOutcome.UNKNOWN, operation="send")
    message_id = getattr(sent, "message_id", None)
    if not isinstance(message_id, int):
        return CatalogDeliveryResult(CatalogDeliveryOutcome.UNKNOWN, operation="send")
    return CatalogDeliveryResult(
        CatalogDeliveryOutcome.SUCCESS, message_id=message_id, operation="send"
    )
