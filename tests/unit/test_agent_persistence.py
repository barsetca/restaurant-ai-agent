import asyncio
import logging
from unittest.mock import AsyncMock

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from app.agent.graph import ORDER_PLACEHOLDER_MESSAGE, build_agent_graph
from app.agent.service import AgentService, MissingConversationIdError
from app.rag.answer_service import FALLBACK_MESSAGE


def _config(thread_id: str) -> dict[str, dict[str, str]]:
    return {"configurable": {"thread_id": thread_id}}


async def test_checkpoint_restores_state_and_increments_turn_count() -> None:
    assistant = AsyncMock()
    assistant.answer.side_effect = ["Первый ответ", "Второй ответ"]
    saver = InMemorySaver()
    graph = build_agent_graph(assistant, checkpointer=saver)
    service = AgentService(graph, persistence_required=True)

    assert await service.handle_message("Первый вопрос", "thread-a") == "Первый ответ"
    first = await graph.aget_state(_config("thread-a"))
    assert first.values["turn_count"] == 1

    assert await service.handle_message("Второй вопрос", "thread-a") == "Второй ответ"
    second = await graph.aget_state(_config("thread-a"))
    assert second.values["turn_count"] == 2
    assert second.values["user_message"] == "Второй вопрос"
    assert second.values["conversation_id"] == "thread-a"


async def test_threads_are_isolated_and_service_can_be_recreated() -> None:
    assistant = AsyncMock()
    assistant.answer.side_effect = ["A1", "B1", "A2"]
    saver = InMemorySaver()
    first_graph = build_agent_graph(assistant, checkpointer=saver)
    first_service = AgentService(first_graph, persistence_required=True)

    assert await first_service.handle_message("A first", "thread-a") == "A1"
    assert await first_service.handle_message("B first", "thread-b") == "B1"

    second_graph = build_agent_graph(assistant, checkpointer=saver)
    second_service = AgentService(second_graph, persistence_required=True)
    assert await second_service.handle_message("A second", "thread-a") == "A2"

    state_a = await second_graph.aget_state(_config("thread-a"))
    state_b = await second_graph.aget_state(_config("thread-b"))
    assert state_a.values["turn_count"] == 2
    assert state_b.values["turn_count"] == 1
    assert state_a.values["user_message"] != state_b.values["user_message"]


async def test_current_faq_error_never_returns_previous_response() -> None:
    assistant = AsyncMock()
    assistant.answer.side_effect = ["Старый успешный ответ", RuntimeError("synthetic")]
    saver = InMemorySaver()
    graph = build_agent_graph(assistant, checkpointer=saver)
    service = AgentService(graph, persistence_required=True)

    assert (
        await service.handle_message("Первый вопрос", "thread-error")
        == "Старый успешный ответ"
    )
    assert (
        await service.handle_message("Новый вопрос", "thread-error") == FALLBACK_MESSAGE
    )

    state = await graph.aget_state(_config("thread-error"))
    assert state.values["response"] == FALLBACK_MESSAGE
    assert state.values["error_code"] == "faq_error"
    assert state.values["turn_count"] == 2


async def test_persistent_service_rejects_missing_thread_id() -> None:
    graph = AsyncMock()
    service = AgentService(graph, persistence_required=True)

    with pytest.raises(MissingConversationIdError):
        await service.handle_message("Вопрос без thread")
    graph.ainvoke.assert_not_awaited()


async def test_checkpoint_failure_is_safe_and_does_not_log_input(
    caplog: pytest.LogCaptureFixture,
) -> None:
    graph = AsyncMock()
    graph.ainvoke.side_effect = ConnectionError("postgresql://user:password@db/private")
    service = AgentService(graph, persistence_required=True)

    with caplog.at_level(logging.ERROR):
        response = await service.handle_message(
            "секретное сообщение пользователя",
            "telegram:v1:private-thread",
        )

    assert response == FALLBACK_MESSAGE
    assert "секретное сообщение" not in caplog.text
    assert "private-thread" not in caplog.text
    assert "password" not in caplog.text
    assert any(
        getattr(record, "exception_type", None) == "ConnectionError"
        for record in caplog.records
    )


async def test_order_and_unknown_routes_remain_unchanged_with_checkpointer() -> None:
    assistant = AsyncMock()
    saver = InMemorySaver()
    graph = build_agent_graph(assistant, checkpointer=saver)
    service = AgentService(graph, persistence_required=True)

    assert await service.handle_message("Хочу заказать пиццу", "thread-order") == (
        ORDER_PLACEHOLDER_MESSAGE
    )
    assert await service.handle_message("!!!", "thread-unknown") == FALLBACK_MESSAGE
    assistant.answer.assert_not_awaited()


async def test_same_thread_is_serialized_without_blocking_other_threads() -> None:
    class ConcurrentGraph:
        def __init__(self) -> None:
            self.active_by_thread: dict[str, int] = {}
            self.max_by_thread: dict[str, int] = {}
            self.total_active = 0
            self.max_total_active = 0

        async def ainvoke(self, state, config=None):
            thread_id = config["configurable"]["thread_id"]
            self.active_by_thread[thread_id] = (
                self.active_by_thread.get(thread_id, 0) + 1
            )
            self.max_by_thread[thread_id] = max(
                self.max_by_thread.get(thread_id, 0),
                self.active_by_thread[thread_id],
            )
            self.total_active += 1
            self.max_total_active = max(self.max_total_active, self.total_active)
            await asyncio.sleep(0.01)
            self.active_by_thread[thread_id] -= 1
            self.total_active -= 1
            return {"response": state["user_message"]}

    graph = ConcurrentGraph()
    service = AgentService(graph, persistence_required=True)

    await asyncio.gather(
        service.handle_message("a1", "thread-a"),
        service.handle_message("a2", "thread-a"),
        service.handle_message("b1", "thread-b"),
    )

    assert graph.max_by_thread["thread-a"] == 1
    assert graph.max_total_active >= 2
