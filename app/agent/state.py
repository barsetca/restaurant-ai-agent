from typing import Any, Literal, TypedDict

AgentIntent = Literal["faq", "order", "unknown", "replay", "catalog"]


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
    checkout_session_id: str | None
    highest_update_id: int | None
    processed_updates: list[dict[str, Any]]
    action_proposal: dict[str, Any] | None
    order_stage: str
    cart: list[dict[str, Any]]
    pending_action: dict[str, Any] | None
    pending_destructive_action: dict[str, Any] | None
    pending_clarification: dict[str, Any] | None
    pending_batch: dict[str, Any] | None
    candidates: list[dict[str, Any]]
    contacts: dict[str, Any]
    quote_snapshot: dict[str, Any] | None
    draft_revision: int
    confirmed_order: dict[str, Any] | None
    confirmed_order_archive: list[dict[str, Any]]
    recent_interactions: list[dict[str, Any]]
    last_action: str | None
    last_referenced_dish_name: str | None
    last_referenced_dish_id: str | None
    catalog_ui: dict[str, Any]
    catalog_transition: dict[str, Any] | None
