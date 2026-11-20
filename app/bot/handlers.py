import asyncio
import logging
import re
from hashlib import sha256
from hmac import new as new_hmac
from time import perf_counter
from typing import Any

import httpx
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from app.agent.service import AgentResponse, AgentService
from app.bot.catalog_runtime import run_catalog_update
from app.catalog_ui.callbacks import decode_callback
from app.rag.answer_service import FALLBACK_MESSAGE

logger = logging.getLogger(__name__)
_CONNECT_RETRY_ATTEMPTS = 2
_CONNECT_RETRY_DELAY_SECONDS = 0.1


def _button(
    text: str,
    action: str,
    revision: int,
    confirmation_id: str | None = None,
) -> InlineKeyboardButton:
    callback_data = f"order:{action}:{revision}"
    if confirmation_id is not None:
        callback_data = f"{callback_data}:{confirmation_id}"
    return InlineKeyboardButton(text, callback_data=callback_data)


def _response_markup(result: AgentResponse) -> InlineKeyboardMarkup | None:
    revision = result.draft_revision
    if revision is None:
        return None
    if result.clarification_id is not None:
        return InlineKeyboardMarkup(
            [
                [
                    _button(
                        "Показать корзину",
                        "clarify_view_cart",
                        revision,
                        result.clarification_id,
                    ),
                    _button(
                        "Перейти к оформлению",
                        "clarify_checkout",
                        revision,
                        result.clarification_id,
                    ),
                ]
            ]
        )
    checkout_session_id = result.checkout_session_id
    if (
        result.order_stage in {"collecting_time", "collecting_edit_time"}
        and checkout_session_id is not None
    ):
        return InlineKeyboardMarkup(
            [
                [
                    _button(
                        "Как можно скорее", "time_asap", revision, checkout_session_id
                    ),
                    _button(
                        "Выбрать дату и время",
                        "time_manual",
                        revision,
                        checkout_session_id,
                    ),
                ],
                [
                    _button(
                        "Отменить оформление",
                        "cancel_checkout",
                        revision,
                        checkout_session_id,
                    )
                ],
            ]
        )
    if (
        result.order_stage
        in {
            "collecting_name",
            "collecting_phone",
            "collecting_address",
            "collecting_edit_name",
            "collecting_edit_phone",
            "collecting_edit_address",
        }
        and checkout_session_id is not None
    ):
        return InlineKeyboardMarkup(
            [
                [
                    _button(
                        "Отменить оформление",
                        "cancel_checkout",
                        revision,
                        checkout_session_id,
                    )
                ]
            ]
        )
    if result.order_stage == "awaiting_confirmation":
        return InlineKeyboardMarkup(
            [
                [
                    _button("Подтвердить", "confirm", revision),
                    _button("Изменить", "edit", revision),
                    _button("Отменить", "cancel", revision),
                ]
            ]
        )
    if result.order_stage == "awaiting_edit_choice":
        return InlineKeyboardMarkup(
            [
                [
                    _button("Корзина", "edit_cart", revision),
                    _button("Имя", "edit_name", revision),
                ],
                [
                    _button("Телефон", "edit_phone", revision),
                    _button("Адрес", "edit_address", revision),
                ],
                [_button("Время", "edit_time", revision)],
                [_button("Вернуться к итогу", "return_summary", revision)],
            ]
        )
    if result.order_stage == "editing_cart":
        return InlineKeyboardMarkup(
            [[_button("Вернуться к итогу", "return_summary", revision)]]
        )
    if result.order_stage == "awaiting_cancel_confirmation":
        confirmation_id = result.destructive_confirmation_id
        if confirmation_id is None:
            return None
        return InlineKeyboardMarkup(
            [
                [
                    _button("Да, отменить", "cancel_yes", revision, confirmation_id),
                    _button("Вернуться", "cancel_back", revision, confirmation_id),
                ]
            ]
        )
    if result.order_stage == "awaiting_new_order_confirmation":
        confirmation_id = result.destructive_confirmation_id
        if confirmation_id is None:
            return None
        return InlineKeyboardMarkup(
            [
                [
                    _button(
                        "Начать новый заказ",
                        "confirm_new_order",
                        revision,
                        confirmation_id,
                    ),
                    _button(
                        "Оставить текущий",
                        "keep_current_order",
                        revision,
                        confirmation_id,
                    ),
                ]
            ]
        )
    if result.order_stage == "awaiting_clear_confirmation":
        confirmation_id = result.destructive_confirmation_id
        if confirmation_id is None:
            return None
        return InlineKeyboardMarkup(
            [
                [
                    _button(
                        "Очистить корзину", "confirm_clear", revision, confirmation_id
                    ),
                    _button(
                        "Оставить текущий",
                        "keep_current_order",
                        revision,
                        confirmation_id,
                    ),
                ]
            ]
        )
    return None


def _is_pre_delivery_connect_error(exc: BaseException) -> bool:
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, httpx.ConnectError):
            return True
        current = current.__cause__ or current.__context__
    return False


async def safe_send_reply(
    target: Any,
    text: str,
    *,
    reply_markup: InlineKeyboardMarkup | None = None,
    operation: str,
) -> bool:
    for attempt in range(1, _CONNECT_RETRY_ATTEMPTS + 1):
        started = perf_counter()
        try:
            if reply_markup is None:
                await target.reply_text(text)
            else:
                await target.reply_text(text, reply_markup=reply_markup)
        except Exception as exc:
            retry = (
                _is_pre_delivery_connect_error(exc)
                and attempt < _CONNECT_RETRY_ATTEMPTS
            )
            logger.warning(
                "Telegram response delivery failed",
                extra={
                    "exception_type": type(exc).__name__,
                    "operation": operation,
                    "attempt": attempt,
                    "outcome": "retrying" if retry else "failed",
                    "duration_ms": round((perf_counter() - started) * 1000, 2),
                },
            )
            if not retry:
                return False
            await asyncio.sleep(_CONNECT_RETRY_DELAY_SECONDS)
            continue
        logger.info(
            "Telegram response delivered",
            extra={
                "operation": operation,
                "attempt": attempt,
                "outcome": "delivered",
                "duration_ms": round((perf_counter() - started) * 1000, 2),
            },
        )
        return True
    return False


def _telegram_identity(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> tuple[AgentService, str] | None:
    service = context.application.bot_data.get("agent_service")
    secret = context.application.bot_data.get("thread_id_hmac_secret")
    key_id = context.application.bot_data.get("thread_id_hmac_key_id", "v1")
    if (
        not isinstance(service, AgentService)
        or not isinstance(secret, str)
        or not secret
    ):
        return None
    conversation_id = conversation_id_for_update(update, secret, str(key_id))
    if conversation_id is None:
        return None
    return service, conversation_id


async def _serialized_agent_delivery(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    target: Any,
    message: str,
    *,
    operation: str,
    callback_action: str | None = None,
    callback_revision: int | None = None,
    callback_confirmation_id: str | None = None,
) -> AgentResponse:
    identity = _telegram_identity(update, context)
    if identity is None:
        result = AgentResponse(FALLBACK_MESSAGE, None, None, "agent_unavailable")
        await safe_send_reply(target, result.text, operation=operation)
        return result
    service, conversation_id = identity

    async def deliver(result: AgentResponse) -> object:
        return await safe_send_reply(
            target,
            result.text,
            reply_markup=_response_markup(result),
            operation=operation,
        )

    return await service.handle_message_result_and_deliver(
        message,
        conversation_id,
        getattr(update, "update_id", None),
        deliver,
        callback_action=callback_action,
        callback_revision=callback_revision,
        callback_confirmation_id=callback_confirmation_id,
    )


def conversation_id_for_update(
    update: Update,
    hmac_secret: str,
    key_id: str = "v1",
) -> str | None:
    chat = getattr(update, "effective_chat", None)
    chat_id = getattr(chat, "id", None)
    if chat_id is None and update.message is not None:
        chat_id = getattr(update.message, "chat_id", None)
    if chat_id is None:
        return None
    digest = new_hmac(
        hmac_secret.encode("utf-8"),
        f"telegram-chat:{chat_id}".encode("utf-8"),
        sha256,
    ).hexdigest()
    return f"telegram:{key_id}:{digest}"


async def agent_response(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    message: str,
    *,
    callback_action: str | None = None,
    callback_revision: int | None = None,
    callback_confirmation_id: str | None = None,
) -> AgentResponse:
    agent_service = context.application.bot_data.get("agent_service")
    if not isinstance(agent_service, AgentService):
        return AgentResponse(FALLBACK_MESSAGE, None, None, "agent_unavailable")
    try:
        hmac_secret = context.application.bot_data.get("thread_id_hmac_secret")
        key_id = context.application.bot_data.get("thread_id_hmac_key_id", "v1")
        if not isinstance(hmac_secret, str) or not hmac_secret:
            raise RuntimeError("Telegram thread identity is not configured")
        return await agent_service.handle_message_result(
            message,
            conversation_id=conversation_id_for_update(
                update, hmac_secret, str(key_id)
            ),
            update_id=getattr(update, "update_id", None),
            callback_action=callback_action,
            callback_revision=callback_revision,
            callback_confirmation_id=callback_confirmation_id,
        )
    except Exception as exc:
        logger.error(
            "Unhandled Telegram agent error",
            extra={"exception_type": type(exc).__name__},
        )
        return AgentResponse(FALLBACK_MESSAGE, None, None, "agent_error")


async def agent_answer(
    update: Update, context: ContextTypes.DEFAULT_TYPE, message: str
) -> str:
    return (await agent_response(update, context, message)).text


async def text_message_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if update.message is None or update.message.text is None:
        return
    await _serialized_agent_delivery(
        update, context, update.message, update.message.text, operation="text_response"
    )


async def menu_message_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if update.message is None:
        return
    identity = _telegram_identity(update, context)
    menu_factory = context.application.bot_data.get("catalog_menu_factory")
    bot = getattr(context, "bot", None) or getattr(context.application, "bot", None)
    chat_id = getattr(getattr(update, "effective_chat", None), "id", None)
    if (
        identity is None
        or not callable(menu_factory)
        or bot is None
        or not isinstance(chat_id, int)
    ):
        return
    service, conversation_id = identity
    try:
        await run_catalog_update(
            service=service,
            menu_factory=menu_factory,
            bot=bot,
            chat_id=chat_id,
            conversation_id=conversation_id,
            update_id=getattr(update, "update_id", None),
            recovery=True,
        )
    except Exception as exc:
        logger.error(
            "Catalog update failed",
            extra={"exception_type": type(exc).__name__, "operation": "catalog_menu"},
        )


async def catalog_callback_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    if query is None:
        return
    valid = decode_callback(getattr(query, "data", None)) is not None
    try:
        await query.answer(None if valid else "Кнопка устарела. Откройте «Меню» снова.")
    except Exception as exc:
        logger.warning(
            "Telegram callback acknowledgement failed",
            extra={
                "exception_type": type(exc).__name__,
                "operation": "catalog_callback_ack",
            },
        )
    if not valid or query.message is None:
        return
    identity = _telegram_identity(update, context)
    menu_factory = context.application.bot_data.get("catalog_menu_factory")
    bot = getattr(context, "bot", None) or getattr(context.application, "bot", None)
    chat_id = getattr(getattr(update, "effective_chat", None), "id", None)
    if (
        identity is None
        or not callable(menu_factory)
        or bot is None
        or not isinstance(chat_id, int)
    ):
        return
    service, conversation_id = identity
    try:
        await run_catalog_update(
            service=service,
            menu_factory=menu_factory,
            bot=bot,
            chat_id=chat_id,
            conversation_id=conversation_id,
            update_id=getattr(update, "update_id", None),
            payload=query.data,
            callback_message_id=getattr(query.message, "message_id", None),
        )
    except Exception as exc:
        logger.error(
            "Catalog callback failed",
            extra={
                "exception_type": type(exc).__name__,
                "operation": "catalog_callback",
            },
        )


async def order_callback_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    if query is None or query.message is None or not isinstance(query.data, str):
        return
    try:
        parts = query.data.split(":")
        if len(parts) not in {3, 4}:
            return
        prefix, action, raw_revision = parts[:3]
        if prefix != "order":
            return
        revision = int(raw_revision)
        confirmation_id = parts[3] if len(parts) == 4 else None
        if confirmation_id is not None and not re.fullmatch(
            r"[A-Za-z0-9_-]{12}", confirmation_id
        ):
            return
    except (TypeError, ValueError):
        return
    try:
        await query.answer()
    except Exception as exc:
        logger.warning(
            "Telegram callback acknowledgement failed",
            extra={
                "exception_type": type(exc).__name__,
                "operation": "callback_ack",
                "attempt": 1,
                "outcome": "failed",
                "duration_ms": 0.0,
            },
        )
    message = {
        "confirm": "Подтверждаю",
        "edit": "Изменить заказ",
        "cancel": "Отмена",
        "time_asap": "Как можно скорее",
        "time_manual": "Выбрать дату и время",
        "confirm_new_order": "Начать новый заказ",
        "confirm_clear": "Очистить корзину",
        "keep_current_order": "Оставить текущий",
        "clarify_view_cart": "Показать корзину",
        "clarify_checkout": "Перейти к оформлению",
        "cancel_checkout": "Отменить оформление",
    }.get(action, "")
    await _serialized_agent_delivery(
        update,
        context,
        query.message,
        message,
        operation="callback_response",
        callback_action=action,
        callback_revision=revision,
        callback_confirmation_id=confirmation_id,
    )


# Backward-compatible import name used by existing tests.
time_choice_callback_handler = order_callback_handler


async def telegram_error_handler(
    update: object, context: ContextTypes.DEFAULT_TYPE
) -> None:
    del update
    error = getattr(context, "error", None)
    logger.error(
        "Unhandled Telegram application error",
        extra={
            "exception_type": type(error).__name__
            if error is not None
            else "UnknownError",
            "operation": "application_update",
            "attempt": 1,
            "outcome": "failed",
            "duration_ms": 0.0,
        },
    )
