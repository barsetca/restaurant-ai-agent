from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
from telegram.error import TimedOut

from app.agent.service import AgentService
from app.bot.bootstrap import build_telegram_application, start_command
from app.bot.handlers import (
    conversation_id_for_update,
    safe_send_reply,
    text_message_handler,
    time_choice_callback_handler,
)

_HMAC_SECRET = "synthetic-hmac-secret-at-least-32-characters"


def _agent_service(
    response: str = "Ресторан работает до 22:00.",
    *,
    stage: str | None = None,
    revision: int | None = None,
    confirmation_id: str | None = None,
    checkout_session_id: str | None = None,
) -> tuple[AgentService, AsyncMock]:
    graph = AsyncMock()
    graph.ainvoke.return_value = {
        "response": response,
        "order_stage": stage,
        "draft_revision": revision,
        "checkout_session_id": checkout_session_id,
        "pending_destructive_action": (
            {"confirmation_id": confirmation_id} if confirmation_id is not None else None
        ),
    }
    return AgentService(graph), graph


def test_bootstrap_reuses_single_application_and_registers_agent_handler() -> None:
    service, _ = _agent_service()
    application = build_telegram_application(
        "123456:synthetic-token",
        service,
        thread_id_hmac_secret=_HMAC_SECRET,
    )
    assert application.bot_data["agent_service"] is service
    assert len(application.handlers[0]) == 3
    assert len(application.error_handlers) == 1


async def test_text_message_is_answered_through_agent_service() -> None:
    service, graph = _agent_service()
    message = SimpleNamespace(
        text="До какого времени вы открыты?",
        chat_id=111,
        reply_text=AsyncMock(),
    )
    update = SimpleNamespace(message=message, effective_chat=SimpleNamespace(id=111), update_id=987)
    context = SimpleNamespace(
        application=SimpleNamespace(
            bot_data={
                "agent_service": service,
                "thread_id_hmac_secret": _HMAC_SECRET,
                "thread_id_hmac_key_id": "v1",
            }
        )
    )

    await text_message_handler(update, context)

    message.reply_text.assert_awaited_once_with("Ресторан работает до 22:00.")
    state = graph.ainvoke.await_args.args[0]
    assert state["user_message"] == "До какого времени вы открыты?"
    assert state["conversation_id"] == conversation_id_for_update(update, _HMAC_SECRET)
    assert state["update_id"] == 987
    assert "111" not in state["conversation_id"]


async def test_start_command_behavior_is_unchanged() -> None:
    message = SimpleNamespace(reply_text=AsyncMock())
    update = SimpleNamespace(message=message)

    await start_command(update, SimpleNamespace())

    message.reply_text.assert_awaited_once_with("Restaurant AI Agent is running.")


async def test_start_command_uses_agent_state_when_service_is_available() -> None:
    service, graph = _agent_service("Подтвержденный заказ сохранен.")
    message = SimpleNamespace(reply_text=AsyncMock())
    update = SimpleNamespace(
        message=message, effective_chat=SimpleNamespace(id=111), update_id=1001
    )
    context = SimpleNamespace(
        application=SimpleNamespace(
            bot_data={
                "agent_service": service,
                "thread_id_hmac_secret": _HMAC_SECRET,
                "thread_id_hmac_key_id": "v1",
            }
        )
    )
    await start_command(update, context)
    message.reply_text.assert_awaited_once_with("Подтвержденный заказ сохранен.")
    assert graph.ainvoke.await_args.args[0]["user_message"] == "/start"


async def test_delivery_time_callback_is_forwarded_to_same_agent_thread() -> None:
    service, graph = _agent_service("Введите дату и время в формате ДД.ММ.ГГГГ ЧЧ:ММ.")
    callback_message = SimpleNamespace(reply_text=AsyncMock())
    query = SimpleNamespace(
        data="order:time_manual:4:session00001",
        message=callback_message,
        answer=AsyncMock(),
    )
    update = SimpleNamespace(
        callback_query=query,
        message=None,
        effective_chat=SimpleNamespace(id=111),
        update_id=1002,
    )
    context = SimpleNamespace(
        application=SimpleNamespace(
            bot_data={
                "agent_service": service,
                "thread_id_hmac_secret": _HMAC_SECRET,
                "thread_id_hmac_key_id": "v1",
            }
        )
    )
    await time_choice_callback_handler(update, context)
    query.answer.assert_awaited_once()
    assert graph.ainvoke.await_args.args[0]["user_message"] == "Выбрать дату и время"
    assert graph.ainvoke.await_args.args[0]["callback_action"] == "time_manual"
    assert graph.ainvoke.await_args.args[0]["callback_revision"] == 4
    assert (
        graph.ainvoke.await_args.args[0]["callback_confirmation_id"]
        == "session00001"
    )
    callback_message.reply_text.assert_awaited_once()


async def test_time_prompt_adds_two_inline_buttons() -> None:
    service, _ = _agent_service(
        "Выберите время доставки: «Как можно скорее» или «Выбрать дату и время».",
        stage="collecting_time",
        revision=3,
        checkout_session_id="session00001",
    )
    message = SimpleNamespace(text="адрес", chat_id=111, reply_text=AsyncMock())
    update = SimpleNamespace(
        message=message, effective_chat=SimpleNamespace(id=111), update_id=1003
    )
    context = SimpleNamespace(
        application=SimpleNamespace(
            bot_data={
                "agent_service": service,
                "thread_id_hmac_secret": _HMAC_SECRET,
                "thread_id_hmac_key_id": "v1",
            }
        )
    )
    await text_message_handler(update, context)
    markup = message.reply_text.await_args.kwargs["reply_markup"]
    assert [button.text for button in markup.inline_keyboard[0]] == [
        "Как можно скорее",
        "Выбрать дату и время",
    ]
    assert [button.text for button in markup.inline_keyboard[1]] == [
        "Отменить оформление"
    ]
    assert markup.inline_keyboard[0][0].callback_data.endswith(":session00001")
    assert markup.inline_keyboard[1][0].callback_data == (
        "order:cancel_checkout:3:session00001"
    )


async def test_confirmation_summary_adds_revision_bound_actions() -> None:
    service, _ = _agent_service(
        "Проверьте заказ:", stage="awaiting_confirmation", revision=7
    )
    message = SimpleNamespace(text="как можно скорее", chat_id=111, reply_text=AsyncMock())
    update = SimpleNamespace(
        message=message, effective_chat=SimpleNamespace(id=111), update_id=1004
    )
    context = SimpleNamespace(
        application=SimpleNamespace(
            bot_data={
                "agent_service": service,
                "thread_id_hmac_secret": _HMAC_SECRET,
                "thread_id_hmac_key_id": "v1",
            }
        )
    )
    await text_message_handler(update, context)
    buttons = message.reply_text.await_args.kwargs["reply_markup"].inline_keyboard[0]
    assert [button.text for button in buttons] == ["Подтвердить", "Изменить", "Отменить"]
    assert [button.callback_data for button in buttons] == [
        "order:confirm:7", "order:edit:7", "order:cancel:7"
    ]


async def test_new_order_warning_adds_revision_bound_safety_actions() -> None:
    service, _ = _agent_service(
        "Текущий черновик и корзина будут очищены.",
        stage="awaiting_new_order_confirmation",
        revision=8,
        confirmation_id="session00001",
    )
    message = SimpleNamespace(text="Новый заказ", chat_id=111, reply_text=AsyncMock())
    update = SimpleNamespace(
        message=message, effective_chat=SimpleNamespace(id=111), update_id=1005
    )
    context = SimpleNamespace(
        application=SimpleNamespace(
            bot_data={
                "agent_service": service,
                "thread_id_hmac_secret": _HMAC_SECRET,
                "thread_id_hmac_key_id": "v1",
            }
        )
    )
    await text_message_handler(update, context)
    buttons = message.reply_text.await_args.kwargs["reply_markup"].inline_keyboard[0]
    assert [button.text for button in buttons] == [
        "Начать новый заказ",
        "Оставить текущий",
    ]
    assert [button.callback_data for button in buttons] == [
        "order:confirm_new_order:8:session00001",
        "order:keep_current_order:8:session00001",
    ]


async def test_clear_warning_adds_revision_bound_safety_actions() -> None:
    service, _ = _agent_service(
        "Текущая корзина будет очищена.",
        stage="awaiting_clear_confirmation",
        revision=9,
        confirmation_id="session00002",
    )
    message = SimpleNamespace(text="Очисти корзину", chat_id=111, reply_text=AsyncMock())
    update = SimpleNamespace(
        message=message, effective_chat=SimpleNamespace(id=111), update_id=1006
    )
    context = SimpleNamespace(
        application=SimpleNamespace(
            bot_data={
                "agent_service": service,
                "thread_id_hmac_secret": _HMAC_SECRET,
                "thread_id_hmac_key_id": "v1",
            }
        )
    )
    await text_message_handler(update, context)
    buttons = message.reply_text.await_args.kwargs["reply_markup"].inline_keyboard[0]
    assert [button.text for button in buttons] == ["Очистить корзину", "Оставить текущий"]
    assert [button.callback_data for button in buttons] == [
        "order:confirm_clear:9:session00002",
        "order:keep_current_order:9:session00002",
    ]


async def test_destructive_callback_forwards_confirmation_session() -> None:
    service, graph = _agent_service("Новый заказ начат.")
    callback_message = SimpleNamespace(reply_text=AsyncMock())
    query = SimpleNamespace(
        data="order:confirm_new_order:4:session00003",
        message=callback_message,
        answer=AsyncMock(),
    )
    update = SimpleNamespace(
        callback_query=query,
        message=None,
        effective_chat=SimpleNamespace(id=111),
        update_id=1007,
    )
    context = SimpleNamespace(
        application=SimpleNamespace(
            bot_data={
                "agent_service": service,
                "thread_id_hmac_secret": _HMAC_SECRET,
                "thread_id_hmac_key_id": "v1",
            }
        )
    )
    await time_choice_callback_handler(update, context)
    state = graph.ainvoke.await_args.args[0]
    assert state["callback_action"] == "confirm_new_order"
    assert state["callback_revision"] == 4
    assert state["callback_confirmation_id"] == "session00003"


async def test_safe_send_retries_connect_error_then_succeeds(caplog) -> None:
    target = SimpleNamespace(
        reply_text=AsyncMock(side_effect=[httpx.ConnectError("synthetic"), None])
    )
    assert await safe_send_reply(target, "safe response", operation="test_send") is True
    assert target.reply_text.await_count == 2
    assert "safe response" not in caplog.text


async def test_safe_send_exhausts_connect_retry_without_raising(caplog) -> None:
    target = SimpleNamespace(
        reply_text=AsyncMock(side_effect=httpx.ConnectError("synthetic"))
    )
    assert await safe_send_reply(target, "private synthetic text", operation="test_send") is False
    assert target.reply_text.await_count == 2
    assert "private synthetic text" not in caplog.text


async def test_safe_send_does_not_retry_ambiguous_timeout(caplog) -> None:
    target = SimpleNamespace(reply_text=AsyncMock(side_effect=TimedOut("synthetic")))
    assert await safe_send_reply(target, "private synthetic text", operation="test_send") is False
    assert target.reply_text.await_count == 1
    assert "private synthetic text" not in caplog.text


def test_conversation_identity_is_stable_and_chat_specific() -> None:
    first = SimpleNamespace(message=None, effective_chat=SimpleNamespace(id=111))
    same = SimpleNamespace(message=None, effective_chat=SimpleNamespace(id=111))
    other = SimpleNamespace(message=None, effective_chat=SimpleNamespace(id=222))

    assert conversation_id_for_update(first, _HMAC_SECRET) == conversation_id_for_update(
        same, _HMAC_SECRET
    )
    assert conversation_id_for_update(first, _HMAC_SECRET) != conversation_id_for_update(
        other, _HMAC_SECRET
    )
    assert conversation_id_for_update(first, _HMAC_SECRET).startswith("telegram:v1:")
    assert "111" not in conversation_id_for_update(first, _HMAC_SECRET)


def test_conversation_identity_changes_with_key_version_or_secret() -> None:
    update = SimpleNamespace(message=None, effective_chat=SimpleNamespace(id=111))
    first = conversation_id_for_update(update, _HMAC_SECRET, "v1")
    rotated = conversation_id_for_update(
        update,
        "rotated-synthetic-secret-at-least-32-characters",
        "v2",
    )
    assert first != rotated
    assert rotated.startswith("telegram:v2:")
