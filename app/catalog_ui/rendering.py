from html import escape
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.catalog_ui.callbacks import encode_callback
from app.catalog_ui.models import (
    CatalogAction,
    CatalogActionKind,
    CatalogScreen,
    CatalogUiState,
    NavigationContext,
    PendingRender,
    RenderedButton,
    RenderedScreen,
)
from app.catalog_ui.navigation import CATEGORIES_PAGE_SIZE, normalize_offset
from app.menu.contracts import (
    BrowseMenuResult,
    CartInspection,
    CategoryList,
    DishDetail,
    SUPPORTED_CURRENCY,
)

MAX_TEXT_LENGTH = 4096
MAX_BUTTON_LABEL_LENGTH = 64


class RenderData(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    categories: CategoryList | None = None
    menu_page: BrowseMenuResult | None = None
    dish: DishDetail | None = None
    cart: CartInspection | None = None
    cart_quantities: dict[str, int] = Field(default_factory=dict)


def format_eur(minor: int, currency: str = SUPPORTED_CURRENCY) -> str:
    if currency != SUPPORTED_CURRENCY or minor < 0:
        return "Цена недоступна"
    return f"{minor // 100},{minor % 100:02d} €"


def _safe(value: object, *, fallback: str = "—", limit: int = 500) -> str:
    text = " ".join(str(value or "").split()) or fallback
    if len(text) > limit:
        text = text[: max(0, limit - 1)].rstrip() + "…"
    return escape(text, quote=False)


def _label(value: object) -> str:
    text = " ".join(str(value or "").split()) or "—"
    if len(text) > MAX_BUTTON_LABEL_LENGTH:
        text = text[: MAX_BUTTON_LABEL_LENGTH - 1].rstrip() + "…"
    return text


def _button(label: str, kind: CatalogActionKind, **kwargs: object) -> RenderedButton:
    return RenderedButton(
        label=_label(label),
        action=CatalogAction(kind=kind, **kwargs),
    )


def _cart_quantity(data: RenderData, dish_id: UUID) -> int:
    return max(0, data.cart_quantities.get(str(dish_id), 0))


def _cart_button() -> RenderedButton:
    return _button("Корзина", CatalogActionKind.OPEN_CART)


def render_screen(state: CatalogUiState, data: RenderData) -> RenderedScreen:
    """Render only supplied DTOs; no database, MCP, LLM or Telegram dependency exists."""
    screen = state.requested_screen
    rows: list[list[RenderedButton]] = []
    checkout_available: bool | None = None
    if screen is CatalogScreen.HOME:
        text = "<b>Ресторан</b>\nВыберите действие."
        rows.append([_button("Меню", CatalogActionKind.OPEN_CATEGORIES)])
        if data.cart is not None and data.cart.items:
            rows.append([_cart_button()])
    elif screen is CatalogScreen.CATEGORIES:
        categories = data.categories.categories if data.categories else []
        text = "<b>Категории меню</b>"
        if not categories:
            text += "\nСейчас доступных категорий нет."
        offset = normalize_offset(
            state.categories_offset, len(categories), CATEGORIES_PAGE_SIZE
        )
        for category in categories[offset : offset + CATEGORIES_PAGE_SIZE]:
            rows.append(
                [
                    _button(
                        f"{category.name} · {category.available_dish_count}",
                        CatalogActionKind.OPEN_CATEGORY,
                        target=category.id,
                    )
                ]
            )
        navigation: list[RenderedButton] = []
        if offset > 0:
            navigation.append(
                _button(
                    "Предыдущая",
                    CatalogActionKind.PREVIOUS_PAGE,
                    offset=offset - CATEGORIES_PAGE_SIZE,
                )
            )
        if offset + CATEGORIES_PAGE_SIZE < len(categories):
            navigation.append(
                _button(
                    "Следующая",
                    CatalogActionKind.NEXT_PAGE,
                    offset=offset + CATEGORIES_PAGE_SIZE,
                )
            )
        if navigation:
            rows.append(navigation)
        rows.append([_button("Назад", CatalogActionKind.BACK)])
    elif screen is CatalogScreen.DISH_LIST:
        page = data.menu_page
        items = page.items if page else []
        category = page.category if page and page.category else state.category_id
        lines = [f"<b>{_safe(category, fallback='Меню', limit=120)}</b>"]
        if not items:
            lines.append("На этой странице нет доступных блюд.")
        for item in items:
            quantity = _cart_quantity(data, item.dish_id)
            lines.append(
                f"• {_safe(item.name, limit=160)} — {format_eur(item.price_minor, item.currency)}"
                f" · в корзине: {quantity}"
            )
            dish_id = str(item.dish_id)
            rows.append(
                [
                    _button(item.name, CatalogActionKind.OPEN_DISH, target=dish_id),
                    _button(
                        "−",
                        CatalogActionKind.DECREMENT,
                        target=dish_id,
                        draft_revision=None,
                        requires_acknowledged_screen=True,
                    ),
                    _button(
                        "+",
                        CatalogActionKind.INCREMENT,
                        target=dish_id,
                        draft_revision=None,
                        requires_acknowledged_screen=True,
                    ),
                ]
            )
        navigation: list[RenderedButton] = []
        offset = page.offset if page else state.offset
        limit = page.limit if page else 5
        if offset > 0:
            navigation.append(
                _button(
                    "←",
                    CatalogActionKind.PREVIOUS_PAGE,
                    offset=max(0, offset - limit),
                )
            )
        if page and page.has_more:
            navigation.append(
                _button("→", CatalogActionKind.NEXT_PAGE, offset=offset + limit)
            )
        if navigation:
            rows.append(navigation)
        rows.append([_button("Категории", CatalogActionKind.BACK), _cart_button()])
        text = "\n".join(lines)
    elif screen is CatalogScreen.DISH_DETAIL:
        dish = data.dish
        if (
            dish is None
            or str(dish.dish_id) != state.selected_dish_id
            or not dish.available
        ):
            text = "<b>Блюдо недоступно</b>\nВернитесь к списку и обновите меню."
        else:
            quantity = _cart_quantity(data, dish.dish_id)
            lines = [
                f"<b>{_safe(dish.name, limit=160)}</b>",
                _safe(dish.description, fallback="Описание не указано.", limit=800),
                f"Цена: {format_eur(dish.price_minor, dish.currency)}",
                f"В корзине: {quantity}",
            ]
            if dish.ingredients:
                lines.append(
                    "Ингредиенты: "
                    + ", ".join(_safe(x, limit=80) for x in dish.ingredients)
                )
            if dish.allergens:
                lines.append(
                    "Аллергены: "
                    + ", ".join(_safe(x, limit=80) for x in dish.allergens)
                )
            if dish.dietary_tags:
                lines.append(
                    "Особенности: "
                    + ", ".join(_safe(x, limit=80) for x in dish.dietary_tags)
                )
            text = "\n".join(lines)
            dish_id = str(dish.dish_id)
            rows.append(
                [
                    _button(
                        "−",
                        CatalogActionKind.DECREMENT,
                        target=dish_id,
                        requires_acknowledged_screen=True,
                    ),
                    _button(
                        "+",
                        CatalogActionKind.INCREMENT,
                        target=dish_id,
                        requires_acknowledged_screen=True,
                    ),
                ]
            )
        rows.append([_button("Назад", CatalogActionKind.BACK), _cart_button()])
    elif screen is CatalogScreen.CART:
        cart = data.cart
        lines = ["<b>Корзина</b>"]
        problem = False
        if cart is None or not cart.items:
            lines.append("Корзина пуста.")
        else:
            for item in cart.items:
                if item.missing:
                    problem = True
                    lines.append(f"• missing ({item.dish_id}) × {item.quantity}")
                elif not item.available:
                    problem = True
                    lines.append(f"• {_safe(item.name)} × {item.quantity} — недоступно")
                else:
                    lines.append(
                        f"• {_safe(item.name)} × {item.quantity} — "
                        f"{format_eur(item.line_total_minor or 0, cart.currency)}"
                    )
            lines.append(
                "Доступная часть корзины: "
                + format_eur(cart.available_subtotal_minor, cart.currency)
                + " (не окончательная стоимость заказа)."
            )
            problem = problem or cart.has_unavailable_items
        checkout_available = bool(cart and cart.items and not problem)
        rows.append([_button("Продолжить выбор", CatalogActionKind.CONTINUE_CATALOG)])
        if checkout_available:
            rows.append(
                [
                    _button(
                        "Оформить",
                        CatalogActionKind.CHECKOUT,
                        requires_acknowledged_screen=True,
                    )
                ]
            )
        else:
            lines.append("Оформление сейчас недоступно.")
        text = "\n".join(lines)
    else:  # pragma: no cover - enum exhaustiveness guard
        raise ValueError("unsupported screen")
    if len(text) > MAX_TEXT_LENGTH:
        text = text[: MAX_TEXT_LENGTH - 1].rstrip() + "…"
    return RenderedScreen(
        screen=screen,
        text=text,
        rows=rows,
        checkout_available=checkout_available,
    )


def prepare_render(
    state: CatalogUiState,
    rendered: RenderedScreen,
    *,
    token: str,
    draft_revision: int | None = None,
) -> CatalogUiState:
    if rendered.screen is not state.requested_screen:
        raise ValueError("rendered screen does not match requested navigation state")
    actions = [button.action for row in rendered.rows for button in row]
    indexed_rows: list[list[RenderedButton]] = []
    index = 0
    for row in rendered.rows:
        indexed_row: list[RenderedButton] = []
        for button in row:
            action = button.action
            if action.requires_acknowledged_screen:
                action = action.model_copy(update={"draft_revision": draft_revision})
                actions[index] = action
            indexed_row.append(
                button.model_copy(
                    update={
                        "action": action,
                        "callback_data": encode_callback(token, index),
                    }
                )
            )
            index += 1
        indexed_rows.append(indexed_row)
    prepared = rendered.model_copy(update={"rows": indexed_rows})
    revision = state.ui_revision + 1
    pending = PendingRender(
        screen=rendered.screen,
        token=token,
        revision=revision,
        rendered=prepared,
        actions=actions,
        draft_revision=draft_revision,
        navigation=NavigationContext(
            screen=rendered.screen,
            category_id=state.category_id,
            offset=state.offset,
            dish_id=state.selected_dish_id,
            categories_offset=state.categories_offset,
        ),
        cart_return=state.cart_return,
    )
    return state.model_copy(update={"pending_render": pending, "ui_revision": revision})


def acknowledge_render(
    state: CatalogUiState,
    *,
    token: str,
    message_id: int | None = None,
) -> CatalogUiState:
    pending = state.pending_render
    if pending is None or pending.token != token:
        raise ValueError("render acknowledgement does not match pending token")
    navigation = pending.navigation or NavigationContext(
        screen=pending.screen,
        category_id=state.category_id,
        offset=state.offset,
        dish_id=state.selected_dish_id,
        categories_offset=state.categories_offset,
    )
    retired = list(state.retired_tokens)
    if state.active_token and state.active_token != pending.token:
        retired.append(state.active_token)
    return state.model_copy(
        update={
            "active_screen": pending.screen,
            "requested_screen": pending.screen,
            "active_token": pending.token,
            "active_revision": pending.revision,
            "active_actions": pending.actions,
            "active_message_id": message_id,
            "active_navigation": navigation,
            "active_cart_return": pending.cart_return,
            "category_id": navigation.category_id,
            "offset": navigation.offset,
            "selected_dish_id": navigation.dish_id,
            "categories_offset": navigation.categories_offset,
            "retired_tokens": retired[-32:],
            "cart_return": pending.cart_return,
            "pending_render": None,
        }
    )
