from unittest.mock import AsyncMock, Mock

from app.agent.graph import ORDER_PLACEHOLDER_MESSAGE, build_agent_graph
from app.agent.service import AgentService, build_agent_service
from app.rag.answer_service import FALLBACK_MESSAGE


def _initial_state(
    message: str, conversation_id: str | None = "conversation"
) -> dict[str, object]:
    return {
        "user_message": message,
        "intent": "unknown",
        "response": "",
        "error_code": None,
        "conversation_id": conversation_id,
    }


async def test_faq_intent_reaches_faq_node_and_returns_grounded_answer() -> None:
    assistant = AsyncMock()
    assistant.answer.return_value = "Ресторан открыт с 12:30."
    graph = build_agent_graph(assistant)

    result = await graph.ainvoke(_initial_state("Во сколько открывается ресторан?"))

    assert result["intent"] == "faq"
    assert result["response"] == "Ресторан открыт с 12:30."
    assert result["error_code"] is None
    assistant.answer.assert_awaited_once_with("Во сколько открывается ресторан?")


async def test_order_intent_reaches_placeholder() -> None:
    assistant = AsyncMock()
    service = build_agent_service(assistant)

    assert (
        await service.handle_message("Хочу заказать пиццу") == ORDER_PLACEHOLDER_MESSAGE
    )
    assert (
        await service.handle_message("Добавь две пиццы в корзину")
        == ORDER_PLACEHOLDER_MESSAGE
    )
    assistant.answer.assert_not_awaited()


async def test_order_question_and_ambiguous_message_prefer_faq() -> None:
    assistant = AsyncMock()
    assistant.answer.side_effect = ["Инструкция FAQ", "Ответ FAQ"]
    service = build_agent_service(assistant)

    assert await service.handle_message("Как оформить заказ?") == "Инструкция FAQ"
    assert await service.handle_message("Я думаю о заказе") == "Ответ FAQ"
    assert assistant.answer.await_count == 2


async def test_empty_message_reaches_unknown_fallback() -> None:
    assistant = AsyncMock()
    graph = build_agent_graph(assistant)

    result = await graph.ainvoke(_initial_state(""))

    assert result["intent"] == "unknown"
    assert result["response"] == FALLBACK_MESSAGE
    assert result["error_code"] == "unsupported_message"
    assistant.answer.assert_not_awaited()


async def test_faq_fallback_is_not_duplicated_by_graph() -> None:
    assistant = AsyncMock()
    assistant.answer.return_value = FALLBACK_MESSAGE
    service = build_agent_service(assistant)

    assert await service.handle_message("Неизвестный вопрос") == FALLBACK_MESSAGE


async def test_faq_exception_returns_safe_fallback() -> None:
    assistant = AsyncMock()
    assistant.answer.side_effect = RuntimeError("synthetic internal details")
    graph = build_agent_graph(assistant)

    result = await graph.ainvoke(_initial_state("Вопрос"))

    assert result["response"] == FALLBACK_MESSAGE
    assert result["error_code"] == "faq_error"
    assert "synthetic" not in result["response"]


async def test_missing_faq_runtime_returns_safe_fallback() -> None:
    graph = build_agent_graph(None)

    result = await graph.ainvoke(_initial_state("Когда вы открыты?"))

    assert result["response"] == FALLBACK_MESSAGE
    assert result["error_code"] == "faq_unavailable"


async def test_graph_execution_error_returns_safe_fallback() -> None:
    graph = AsyncMock()
    graph.ainvoke.side_effect = RuntimeError("synthetic graph details")
    service = AgentService(graph)

    assert await service.handle_message("Безопасный вопрос") == FALLBACK_MESSAGE


async def test_compiled_graph_is_created_once_and_reused(monkeypatch) -> None:
    graph = AsyncMock()
    graph.ainvoke.return_value = {"response": "Ответ"}
    builder = Mock(return_value=graph)
    monkeypatch.setattr("app.agent.service.build_agent_graph", builder)

    service = build_agent_service(None)
    assert await service.handle_message("Первый") == "Ответ"
    assert await service.handle_message("Второй") == "Ответ"

    builder.assert_called_once_with(None, checkpointer=None)
    assert graph.ainvoke.await_count == 2
