import logging
from typing import Any, Protocol

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from app.agent.router import route_message
from app.agent.state import AgentIntent, AgentState
from app.rag.answer_service import FALLBACK_MESSAGE
from app.orders.context import append_interaction
from app.orders.contracts import InteractionEvent, OrderActionPlan, OrderIntentProposal
from app.orders.dialog import OrderDialog

logger = logging.getLogger(__name__)

ORDER_PLACEHOLDER_MESSAGE = (
    "Оформление заказов пока недоступно. Сейчас я могу ответить на вопросы о ресторане."
)


class FaqAnswerService(Protocol):
    async def answer(self, question: str) -> str: ...


def build_agent_graph(
    faq_assistant: FaqAnswerService | None,
    checkpointer: BaseCheckpointSaver | None = None,
    *,
    order_dialog: OrderDialog | None = None,
) -> CompiledStateGraph:
    def replay_result(state: AgentState) -> tuple[bool, str, str | None]:
        update_id = state.get("update_id")
        if update_id is None:
            return False, "", None
        for processed in state.get("processed_updates", []):
            if processed.get("update_id") == update_id:
                return (
                    True,
                    str(processed.get("response") or FALLBACK_MESSAGE),
                    processed.get("error_code"),
                )
        highest = state.get("highest_update_id")
        if highest is not None and update_id <= highest:
            return True, "Это сообщение уже обработано.", "stale_update"
        return False, "", None

    def record_update(state: AgentState, changes: dict[str, Any]) -> dict[str, Any]:
        proposal_data = state.get("action_proposal")
        semantic = "contact_input"
        if state.get("intent") == "faq":
            semantic = "faq"
        elif state.get("intent") == "unknown":
            semantic = "unknown"
        elif isinstance(proposal_data, dict):
            try:
                if "operations" in proposal_data:
                    OrderActionPlan.model_validate(proposal_data)
                    semantic = "cart_batch"
                else:
                    semantic = OrderIntentProposal.model_validate(proposal_data).action
            except Exception:
                semantic = "unknown"

        error_code = changes.get("error_code")
        if error_code is None:
            event_outcome = "handled"
        elif error_code in {
            "order_clarification",
            "dish_ambiguous",
            "dish_not_found",
            "batch_dish_ambiguous",
            "batch_dish_not_found",
            "empty_cart",
            "invalid_contact",
            "invalid_requested_at",
            "explicit_confirmation_required",
            "confirmation_not_ready",
        }:
            event_outcome = "clarified"
        elif isinstance(error_code, str) and (
            error_code.endswith("_error") or error_code.endswith("_unavailable")
        ):
            event_outcome = "failed"
        else:
            event_outcome = "rejected"

        trusted_dish_name = changes.get("last_referenced_dish_name")
        event = InteractionEvent(
            source="callback" if state.get("callback_action") else "text",
            semantic=semantic,
            outcome=event_outcome,
            dish_name=(
                trusted_dish_name
                if isinstance(trusted_dish_name, str) and trusted_dish_name
                else None
            ),
        )
        history = changes.get(
            "recent_interactions",
            state.get("recent_interactions", []),
        )
        changes["recent_interactions"] = append_interaction(history, event)
        changes["last_action"] = event.semantic

        update_id = state.get("update_id")
        if update_id is None:
            return changes
        processed = list(state.get("processed_updates", []))
        processed.append(
            {
                "update_id": update_id,
                "response": changes.get("response") or FALLBACK_MESSAGE,
                "error_code": changes.get("error_code"),
            }
        )
        changes["processed_updates"] = processed[-32:]
        changes["highest_update_id"] = max(
            update_id, state.get("highest_update_id") or update_id
        )
        return changes

    async def routing_node(state: AgentState) -> dict[str, Any]:
        replay, response, error_code = replay_result(state)
        if replay:
            return {"intent": "replay", "response": response, "error_code": error_code}
        if order_dialog is not None:
            intent, proposal = await order_dialog.classify(state)
            changes: dict[str, Any] = {
                "intent": intent,
                "action_proposal": proposal,
                "turn_count": state.get("turn_count", 0) + 1,
            }
            if state.get("pending_clarification") and intent in {"faq", "unknown"}:
                changes["pending_clarification"] = None
            return changes
        return {
            "intent": route_message(state["user_message"]),
            "turn_count": state.get("turn_count", 0) + 1,
        }

    async def faq_node(state: AgentState) -> dict[str, Any]:
        if faq_assistant is None:
            logger.info(
                "Agent route diagnostic",
                extra={"final_route": "faq", "fallback_reason": "faq_unavailable"},
            )
            return record_update(
                state, {"response": FALLBACK_MESSAGE, "error_code": "faq_unavailable"}
            )
        try:
            response = await faq_assistant.answer(state["user_message"])
        except Exception as exc:
            # Do not log the user message or exception text: either may contain PII.
            logger.error(
                "FAQ graph node failed",
                extra={
                    "exception_type": type(exc).__name__,
                    "final_route": "faq",
                    "fallback_reason": "faq_error",
                },
            )
            return record_update(
                state, {"response": FALLBACK_MESSAGE, "error_code": "faq_error"}
            )
        if not response or response == FALLBACK_MESSAGE:
            logger.info(
                "Agent route diagnostic",
                extra={"final_route": "faq", "fallback_reason": "faq_safe_fallback"},
            )
        return record_update(
            state, {"response": response or FALLBACK_MESSAGE, "error_code": None}
        )

    async def order_placeholder_node(state: AgentState) -> dict[str, Any]:
        return record_update(
            state, {"response": ORDER_PLACEHOLDER_MESSAGE, "error_code": None}
        )

    async def order_node(state: AgentState) -> dict[str, Any]:
        if order_dialog is None:
            return await order_placeholder_node(state)
        changes = await order_dialog.handle(state)
        if state.get("pending_clarification") and "pending_clarification" not in changes:
            changes["pending_clarification"] = None
        return record_update(state, changes)

    async def unknown_node(state: AgentState) -> dict[str, Any]:
        logger.info(
            "Agent route diagnostic",
            extra={
                "final_route": "unknown",
                "fallback_reason": "unsupported_message",
            },
        )
        return record_update(
            state, {"response": FALLBACK_MESSAGE, "error_code": "unsupported_message"}
        )

    async def replay_node(state: AgentState) -> dict[str, Any]:
        return {
            "response": state.get("response") or FALLBACK_MESSAGE,
            "error_code": state.get("error_code"),
        }

    async def select_route(state: AgentState) -> AgentIntent:
        return state["intent"]

    workflow = StateGraph(AgentState)
    workflow.add_node("route", routing_node)
    workflow.add_node("faq", faq_node)
    workflow.add_node("order", order_node)
    workflow.add_node("unknown", unknown_node)
    workflow.add_node("replay", replay_node)
    workflow.add_edge(START, "route")
    workflow.add_conditional_edges(
        "route",
        select_route,
        {
            "faq": "faq",
            "order": "order",
            "unknown": "unknown",
            "replay": "replay",
        },
    )
    workflow.add_edge("faq", END)
    workflow.add_edge("order", END)
    workflow.add_edge("unknown", END)
    workflow.add_edge("replay", END)
    return workflow.compile(checkpointer=checkpointer)
