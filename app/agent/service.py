import logging
from asyncio import Lock
from dataclasses import dataclass
from typing import Any, Protocol
from weakref import WeakValueDictionary

from langgraph.checkpoint.base import BaseCheckpointSaver
from langchain_core.runnables import RunnableConfig

from app.agent.graph import FaqAnswerService, build_agent_graph
from app.orders.dialog import OrderDialog
from app.agent.state import AgentState
from app.rag.answer_service import FALLBACK_MESSAGE

logger = logging.getLogger(__name__)


class AsyncAgentGraph(Protocol):
    async def ainvoke(
        self,
        input: AgentState,
        config: RunnableConfig | None = None,
    ) -> dict[str, Any]: ...


class MissingConversationIdError(ValueError):
    """Raised when a persistent graph invocation has no isolated thread ID."""


@dataclass(frozen=True)
class AgentResponse:
    text: str
    order_stage: str | None
    draft_revision: int | None
    error_code: str | None
    destructive_confirmation_id: str | None = None


class AgentService:
    def __init__(self, graph: AsyncAgentGraph, *, persistence_required: bool = False) -> None:
        self._graph = graph
        self._persistence_required = persistence_required
        self._thread_locks: WeakValueDictionary[str, Lock] = WeakValueDictionary()
        self._thread_locks_guard = Lock()

    async def _lock_for_thread(self, thread_id: str) -> Lock:
        async with self._thread_locks_guard:
            lock = self._thread_locks.get(thread_id)
            if lock is None:
                lock = Lock()
                self._thread_locks[thread_id] = lock
            return lock

    async def handle_message(
        self,
        message: str,
        conversation_id: str | None = None,
        update_id: int | None = None,
    ) -> str:
        result = await self.handle_message_result(message, conversation_id, update_id)
        return result.text

    async def handle_message_result(
        self,
        message: str,
        conversation_id: str | None = None,
        update_id: int | None = None,
        *,
        callback_action: str | None = None,
        callback_revision: int | None = None,
        callback_confirmation_id: str | None = None,
    ) -> AgentResponse:
        if self._persistence_required and not conversation_id:
            raise MissingConversationIdError(
                "conversation_id is required when checkpoint persistence is enabled"
            )

        state_update: AgentState = {
            "user_message": message,
            "response": "",
            "error_code": None,
            "conversation_id": conversation_id,
            "update_id": update_id,
            "callback_action": callback_action,
            "callback_revision": callback_revision,
            "callback_confirmation_id": callback_confirmation_id,
        }
        config: RunnableConfig | None = None
        if conversation_id:
            config = {"configurable": {"thread_id": conversation_id}}

        if self._persistence_required:
            lock = await self._lock_for_thread(conversation_id or "")
            async with lock:
                return await self._invoke_result(state_update, config)
        return await self._invoke_result(state_update, config)

    async def _invoke(
        self,
        state_update: AgentState,
        config: RunnableConfig | None,
    ) -> str:
        return (await self._invoke_result(state_update, config)).text

    async def _invoke_result(
        self,
        state_update: AgentState,
        config: RunnableConfig | None,
    ) -> AgentResponse:
        try:
            result = await self._graph.ainvoke(state_update, config=config)
        except Exception as exc:
            # Graph errors must not expose input state, PII, or exception details.
            logger.error(
                "Agent graph execution failed",
                extra={"exception_type": type(exc).__name__},
            )
            return AgentResponse(FALLBACK_MESSAGE, None, None, "agent_error")
        response = result.get("response")
        text = response.strip() if isinstance(response, str) and response.strip() else FALLBACK_MESSAGE
        revision = result.get("draft_revision")
        pending = result.get("pending_destructive_action")
        confirmation_id = pending.get("confirmation_id") if isinstance(pending, dict) else None
        return AgentResponse(
            text=text,
            order_stage=result.get("order_stage") if isinstance(result.get("order_stage"), str) else None,
            draft_revision=revision if isinstance(revision, int) else None,
            error_code=result.get("error_code") if isinstance(result.get("error_code"), str) else None,
            destructive_confirmation_id=(
                confirmation_id if isinstance(confirmation_id, str) else None
            ),
        )


def build_agent_service(
    faq_assistant: FaqAnswerService | None,
    checkpointer: BaseCheckpointSaver | None = None,
    *,
    order_dialog: OrderDialog | None = None,
) -> AgentService:
    graph = (
        build_agent_graph(faq_assistant, checkpointer=checkpointer)
        if order_dialog is None
        else build_agent_graph(
            faq_assistant,
            checkpointer=checkpointer,
            order_dialog=order_dialog,
        )
    )
    return AgentService(
        graph,
        persistence_required=checkpointer is not None,
    )
