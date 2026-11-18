from typing import Any, Literal, TypedDict

AgentIntent = Literal["faq", "order", "unknown", "replay"]


class AgentState(TypedDict, total=False):
    """Serializable state persisted by the LangGraph checkpointer."""

    user_message: str
    intent: AgentIntent
    response: str
    error_code: str | None
    conversation_id: str | None
    turn_count: int
    update_id: int | None
    callback_action: str | None
    callback_revision: int | None
    callback_confirmation_id: str | None
    highest_update_id: int | None
    processed_updates: list[dict[str, Any]]
    action_proposal: dict[str, Any] | None
    order_stage: str
    cart: list[dict[str, Any]]
    pending_action: dict[str, Any] | None
    pending_destructive_action: dict[str, Any] | None
    pending_batch: dict[str, Any] | None
    candidates: list[dict[str, Any]]
    contacts: dict[str, Any]
    quote_snapshot: dict[str, Any] | None
    draft_revision: int
    confirmed_order: dict[str, Any] | None
    confirmed_order_archive: list[dict[str, Any]]
