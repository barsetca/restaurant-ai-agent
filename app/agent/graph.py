import logging
from typing import Protocol

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from app.agent.router import route_message
from app.agent.state import AgentIntent, AgentState
from app.rag.answer_service import FALLBACK_MESSAGE

logger = logging.getLogger(__name__)

ORDER_PLACEHOLDER_MESSAGE = (
    "Оформление заказов пока недоступно. "
    "Сейчас я могу ответить на вопросы о ресторане."
)


class FaqAnswerService(Protocol):
    async def answer(self, question: str) -> str: ...


def build_agent_graph(
    faq_assistant: FaqAnswerService | None,
    checkpointer: BaseCheckpointSaver | None = None,
) -> CompiledStateGraph:
    async def routing_node(state: AgentState) -> dict[str, AgentIntent | int]:
        return {
            "intent": route_message(state["user_message"]),
            "turn_count": state.get("turn_count", 0) + 1,
        }

    async def faq_node(state: AgentState) -> dict[str, str | None]:
        if faq_assistant is None:
            return {"response": FALLBACK_MESSAGE, "error_code": "faq_unavailable"}
        try:
            response = await faq_assistant.answer(state["user_message"])
        except Exception as exc:
            # Do not log the user message or exception text: either may contain PII.
            logger.error(
                "FAQ graph node failed",
                extra={"exception_type": type(exc).__name__},
            )
            return {"response": FALLBACK_MESSAGE, "error_code": "faq_error"}
        return {"response": response or FALLBACK_MESSAGE, "error_code": None}

    async def order_placeholder_node(state: AgentState) -> dict[str, str | None]:
        del state
        return {"response": ORDER_PLACEHOLDER_MESSAGE, "error_code": None}

    async def unknown_node(state: AgentState) -> dict[str, str | None]:
        del state
        return {"response": FALLBACK_MESSAGE, "error_code": "unsupported_message"}

    async def select_route(state: AgentState) -> AgentIntent:
        return state["intent"]

    workflow = StateGraph(AgentState)
    workflow.add_node("route", routing_node)
    workflow.add_node("faq", faq_node)
    workflow.add_node("order_placeholder", order_placeholder_node)
    workflow.add_node("unknown", unknown_node)
    workflow.add_edge(START, "route")
    workflow.add_conditional_edges(
        "route",
        select_route,
        {
            "faq": "faq",
            "order": "order_placeholder",
            "unknown": "unknown",
        },
    )
    workflow.add_edge("faq", END)
    workflow.add_edge("order_placeholder", END)
    workflow.add_edge("unknown", END)
    return workflow.compile(checkpointer=checkpointer)
