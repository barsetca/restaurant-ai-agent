import secrets
from collections.abc import Callable
from typing import Any, Protocol
from uuid import UUID

from app.agent.service import AgentService
from app.bot.catalog_delivery import CatalogDeliveryOutcome, deliver_catalog_screen
from app.catalog_ui import (
    CatalogActionKind,
    CatalogScreen,
    CatalogUiState,
    acknowledge_render,
    navigate,
    prepare_render,
    render_screen,
    resolve_callback,
)
from app.catalog_ui.rendering import RenderData
from app.catalog_ui.navigation import CATEGORIES_PAGE_SIZE, normalize_offset
from app.menu.contracts import CartInspection, CartItemInput


class CatalogMenu(Protocol):
    async def __aenter__(self) -> "CatalogMenu": ...
    async def __aexit__(self, *args: object) -> None: ...
    async def list_categories(self) -> Any: ...
    async def browse_menu(
        self, category: str, offset: int = 0, limit: int = 5
    ) -> Any: ...
    async def get_dish(self, dish_id: UUID) -> Any: ...
    async def inspect_cart(self, items: list[CartItemInput]) -> CartInspection: ...


_NAVIGATION_ACTIONS = {
    CatalogActionKind.OPEN_CATEGORIES,
    CatalogActionKind.OPEN_CATEGORY,
    CatalogActionKind.OPEN_DISH,
    CatalogActionKind.BACK,
    CatalogActionKind.OPEN_CART,
    CatalogActionKind.CONTINUE_CATALOG,
    CatalogActionKind.NEXT_PAGE,
    CatalogActionKind.PREVIOUS_PAGE,
}


def _cart_items(values: dict[str, Any]) -> list[CartItemInput]:
    result: list[CartItemInput] = []
    for item in values.get("cart", []):
        if not isinstance(item, dict):
            continue
        try:
            result.append(CartItemInput.model_validate(item))
        except (TypeError, ValueError):
            continue
    return result


def _fresh_token(state: CatalogUiState) -> str:
    excluded = {state.active_token, *state.retired_tokens}
    if state.pending_render is not None:
        excluded.add(state.pending_render.token)
    for _ in range(8):
        token = secrets.token_urlsafe(8)
        if token not in excluded:
            return token
    raise RuntimeError("unable to create a unique catalog token")


async def _render_data(
    menu: CatalogMenu,
    state: CatalogUiState,
    values: dict[str, Any],
) -> tuple[CatalogUiState, RenderData]:
    cart_items = _cart_items(values)
    cart = await menu.inspect_cart(cart_items)
    quantities = {str(item.dish_id): item.quantity for item in cart_items}
    if state.requested_screen is CatalogScreen.HOME:
        return state, RenderData(cart=cart)
    if state.requested_screen is CatalogScreen.CATEGORIES:
        categories = await menu.list_categories()
        state = state.model_copy(
            update={
                "categories_offset": normalize_offset(
                    state.categories_offset,
                    len(categories.categories),
                    CATEGORIES_PAGE_SIZE,
                )
            }
        )
        return state, RenderData(categories=categories, cart=cart)
    if state.requested_screen is CatalogScreen.DISH_LIST:
        category = state.category_id
        if not category:
            raise ValueError("catalog category is missing")
        page = await menu.browse_menu(category, state.offset, 5)
        if not page.items and state.offset > 0:
            recovered_offset = max(0, state.offset - 5)
            page = await menu.browse_menu(category, recovered_offset, 5)
            state = state.model_copy(update={"offset": recovered_offset})
        return state, RenderData(menu_page=page, cart=cart, cart_quantities=quantities)
    if state.requested_screen is CatalogScreen.DISH_DETAIL:
        if not state.selected_dish_id:
            raise ValueError("catalog dish is missing")
        dish = await menu.get_dish(UUID(state.selected_dish_id))
        return state, RenderData(dish=dish, cart=cart, cart_quantities=quantities)
    return state, RenderData(cart=cart)


async def run_catalog_update(
    *,
    service: AgentService,
    menu_factory: Callable[[], CatalogMenu],
    bot: Any,
    chat_id: int,
    conversation_id: str,
    update_id: int | None,
    payload: object | None = None,
    callback_message_id: int | None = None,
    recovery: bool = False,
) -> str:
    """Run prepare/delivery/ack under AgentService's single thread lock."""
    async with service.thread_lifecycle(conversation_id):
        values = await service.checkpoint_values_unlocked(conversation_id)
        state = CatalogUiState.from_checkpoint(values.get("catalog_ui"))
        if recovery:
            retired = list(state.retired_tokens)
            if state.active_token:
                retired.append(state.active_token)
            if state.pending_render is not None:
                retired.append(state.pending_render.token)
            state = state.model_copy(
                update={
                    "requested_screen": CatalogScreen.HOME,
                    "pending_render": None,
                    "active_token": None,
                    "active_actions": [],
                    "retired_tokens": retired[-32:],
                    "category_id": None,
                    "offset": 0,
                    "selected_dish_id": None,
                    "categories_offset": 0,
                    "cart_return": None,
                }
            )
        else:
            action = resolve_callback(
                state,
                payload,
                message_id=callback_message_id,
                draft_revision=values.get("draft_revision"),
            )
            if action is None:
                return "stale"
            if action.kind not in _NAVIGATION_ACTIONS:
                committed = await service.commit_catalog_unlocked(
                    conversation_id, update_id, state.to_checkpoint()
                )
                return "replay" if committed.replayed else "unavailable"
            total_items = None
            if state.active_screen is CatalogScreen.CATEGORIES and action.kind in {
                CatalogActionKind.NEXT_PAGE,
                CatalogActionKind.PREVIOUS_PAGE,
            }:
                async with menu_factory() as menu:
                    total_items = len((await menu.list_categories()).categories)
            state = navigate(state, action, total_items=total_items)

        async with menu_factory() as menu:
            state, data = await _render_data(menu, state, values)
        rendered = render_screen(state, data)
        prepared = prepare_render(
            state,
            rendered,
            token=_fresh_token(state),
            draft_revision=(
                values.get("draft_revision")
                if isinstance(values.get("draft_revision"), int)
                else None
            ),
        )
        committed = await service.commit_catalog_unlocked(
            conversation_id, update_id, prepared.to_checkpoint()
        )
        if committed.replayed:
            return "replay"
        pending = prepared.pending_render
        if pending is None:  # pragma: no cover - prepare_render invariant
            raise RuntimeError("catalog pending render is missing")
        delivered = await deliver_catalog_screen(
            bot,
            chat_id=chat_id,
            pending=pending,
            # A reply-keyboard «Меню» is an explicit recovery request. Telegram
            # cannot tell the bot that the client deleted its local history, so
            # editing an old acknowledged message may succeed invisibly. A new
            # user-initiated HOME is deliberately sent instead; inline catalog
            # navigation remains edit-first.
            active_message_id=None if recovery else state.active_message_id,
        )
        if delivered.outcome is not CatalogDeliveryOutcome.SUCCESS:
            return "unknown"
        acknowledged = acknowledge_render(
            prepared, token=pending.token, message_id=delivered.message_id
        )
        await service.acknowledge_catalog_unlocked(
            conversation_id, acknowledged.to_checkpoint()
        )
        return "delivered"
