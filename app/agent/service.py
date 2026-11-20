import logging
from asyncio import Lock
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
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

    async def aget_state(self, config: RunnableConfig) -> Any: ...

    async def aupdate_state(
        self, config: RunnableConfig, values: dict[str, Any]
    ) -> Any: ...


class MissingConversationIdError(ValueError):
    """Raised when a persistent graph invocation has no isolated thread ID."""


@dataclass(frozen=True)
class AgentResponse:
    text: str
    order_stage: str | None
    draft_revision: int | None
    error_code: str | None
    destructive_confirmation_id: str | None = None
    clarification_id: str | None = None
    checkout_session_id: str | None = None


@dataclass(frozen=True)
class CatalogCommitResult:
    state: dict[str, Any]
    replayed: bool


class AgentService:
    def __init__(
        self, graph: AsyncAgentGraph, *, persistence_required: bool = False
    ) -> None:
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

    @asynccontextmanager
    async def thread_lifecycle(self, thread_id: str) -> AsyncIterator[None]:
        """Own the single same-thread lock for the complete Telegram lifecycle."""
        lock = await self._lock_for_thread(thread_id)
        async with lock:
            yield

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

        state_update, config = self._message_input(
            message,
            conversation_id,
            update_id,
            callback_action=callback_action,
            callback_revision=callback_revision,
            callback_confirmation_id=callback_confirmation_id,
        )

        if self._persistence_required:
            async with self.thread_lifecycle(conversation_id or ""):
                return await self._invoke_result(state_update, config)
        return await self._invoke_result(state_update, config)

    def _message_input(
        self,
        message: str,
        conversation_id: str | None,
        update_id: int | None,
        *,
        callback_action: str | None = None,
        callback_revision: int | None = None,
        callback_confirmation_id: str | None = None,
    ) -> tuple[AgentState, RunnableConfig | None]:
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

        return state_update, config

    async def handle_message_result_and_deliver(
        self,
        message: str,
        conversation_id: str,
        update_id: int | None,
        deliver: Callable[[AgentResponse], Awaitable[object]],
        *,
        callback_action: str | None = None,
        callback_revision: int | None = None,
        callback_confirmation_id: str | None = None,
    ) -> AgentResponse:
        state_update, config = self._message_input(
            message,
            conversation_id,
            update_id,
            callback_action=callback_action,
            callback_revision=callback_revision,
            callback_confirmation_id=callback_confirmation_id,
        )
        async with self.thread_lifecycle(conversation_id):
            result = await self._invoke_result(state_update, config)
            await deliver(result)
            return result

    async def checkpoint_values_unlocked(self, conversation_id: str) -> dict[str, Any]:
        snapshot = await self._graph.aget_state(
            {"configurable": {"thread_id": conversation_id}}
        )
        return dict(snapshot.values)

    async def commit_catalog_unlocked(
        self,
        conversation_id: str,
        update_id: int | None,
        catalog_ui: dict[str, Any],
    ) -> CatalogCommitResult:
        result = await self._graph.ainvoke(
            {
                "user_message": "",
                "conversation_id": conversation_id,
                "update_id": update_id,
                "catalog_transition": catalog_ui,
            },
            config={"configurable": {"thread_id": conversation_id}},
        )
        return CatalogCommitResult(
            state=dict(result), replayed=result.get("intent") == "replay"
        )

    async def acknowledge_catalog_unlocked(
        self, conversation_id: str, catalog_ui: dict[str, Any]
    ) -> None:
        await self._graph.aupdate_state(
            {"configurable": {"thread_id": conversation_id}},
            {"catalog_ui": catalog_ui},
        )

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
        text = (
            response.strip()
            if isinstance(response, str) and response.strip()
            else FALLBACK_MESSAGE
        )
        revision = result.get("draft_revision")
        pending = result.get("pending_destructive_action")
        confirmation_id = (
            pending.get("confirmation_id") if isinstance(pending, dict) else None
        )
        clarification = result.get("pending_clarification")
        clarification_id = (
            clarification.get("clarification_id")
            if isinstance(clarification, dict)
            and clarification.get("reason") == "order_intent"
            else None
        )
        checkout_session_id = result.get("checkout_session_id")
        return AgentResponse(
            text=text,
            order_stage=result.get("order_stage")
            if isinstance(result.get("order_stage"), str)
            else None,
            draft_revision=revision if isinstance(revision, int) else None,
            error_code=result.get("error_code")
            if isinstance(result.get("error_code"), str)
            else None,
            destructive_confirmation_id=(
                confirmation_id if isinstance(confirmation_id, str) else None
            ),
            clarification_id=(
                clarification_id if isinstance(clarification_id, str) else None
            ),
            checkout_session_id=(
                checkout_session_id if isinstance(checkout_session_id, str) else None
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
