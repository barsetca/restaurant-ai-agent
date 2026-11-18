from typing import Literal
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
    confirmation_id: str = Field(min_length=12, max_length=12, pattern=r"^[A-Za-z0-9_-]{12}$")

    @model_validator(mode="after")
    def validate_return_stage(self) -> "PendingDestructiveAction":
        if self.return_stage in {
            "awaiting_new_order_confirmation",
            "awaiting_clear_confirmation",
            "awaiting_cancel_confirmation",
        }:
            raise ValueError("destructive action cannot return to its confirmation stage")
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
