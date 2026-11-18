from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

MAX_SEARCH_QUERY_LENGTH = 200
MAX_SEARCH_RESULTS = 20
MAX_DIETARY_TAGS = 10
MAX_DIETARY_TAG_LENGTH = 50
MAX_CART_LINES = 20
MAX_ITEM_QUANTITY = 20
MAX_CART_UNITS = 100
SUPPORTED_CURRENCY = "EUR"

SearchQuery = Annotated[str, Field(max_length=MAX_SEARCH_QUERY_LENGTH)]
CategoryFilter = Annotated[str, Field(min_length=1, max_length=120)]
DietaryTag = Annotated[str, Field(min_length=1, max_length=MAX_DIETARY_TAG_LENGTH)]
Quantity = Annotated[int, Field(ge=1, le=MAX_ITEM_QUANTITY)]


def _clean_text(value: str) -> str:
    return " ".join(value.strip().split())


def _normalize_tag(value: str) -> str:
    return _clean_text(value).casefold()


class MenuContract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SearchMenuRequest(MenuContract):
    query: SearchQuery | None = None
    category: CategoryFilter | None = None
    dietary_tags: list[DietaryTag] = Field(default_factory=list, max_length=MAX_DIETARY_TAGS)

    @field_validator("query")
    @classmethod
    def normalize_query(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = _clean_text(value)
        return cleaned or None

    @field_validator("category")
    @classmethod
    def normalize_category(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = _clean_text(value)
        if not cleaned:
            raise ValueError("category must not be blank")
        return cleaned

    @field_validator("dietary_tags")
    @classmethod
    def normalize_tags(cls, values: list[str]) -> list[str]:
        normalized = [_normalize_tag(value) for value in values]
        if any(not value for value in normalized):
            raise ValueError("dietary tags must not be blank")
        if len(set(normalized)) != len(normalized):
            raise ValueError("dietary tags must be unique")
        return normalized


class CartItemInput(MenuContract):
    dish_id: UUID
    quantity: Quantity


class DishSummary(MenuContract):
    dish_id: UUID
    name: str
    category: str
    dietary_tags: list[str]
    price_minor: int = Field(ge=0)
    currency: str


class DishDetail(DishSummary):
    description: str | None
    ingredients: list[str]
    allergens: list[str]
    available: bool


class MenuSearchResult(MenuContract):
    items: list[DishSummary]


class CartLine(MenuContract):
    dish_id: UUID
    name: str
    quantity: int = Field(ge=1)
    unit_price_minor: int = Field(ge=0)
    line_total_minor: int = Field(ge=0)


class CartQuote(MenuContract):
    items: list[CartLine]
    subtotal_minor: int = Field(ge=0)
    currency: str = SUPPORTED_CURRENCY
