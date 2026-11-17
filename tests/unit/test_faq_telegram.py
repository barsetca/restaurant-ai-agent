from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.agent.service import AgentService
from app.bot.bootstrap import build_telegram_application, start_command
from app.bot.handlers import conversation_id_for_update, text_message_handler

_HMAC_SECRET = "synthetic-hmac-secret-at-least-32-characters"


def _agent_service(response: str = "Ресторан работает до 22:00.") -> tuple[AgentService, AsyncMock]:
    graph = AsyncMock()
    graph.ainvoke.return_value = {"response": response}
    return AgentService(graph), graph


def test_bootstrap_reuses_single_application_and_registers_agent_handler() -> None:
    service, _ = _agent_service()
    application = build_telegram_application(
        "123456:synthetic-token",
        service,
        thread_id_hmac_secret=_HMAC_SECRET,
    )
    assert application.bot_data["agent_service"] is service
    assert len(application.handlers[0]) == 2


async def test_text_message_is_answered_through_agent_service() -> None:
    service, graph = _agent_service()
    message = SimpleNamespace(
        text="До какого времени вы открыты?",
        chat_id=111,
        reply_text=AsyncMock(),
    )
    update = SimpleNamespace(message=message, effective_chat=SimpleNamespace(id=111))
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
    assert "111" not in state["conversation_id"]


async def test_start_command_behavior_is_unchanged() -> None:
    message = SimpleNamespace(reply_text=AsyncMock())
    update = SimpleNamespace(message=message)

    await start_command(update, SimpleNamespace())

    message.reply_text.assert_awaited_once_with("Restaurant AI Agent is running.")


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
