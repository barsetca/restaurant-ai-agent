from typing import Literal, TypedDict

AgentIntent = Literal["faq", "order", "unknown"]


class AgentState(TypedDict, total=False):
    """Serializable state persisted by the LangGraph checkpointer."""

    user_message: str
    intent: AgentIntent
    response: str
    error_code: str | None
    conversation_id: str | None
    turn_count: int
