from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator


class UiModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CatalogScreen(StrEnum):
    HOME = "home"
    CATEGORIES = "categories"
    DISH_LIST = "dish_list"
    DISH_DETAIL = "dish_detail"
    CART = "cart"


class CatalogActionKind(StrEnum):
    OPEN_CATEGORIES = "open_categories"
    OPEN_CATEGORY = "open_category"
    OPEN_DISH = "open_dish"
    BACK = "back"
    OPEN_CART = "open_cart"
    CONTINUE_CATALOG = "continue_catalog"
    NEXT_PAGE = "next_page"
    PREVIOUS_PAGE = "previous_page"
    INCREMENT = "increment"
    DECREMENT = "decrement"
    CHECKOUT = "checkout"


class CatalogAction(UiModel):
    kind: CatalogActionKind
    target: str | None = Field(default=None, max_length=120)
    offset: int | None = Field(default=None, ge=0)
    draft_revision: int | None = Field(default=None, ge=0)
    requires_acknowledged_screen: bool = False


class NavigationContext(UiModel):
    screen: CatalogScreen
    category_id: str | None = Field(default=None, max_length=120)
    offset: int = Field(default=0, ge=0)
    dish_id: str | None = Field(default=None, max_length=36)
    categories_offset: int = Field(default=0, ge=0)


class RenderedButton(UiModel):
    label: str = Field(min_length=1, max_length=64)
    action: CatalogAction
    callback_data: str | None = Field(default=None, max_length=64)


class RenderedScreen(UiModel):
    screen: CatalogScreen
    text: str = Field(max_length=4096)
    rows: list[list[RenderedButton]] = Field(default_factory=list, max_length=40)
    parse_mode: str = "HTML"
    checkout_available: bool | None = None


class PendingRender(UiModel):
    screen: CatalogScreen
    token: str = Field(min_length=8, max_length=16, pattern=r"^[A-Za-z0-9_-]+$")
    revision: int = Field(ge=1)
    rendered: RenderedScreen
    actions: list[CatalogAction] = Field(default_factory=list, max_length=80)
    draft_revision: int | None = Field(default=None, ge=0)
    message_id: int | None = None
    navigation: NavigationContext | None = None
    cart_return: NavigationContext | None = None


class CatalogUiState(UiModel):
    active_screen: CatalogScreen = CatalogScreen.HOME
    requested_screen: CatalogScreen = CatalogScreen.HOME
    active_token: str | None = Field(
        default=None, min_length=8, max_length=16, pattern=r"^[A-Za-z0-9_-]+$"
    )
    active_revision: int = Field(default=0, ge=0)
    active_actions: list[CatalogAction] = Field(default_factory=list, max_length=80)
    active_message_id: int | None = None
    active_navigation: NavigationContext | None = None
    active_cart_return: NavigationContext | None = None
    pending_render: PendingRender | None = None
    categories_offset: int = Field(default=0, ge=0)
    category_id: str | None = Field(default=None, max_length=120)
    offset: int = Field(default=0, ge=0)
    selected_dish_id: str | None = Field(default=None, max_length=36)
    cart_return: NavigationContext | None = None
    ui_revision: int = Field(default=0, ge=0)
    retired_tokens: list[str] = Field(default_factory=list, max_length=32)

    @field_validator("category_id")
    @classmethod
    def clean_category(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = " ".join(value.split())
        return cleaned or None

    @classmethod
    def from_checkpoint(cls, value: object) -> "CatalogUiState":
        """Load a legacy/malformed optional field without breaking the checkpoint."""
        if not isinstance(value, dict):
            return cls()
        try:
            return cls.model_validate(value)
        except (TypeError, ValueError):
            return cls()

    def to_checkpoint(self) -> dict[str, object]:
        return self.model_dump(mode="json")
