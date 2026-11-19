import re
from typing import cast

from pydantic import BaseModel, ConfigDict, ValidationError

from app.menu.contracts import MAX_CART_LINES, MAX_CART_UNITS
from app.orders.contracts import (
    ExpectedInput,
    InteractionEvent,
    InteractionSemantic,
    LlmAction,
    OrderContext,
    OrderStage,
)

MAX_SEMANTIC_HISTORY = 10
MAX_CONFIRMED_SNAPSHOTS = 5

_EMAIL_PATTERN = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
_UUID_PATTERN = re.compile(
    r"(?i)\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b"
)

_DEFAULT_STAGE: OrderStage = "idle"
_VALID_STAGES = {
    "idle",
    "awaiting_disambiguation",
    "collecting_name",
    "collecting_phone",
    "collecting_address",
    "collecting_time",
    "collecting_edit_name",
    "collecting_edit_phone",
    "collecting_edit_address",
    "collecting_edit_time",
    "editing_cart",
    "awaiting_edit_choice",
    "awaiting_cancel_confirmation",
    "awaiting_new_order_confirmation",
    "awaiting_clear_confirmation",
    "awaiting_confirmation",
    "confirmed",
}

_EXPECTED_INPUT: dict[OrderStage, ExpectedInput] = {
    "idle": "free_text",
    "awaiting_disambiguation": "dish_choice",
    "collecting_name": "customer_name",
    "collecting_phone": "phone",
    "collecting_address": "address",
    "collecting_time": "requested_time",
    "collecting_edit_name": "customer_name",
    "collecting_edit_phone": "phone",
    "collecting_edit_address": "address",
    "collecting_edit_time": "requested_time",
    "editing_cart": "free_text",
    "awaiting_edit_choice": "edit_choice",
    "awaiting_cancel_confirmation": "destructive_confirmation",
    "awaiting_new_order_confirmation": "destructive_confirmation",
    "awaiting_clear_confirmation": "destructive_confirmation",
    "awaiting_confirmation": "confirmation",
    "confirmed": "free_text",
}

_GENERAL_ACTIONS: tuple[LlmAction, ...] = (
    "faq",
    "browse_menu",
    "search_menu",
    "dish_info",
    "add",
    "set_quantity",
    "decrement",
    "remove",
    "clear",
    "view_cart",
    "checkout",
    "cancel",
    "new_order",
    "previous_order",
)
_CONFIRMED_ACTIONS: tuple[LlmAction, ...] = (
    "faq",
    "browse_menu",
    "search_menu",
    "dish_info",
    "view_cart",
    "new_order",
    "previous_order",
)


class ContextSource(BaseModel):
    """Explicit internal projection accepted by ContextBuilder."""

    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    order_stage: object = _DEFAULT_STAGE
    cart_quantities: tuple[object, ...] = ()
    recent_interactions: object = ()
    last_action: object = None
    last_referenced_dish_name: object = None
    has_confirmed_snapshot: bool = False
    confirmed_snapshot_count: int = 0
    pending_allowed_actions: tuple[object, ...] = ()


def expected_input_for_stage(stage: OrderStage) -> ExpectedInput:
    return _EXPECTED_INPUT[stage]


def allowed_actions_for_stage(stage: OrderStage) -> tuple[LlmAction, ...]:
    if stage in {
        "awaiting_cancel_confirmation",
        "awaiting_new_order_confirmation",
        "awaiting_clear_confirmation",
    }:
        return ()
    if stage.startswith("collecting_"):
        return ()
    if stage == "confirmed":
        return _CONFIRMED_ACTIONS
    return _GENERAL_ACTIONS


def validated_history(value: object) -> list[InteractionEvent]:
    if not isinstance(value, list):
        return []
    events: list[InteractionEvent] = []
    for raw_event in value[-MAX_SEMANTIC_HISTORY:]:
        try:
            event = InteractionEvent.model_validate(raw_event)
        except ValidationError:
            continue
        if event.dish_name is not None and not _safe_public_label(event.dish_name):
            continue
        events.append(event)
    return events[-MAX_SEMANTIC_HISTORY:]


def append_interaction(
    history: object,
    event: InteractionEvent,
) -> list[dict[str, object]]:
    events = [*validated_history(history), event]
    return [item.model_dump(mode="json") for item in events[-MAX_SEMANTIC_HISTORY:]]


def _safe_public_label(value: str) -> bool:
    if _EMAIL_PATTERN.search(value) or _UUID_PATTERN.search(value):
        return False
    return len(re.sub(r"\D", "", value)) < 7


class ContextBuilder:
    """Build a bounded, allowlisted LLM view without mutating source state."""

    def build(self, source: ContextSource) -> OrderContext:
        raw_stage = source.order_stage
        stage = cast(
            OrderStage, raw_stage if raw_stage in _VALID_STAGES else _DEFAULT_STAGE
        )

        valid_quantities: list[int] = []
        for quantity in source.cart_quantities[:MAX_CART_LINES]:
            if (
                isinstance(quantity, int)
                and not isinstance(quantity, bool)
                and 1 <= quantity <= 20
            ):
                valid_quantities.append(quantity)

        history = validated_history(source.recent_interactions)
        last_action: InteractionSemantic | None = None
        raw_last_action = source.last_action
        if isinstance(raw_last_action, str):
            try:
                probe = InteractionEvent(
                    source="text",
                    semantic=raw_last_action,
                    outcome="handled",
                )
                last_action = probe.semantic
            except ValidationError:
                pass

        dish_name = source.last_referenced_dish_name
        if isinstance(dish_name, str):
            dish_name = " ".join(dish_name.strip().split())[:80] or None
            if dish_name is not None and not _safe_public_label(dish_name):
                dish_name = None
        else:
            dish_name = None

        confirmed_count = min(
            MAX_CONFIRMED_SNAPSHOTS,
            max(0, source.confirmed_snapshot_count),
        )

        pending_actions = tuple(
            action
            for action in source.pending_allowed_actions
            if isinstance(action, str) and action in _GENERAL_ACTIONS
        )
        allowed_actions = (
            list(dict.fromkeys(pending_actions))
            if pending_actions
            else list(allowed_actions_for_stage(stage))
        )
        return OrderContext(
            order_stage=stage,
            expected_input=(
                "clarification_choice"
                if pending_actions
                else expected_input_for_stage(stage)
            ),
            has_active_cart=bool(valid_quantities)
            and not (stage == "confirmed" and source.has_confirmed_snapshot),
            cart_line_count=len(valid_quantities),
            cart_unit_count=min(MAX_CART_UNITS, sum(valid_quantities)),
            allowed_actions=allowed_actions,
            last_action=last_action,
            last_referenced_dish_name=dish_name,
            recent_interactions=history,
            has_confirmed_snapshot=source.has_confirmed_snapshot,
            confirmed_snapshot_count=confirmed_count,
        )
