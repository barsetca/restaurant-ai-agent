from app.catalog_ui.models import (
    CatalogAction,
    CatalogActionKind,
    CatalogScreen,
    CatalogUiState,
    NavigationContext,
)

DEFAULT_PAGE_SIZE = 5
CATEGORIES_PAGE_SIZE = 10


def normalize_offset(
    offset: object, total: int, page_size: int = DEFAULT_PAGE_SIZE
) -> int:
    if page_size <= 0:
        raise ValueError("page_size must be positive")
    if total <= 0:
        return 0
    safe = offset if isinstance(offset, int) and not isinstance(offset, bool) else 0
    safe = max(0, safe)
    return min((safe // page_size) * page_size, ((total - 1) // page_size) * page_size)


def _context(state: CatalogUiState) -> NavigationContext:
    return NavigationContext(
        screen=state.active_screen,
        category_id=state.category_id,
        offset=state.offset,
        dish_id=state.selected_dish_id,
    )


def navigate(
    state: CatalogUiState,
    action: CatalogAction,
    *,
    total_items: int | None = None,
) -> CatalogUiState:
    """Return navigation state only; order/cart fields do not exist in this domain."""
    screen = state.active_screen
    kind = action.kind
    changes: dict[str, object] = {"pending_render": None}
    if screen is CatalogScreen.HOME and kind is CatalogActionKind.OPEN_CATEGORIES:
        changes.update(
            requested_screen=CatalogScreen.CATEGORIES, category_id=None, offset=0
        )
    elif (
        screen is CatalogScreen.CATEGORIES
        and kind is CatalogActionKind.OPEN_CATEGORY
        and action.target
    ):
        changes.update(
            requested_screen=CatalogScreen.DISH_LIST,
            category_id=action.target,
            offset=0,
            selected_dish_id=None,
        )
    elif (
        screen is CatalogScreen.DISH_LIST
        and kind is CatalogActionKind.OPEN_DISH
        and action.target
    ):
        changes.update(
            requested_screen=CatalogScreen.DISH_DETAIL, selected_dish_id=action.target
        )
    elif screen is CatalogScreen.DISH_DETAIL and kind is CatalogActionKind.BACK:
        changes.update(requested_screen=CatalogScreen.DISH_LIST, selected_dish_id=None)
    elif screen is CatalogScreen.DISH_LIST and kind is CatalogActionKind.BACK:
        changes.update(
            requested_screen=CatalogScreen.CATEGORIES,
            category_id=None,
            offset=0,
            selected_dish_id=None,
        )
    elif screen is CatalogScreen.CATEGORIES and kind is CatalogActionKind.BACK:
        changes.update(requested_screen=CatalogScreen.HOME)
    elif (
        screen in {CatalogScreen.DISH_LIST, CatalogScreen.DISH_DETAIL}
        and kind is CatalogActionKind.OPEN_CART
    ):
        changes.update(requested_screen=CatalogScreen.CART, cart_return=_context(state))
    elif screen is CatalogScreen.CART and kind is CatalogActionKind.CONTINUE_CATALOG:
        target = state.cart_return or NavigationContext(screen=CatalogScreen.HOME)
        changes.update(
            requested_screen=target.screen,
            category_id=target.category_id,
            offset=target.offset,
            selected_dish_id=target.dish_id,
            cart_return=None,
        )
    elif screen is CatalogScreen.CATEGORIES and kind in {
        CatalogActionKind.NEXT_PAGE,
        CatalogActionKind.PREVIOUS_PAGE,
    }:
        proposed = (
            action.offset if action.offset is not None else state.categories_offset
        )
        changes["categories_offset"] = normalize_offset(
            proposed, total_items or 0, CATEGORIES_PAGE_SIZE
        )
    elif screen is CatalogScreen.DISH_LIST and kind in {
        CatalogActionKind.NEXT_PAGE,
        CatalogActionKind.PREVIOUS_PAGE,
    }:
        proposed = action.offset if action.offset is not None else state.offset
        changes["offset"] = normalize_offset(proposed, total_items or 0)
    else:
        raise ValueError(f"invalid catalog transition: {screen}/{kind}")
    return state.model_copy(update=changes)
