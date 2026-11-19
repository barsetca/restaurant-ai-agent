from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.menu.contracts import MAX_CART_LINES, MAX_DIETARY_TAGS, MAX_ITEM_QUANTITY

OrderAction = Literal[
    "start",
    "new_order",
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
    "confirm",
    "edit",
    "edit_cart",
    "edit_name",
    "edit_phone",
    "edit_address",
    "edit_time",
    "return_summary",
    "cancel",
    "cancel_yes",
    "cancel_back",
    "time_asap",
    "time_manual",
    "confirm_new_order",
    "confirm_clear",
    "keep_current_order",
    "clarify_view_cart",
    "clarify_checkout",
    "cancel_checkout",
    "referenced_dish_info",
    "previous_order",
    "unknown",
]
OrderStage = Literal[
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
]

ExpectedInput = Literal[
    "free_text",
    "dish_choice",
    "customer_name",
    "phone",
    "address",
    "requested_time",
    "edit_choice",
    "confirmation",
    "destructive_confirmation",
    "clarification_choice",
]

LlmAction = Literal[
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
]

CartMutationAction = Literal["add", "set_quantity", "decrement", "remove"]
MAX_ACTION_OPERATIONS = 8

InteractionSemantic = OrderAction | Literal["contact_input", "cart_batch"]
InteractionOutcome = Literal["handled", "clarified", "rejected", "failed"]
ClarificationKind = Literal[
    "ambiguous_intent",
    "missing_information",
    "retrospective_intent",
    "unsafe_input",
]
UnsupportedReason = Literal[
    "not_supported",
    "insufficient_context",
    "unsafe_input",
]
OrderValidationRule = Literal["quantity_out_of_range"]
ActionValidationRule = Literal[
    "missing_items_for_item_action",
    "invalid_item_count_for_single_item_action",
    "missing_quantity_for_set_quantity",
    "missing_selector_for_search_menu",
    "items_not_allowed_for_action",
    "selectors_not_allowed_for_action",
]


def action_validation_rule(
    action: LlmAction,
    *,
    items_count: int,
    quantities: list[int | None],
    query_present: bool,
    category_present: bool,
    dietary_tags_count: int,
) -> ActionValidationRule | None:
    item_actions = {"dish_info", "add", "set_quantity", "decrement", "remove"}
    single_item_actions = {"dish_info", "set_quantity", "decrement", "remove"}
    if action in item_actions and items_count == 0:
        return "missing_items_for_item_action"
    if action in single_item_actions and items_count != 1:
        return "invalid_item_count_for_single_item_action"
    if action == "set_quantity" and quantities[0] is None:
        return "missing_quantity_for_set_quantity"
    if action == "search_menu" and not (
        query_present or category_present or dietary_tags_count
    ):
        return "missing_selector_for_search_menu"
    if action not in item_actions and items_count:
        return "items_not_allowed_for_action"
    if action not in {"search_menu", "browse_menu"} and (
        query_present or category_present or dietary_tags_count
    ):
        return "selectors_not_allowed_for_action"
    return None


_ACTION_VALIDATION_MESSAGES: dict[ActionValidationRule, str] = {
    "missing_items_for_item_action": "item action requires at least one item",
    "invalid_item_count_for_single_item_action": "action requires exactly one item",
    "missing_quantity_for_set_quantity": "set_quantity requires quantity",
    "missing_selector_for_search_menu": "search_menu requires a selector",
    "items_not_allowed_for_action": "items are not allowed for this action",
    "selectors_not_allowed_for_action": "menu selectors are not allowed for this action",
}


class OrderContract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProposedItem(OrderContract):
    name: str = Field(min_length=1, max_length=200)
    lookup_query: str | None = Field(default=None, max_length=200)
    quantity: int | None = Field(default=None, ge=1, le=MAX_ITEM_QUANTITY)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        return " ".join(value.strip().split())

    @field_validator("lookup_query")
    @classmethod
    def normalize_lookup_query(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = " ".join(value.strip().split())
        return cleaned or None


class OrderIntentProposal(OrderContract):
    action: OrderAction
    items: list[ProposedItem] = Field(default_factory=list, max_length=MAX_CART_LINES)
    query: str | None = Field(default=None, max_length=200)
    category: str | None = Field(default=None, max_length=120)
    dietary_tags: list[str] = Field(default_factory=list, max_length=MAX_DIETARY_TAGS)
    confidence: Literal["high", "low"] = "high"
    clarification: str | None = Field(default=None, max_length=300)

    @model_validator(mode="after")
    def validate_action_payload(self) -> "OrderIntentProposal":
        item_actions = {"dish_info", "add", "set_quantity", "decrement", "remove"}
        if self.action in item_actions and not self.items:
            raise ValueError("item action requires at least one item")
        return self


class CartMutationOperation(OrderContract):
    action: CartMutationAction
    items: list[ProposedItem] = Field(default_factory=list, max_length=MAX_CART_LINES)

    @model_validator(mode="after")
    def validate_action_payload(self) -> "CartMutationOperation":
        rule = action_validation_rule(
            self.action,
            items_count=len(self.items),
            quantities=[item.quantity for item in self.items],
            query_present=False,
            category_present=False,
            dietary_tags_count=0,
        )
        if rule is not None:
            raise ValueError(_ACTION_VALIDATION_MESSAGES[rule])
        return self


class OrderActionPlan(OrderContract):
    operations: list[CartMutationOperation]

    @model_validator(mode="after")
    def validate_operation_count(self) -> "OrderActionPlan":
        if not 2 <= len(self.operations) <= MAX_ACTION_OPERATIONS:
            raise ValueError(
                f"action plan requires 2-{MAX_ACTION_OPERATIONS} operations"
            )
        return self


class InteractionEvent(OrderContract):
    source: Literal["text", "callback"]
    semantic: InteractionSemantic
    outcome: InteractionOutcome
    dish_name: str | None = Field(default=None, min_length=1, max_length=80)

    @field_validator("dish_name")
    @classmethod
    def normalize_dish_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = " ".join(value.strip().split())
        return cleaned or None


class OrderContext(OrderContract):
    order_stage: OrderStage
    expected_input: ExpectedInput
    has_active_cart: bool
    cart_line_count: int = Field(ge=0, le=MAX_CART_LINES)
    cart_unit_count: int = Field(ge=0, le=100)
    allowed_actions: list[LlmAction] = Field(max_length=20)
    last_action: InteractionSemantic | None = None
    last_referenced_dish_name: str | None = Field(
        default=None, min_length=1, max_length=80
    )
    recent_interactions: list[InteractionEvent] = Field(
        default_factory=list, max_length=10
    )
    has_confirmed_snapshot: bool
    confirmed_snapshot_count: int = Field(ge=0, le=5)


class ActionOutcome(OrderContract):
    kind: Literal["action"] = "action"
    action: LlmAction
    items: list[ProposedItem] = Field(default_factory=list, max_length=MAX_CART_LINES)
    query: str | None = Field(default=None, max_length=200)
    category: str | None = Field(default=None, max_length=120)
    dietary_tags: list[str] = Field(default_factory=list, max_length=MAX_DIETARY_TAGS)

    @field_validator("query", "category")
    @classmethod
    def normalize_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = " ".join(value.strip().split())
        return cleaned or None

    @field_validator("dietary_tags")
    @classmethod
    def normalize_dietary_tags(cls, values: list[str]) -> list[str]:
        normalized = [" ".join(value.strip().split()).casefold() for value in values]
        if any(not value or len(value) > 50 for value in normalized):
            raise ValueError("dietary tags must contain 1-50 characters")
        if len(set(normalized)) != len(normalized):
            raise ValueError("dietary tags must be unique")
        return normalized

    @model_validator(mode="after")
    def validate_action_payload(self) -> "ActionOutcome":
        rule = action_validation_rule(
            self.action,
            items_count=len(self.items),
            quantities=[item.quantity for item in self.items],
            query_present=self.query is not None,
            category_present=self.category is not None,
            dietary_tags_count=len(self.dietary_tags),
        )
        if rule is not None:
            raise ValueError(_ACTION_VALIDATION_MESSAGES[rule])
        return self


class ClarifyOutcome(OrderContract):
    kind: Literal["clarify"] = "clarify"
    clarification_type: ClarificationKind
    question: str = Field(min_length=1, max_length=300)

    @field_validator("question")
    @classmethod
    def normalize_question(cls, value: str) -> str:
        cleaned = " ".join(value.strip().split())
        if not cleaned:
            raise ValueError("clarification question must not be blank")
        return cleaned


class UnsupportedOutcome(OrderContract):
    kind: Literal["unsupported"] = "unsupported"
    reason: UnsupportedReason


class ValidationOutcome(OrderContract):
    kind: Literal["validation_error"] = "validation_error"
    validation_rule: OrderValidationRule


class ActionsOutcome(OrderContract):
    kind: Literal["actions"] = "actions"
    operations: list[CartMutationOperation]

    @model_validator(mode="after")
    def validate_operation_count(self) -> "ActionsOutcome":
        if not 2 <= len(self.operations) <= MAX_ACTION_OPERATIONS:
            raise ValueError(
                f"actions outcome requires 2-{MAX_ACTION_OPERATIONS} operations"
            )
        return self


ContextualOutcome = Annotated[
    ActionOutcome
    | ActionsOutcome
    | ClarifyOutcome
    | UnsupportedOutcome
    | ValidationOutcome,
    Field(discriminator="kind"),
]


class ContextualIntentResult(OrderContract):
    outcome: ContextualOutcome


class ApiProposedItem(OrderContract):
    """Constraint-free item shape for OpenAI Structured Outputs."""

    name: str = Field(
        description="Dish phrase from the user; required only for item actions."
    )
    lookup_query: str | None = Field(
        description="Canonical dish search phrase for item actions; otherwise null."
    )
    quantity: int | None = Field(
        description=(
            "For add, the increment when explicitly stated; null means one. "
            "For set_quantity, the required final quantity."
        )
    )


class ApiActionOperation(OrderContract):
    """Flat cart mutation operation for OpenAI Structured Outputs."""

    action: CartMutationAction = Field(
        description="One cart mutation: add, set_quantity, decrement, or remove."
    )
    items: list[ApiProposedItem] = Field(
        description=(
            "Items for this operation. add may contain several items; "
            "set_quantity, decrement, and remove require exactly one."
        )
    )


class ApiContextualIntentResult(OrderContract):
    """Flat OpenAI-compatible DTO; domain constraints are applied after parsing."""

    kind: Literal["action", "actions", "clarify", "unsupported"] = Field(
        description="Outcome kind."
    )
    action: LlmAction | None = Field(
        description="Allowed action for kind=action; otherwise null."
    )
    items: list[ApiProposedItem] = Field(
        description=(
            "Dish items for add, set_quantity, decrement, remove, or dish_info; "
            "otherwise an empty array."
        )
    )
    query: str | None = Field(
        description="Menu selector for search_menu or browse_menu; otherwise null."
    )
    category: str | None = Field(
        description="Menu category for search_menu or browse_menu; otherwise null."
    )
    dietary_tags: list[str] = Field(
        description=(
            "Menu dietary selectors for search_menu or browse_menu; "
            "otherwise an empty array."
        )
    )
    operations: list[ApiActionOperation] = Field(
        description=(
            "Ordered cart mutations for kind=actions; otherwise an empty array. "
            "Do not create operations for items the user says to leave unchanged."
        )
    )
    clarification_type: ClarificationKind | None = Field(
        description="Clarification type for kind=clarify; otherwise null."
    )
    question: str | None = Field(
        description="Short question for kind=clarify; otherwise null."
    )
    reason: UnsupportedReason | None = Field(
        description="Reason for kind=unsupported; otherwise null."
    )


class CartStateItem(OrderContract):
    dish_id: UUID
    quantity: int = Field(ge=1, le=MAX_ITEM_QUANTITY)


class PendingCandidate(OrderContract):
    dish_id: UUID
    name: str = Field(min_length=1, max_length=255)


class PendingAction(OrderContract):
    action: Literal["dish_info", "add", "set_quantity", "decrement", "remove"]
    quantity: int | None = Field(default=None, ge=1, le=MAX_ITEM_QUANTITY)


class PendingDestructiveAction(OrderContract):
    action: Literal["new_order", "clear", "cancel"]
    return_stage: OrderStage
    confirmation_id: str = Field(
        min_length=12, max_length=12, pattern=r"^[A-Za-z0-9_-]{12}$"
    )

    @model_validator(mode="after")
    def validate_return_stage(self) -> "PendingDestructiveAction":
        if self.return_stage in {
            "awaiting_new_order_confirmation",
            "awaiting_clear_confirmation",
            "awaiting_cancel_confirmation",
        }:
            raise ValueError(
                "destructive action cannot return to its confirmation stage"
            )
        return self


ClarificationReason = Literal["order_intent", "dish_referent"]
ClarificationAction = Literal["view_cart", "checkout", "dish_info"]


class ClarificationChoice(OrderContract):
    action: ClarificationAction
    label: str = Field(min_length=1, max_length=40)


class PendingClarification(OrderContract):
    """Bounded continuation state; contains no raw user text or PII."""

    reason: ClarificationReason
    choices: list[ClarificationChoice] = Field(min_length=1, max_length=3)
    return_stage: OrderStage
    clarification_id: str = Field(
        min_length=12, max_length=12, pattern=r"^[A-Za-z0-9_-]{12}$"
    )

    @model_validator(mode="after")
    def validate_choices(self) -> "PendingClarification":
        actions = [choice.action for choice in self.choices]
        if len(set(actions)) != len(actions):
            raise ValueError("clarification choices must be unique")
        expected = (
            {"view_cart", "checkout"}
            if self.reason == "order_intent"
            else {"dish_info"}
        )
        if set(actions) != expected:
            raise ValueError("clarification choices do not match reason")
        return self


class PendingBatch(OrderContract):
    proposal: OrderIntentProposal
    resolved_dish_ids: list[UUID | None] = Field(max_length=MAX_CART_LINES)
    unresolved_index: int = Field(ge=0, lt=MAX_CART_LINES)
    return_stage: Literal["idle", "editing_cart"] = "idle"

    @model_validator(mode="after")
    def validate_batch_shape(self) -> "PendingBatch":
        if len(self.resolved_dish_ids) != len(self.proposal.items):
            raise ValueError("resolved dish IDs must match proposal items")
        if self.unresolved_index >= len(self.proposal.items):
            raise ValueError("unresolved index is outside proposal items")
        return self


class PendingActionPlan(OrderContract):
    plan: OrderActionPlan
    resolved_dish_ids: list[list[UUID | None]]
    unresolved_operation_index: int = Field(ge=0, lt=MAX_ACTION_OPERATIONS)
    unresolved_item_index: int = Field(ge=0, lt=MAX_CART_LINES)
    return_stage: Literal["idle", "editing_cart"] = "idle"

    @model_validator(mode="after")
    def validate_plan_shape(self) -> "PendingActionPlan":
        if len(self.resolved_dish_ids) != len(self.plan.operations):
            raise ValueError("resolved dish IDs must match plan operations")
        if any(
            len(ids) != len(operation.items)
            for ids, operation in zip(
                self.resolved_dish_ids,
                self.plan.operations,
                strict=True,
            )
        ):
            raise ValueError("resolved dish IDs must match operation items")
        operation_index = self.unresolved_operation_index
        if operation_index >= len(self.plan.operations):
            raise ValueError("unresolved operation index is outside the plan")
        if self.unresolved_item_index >= len(
            self.plan.operations[operation_index].items
        ):
            raise ValueError("unresolved item index is outside the operation")
        return self


class ContactData(OrderContract):
    customer_name: str | None = Field(default=None, max_length=100)
    phone: str | None = Field(default=None, max_length=32)
    address: str | None = Field(default=None, max_length=300)
    requested_at: str | None = Field(default=None, max_length=40)


class QuoteSnapshot(OrderContract):
    items: list[dict[str, object]] = Field(max_length=MAX_CART_LINES)
    subtotal_minor: int = Field(ge=0)
    delivery_fee_minor: int = Field(ge=0)
    total_minor: int = Field(ge=0)
    currency: Literal["EUR"] = "EUR"
    draft_revision: int = Field(ge=0)
    fingerprint: str = Field(min_length=64, max_length=64)


class ConfirmedOrder(QuoteSnapshot):
    customer_name: str = Field(min_length=1, max_length=100)
    phone: str = Field(min_length=1, max_length=32)
    address: str = Field(min_length=1, max_length=300)
    requested_at: str | None = Field(default=None, max_length=40)
    confirmation_key: str = Field(min_length=1, max_length=64)
    confirmed_at: str = Field(min_length=1, max_length=40)
