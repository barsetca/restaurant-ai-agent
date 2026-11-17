import logging
from asyncio import Lock
from typing import Any, Protocol
from weakref import WeakValueDictionary

from langgraph.checkpoint.base import BaseCheckpointSaver
from langchain_core.runnables import RunnableConfig

from app.agent.graph import FaqAnswerService, build_agent_graph
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

    async def handle_message(self, message: str, conversation_id: str | None = None) -> str:
        if self._persistence_required and not conversation_id:
            raise MissingConversationIdError(
                "conversation_id is required when checkpoint persistence is enabled"
            )

        state_update: AgentState = {
            "user_message": message,
            "response": "",
            "error_code": None,
            "conversation_id": conversation_id,
        }
        config: RunnableConfig | None = None
        if conversation_id:
            config = {"configurable": {"thread_id": conversation_id}}

        if self._persistence_required:
            lock = await self._lock_for_thread(conversation_id or "")
            async with lock:
                return await self._invoke(state_update, config)
        return await self._invoke(state_update, config)

    async def _invoke(
        self,
        state_update: AgentState,
        config: RunnableConfig | None,
    ) -> str:
        try:
            result = await self._graph.ainvoke(state_update, config=config)
        except Exception as exc:
            # Graph errors must not expose input state, PII, or exception details.
            logger.error(
                "Agent graph execution failed",
                extra={"exception_type": type(exc).__name__},
            )
            return FALLBACK_MESSAGE
        response = result.get("response")
        return response.strip() if isinstance(response, str) and response.strip() else FALLBACK_MESSAGE


def build_agent_service(
    faq_assistant: FaqAnswerService | None,
    checkpointer: BaseCheckpointSaver | None = None,
) -> AgentService:
    return AgentService(
        build_agent_graph(faq_assistant, checkpointer=checkpointer),
        persistence_required=checkpointer is not None,
    )
