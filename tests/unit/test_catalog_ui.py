from copy import deepcopy
from uuid import UUID

import pytest

from app.catalog_ui.callbacks import decode_callback, encode_callback, resolve_callback
from app.catalog_ui.models import (
    CatalogAction,
    CatalogActionKind,
    CatalogScreen,
    CatalogUiState,
)
from app.catalog_ui.navigation import navigate, normalize_offset
from app.catalog_ui.rendering import (
    RenderData,
    acknowledge_render,
    format_eur,
    prepare_render,
    render_screen,
)
from app.menu.contracts import (
    BrowseMenuItem,
    BrowseMenuResult,
    CartInspection,
    CategoryList,
    CategorySummary,
    DishDetail,
    InspectedCartItem,
)

DISH_ID = UUID("11111111-1111-1111-1111-111111111111")
OTHER_ID = UUID("22222222-2222-2222-2222-222222222222")


def action(kind: CatalogActionKind, **kwargs: object) -> CatalogAction:
    return CatalogAction(kind=kind, **kwargs)


def activate(state: CatalogUiState) -> CatalogUiState:
    """Test helper standing in for a successful delivery acknowledgement."""
    return state.model_copy(update={"active_screen": state.requested_screen})


def page(*, offset: int = 0, has_more: bool = False) -> BrowseMenuResult:
    return BrowseMenuResult(
        category="Супы & салаты",
        items=[
            BrowseMenuItem(
                dish_id=DISH_ID,
                name="Томатный <суп>",
                price_minor=1234,
                currency="EUR",
            )
        ],
        offset=offset,
        limit=5,
        has_more=has_more,
    )


def inspection(*, unavailable: bool = False, missing: bool = False) -> CartInspection:
    return CartInspection(
        items=[
            InspectedCartItem(
                dish_id=DISH_ID,
                name=None if missing else "Суп & хлеб",
                quantity=2,
                unit_price_minor=None if missing else 1000,
                line_total_minor=None if missing else 2000,
                available=not unavailable and not missing,
                missing=missing,
            )
        ],
        available_subtotal_minor=0 if unavailable or missing else 2000,
        has_unavailable_items=unavailable or missing,
    )


def test_default_legacy_and_checkpoint_round_trip() -> None:
    default = CatalogUiState.from_checkpoint(None)
    assert default.active_screen is CatalogScreen.HOME
    assert default.pending_render is None
    restored = CatalogUiState.from_checkpoint(default.to_checkpoint())
    assert restored == default
    assert CatalogUiState.from_checkpoint({"unknown": True}) == default


def test_ui_revision_is_independent_and_state_has_no_business_fields() -> None:
    state = CatalogUiState(ui_revision=7)
    original_order = {
        "draft_revision": 9,
        "cart": [{"quantity": 2}],
        "contacts": {"name": "X"},
    }
    snapshot = deepcopy(original_order)
    prepared = prepare_render(
        state,
        render_screen(state, RenderData()),
        token="token001",
        draft_revision=9,
    )
    assert prepared.ui_revision == 8
    assert original_order == snapshot
    assert not (
        {"cart", "contacts", "quote_snapshot", "draft_revision"}
        & set(type(prepared).model_fields)
    )


def test_navigation_full_route_and_return_context() -> None:
    state = CatalogUiState()
    state = activate(navigate(state, action(CatalogActionKind.OPEN_CATEGORIES)))
    assert state.requested_screen is CatalogScreen.CATEGORIES
    state = activate(
        navigate(state, action(CatalogActionKind.OPEN_CATEGORY, target="Супы"))
    )
    state = state.model_copy(update={"offset": 5})
    state = activate(
        navigate(state, action(CatalogActionKind.OPEN_DISH, target=str(DISH_ID)))
    )
    assert state.requested_screen is CatalogScreen.DISH_DETAIL
    state = activate(navigate(state, action(CatalogActionKind.BACK)))
    assert (state.requested_screen, state.offset) == (CatalogScreen.DISH_LIST, 5)
    state = activate(navigate(state, action(CatalogActionKind.OPEN_CART)))
    assert state.cart_return is not None and state.cart_return.offset == 5
    state = activate(navigate(state, action(CatalogActionKind.CONTINUE_CATALOG)))
    assert (state.requested_screen, state.category_id, state.offset) == (
        CatalogScreen.DISH_LIST,
        "Супы",
        5,
    )
    state = activate(navigate(state, action(CatalogActionKind.BACK)))
    state = activate(navigate(state, action(CatalogActionKind.BACK)))
    assert state.requested_screen is CatalogScreen.HOME


@pytest.mark.parametrize(
    ("offset", "total", "expected"),
    [(-5, 12, 0), (999, 12, 10), (7, 12, 5), ("bad", 12, 0), (10, 0, 0)],
)
def test_offset_normalization(offset: object, total: int, expected: int) -> None:
    assert normalize_offset(offset, total) == expected


def test_invalid_navigation_fails_closed() -> None:
    with pytest.raises(ValueError, match="invalid catalog transition"):
        navigate(
            CatalogUiState(), action(CatalogActionKind.OPEN_DISH, target=str(DISH_ID))
        )


def test_render_home_and_empty_categories() -> None:
    home = CatalogUiState()
    assert [
        button.label for row in render_screen(home, RenderData()).rows for button in row
    ] == ["Меню"]
    with_cart = render_screen(home, RenderData(cart=inspection()))
    assert "Корзина" in [button.label for row in with_cart.rows for button in row]
    categories = navigate(home, action(CatalogActionKind.OPEN_CATEGORIES))
    rendered = render_screen(
        categories, RenderData(categories=CategoryList(categories=[]))
    )
    assert "нет" in rendered.text


def test_render_categories_is_deterministic() -> None:
    state = activate(
        navigate(CatalogUiState(), action(CatalogActionKind.OPEN_CATEGORIES))
    )
    data = RenderData(
        categories=CategoryList(
            categories=[CategorySummary(id="Супы", name="Супы", available_dish_count=3)]
        )
    )
    rendered = render_screen(state, data)
    assert rendered.rows[0][0].label == "Супы · 3"
    assert rendered.rows[0][0].action.target == "Супы"


def test_render_dish_list_escapes_price_quantity_and_pagination() -> None:
    state = activate(
        navigate(CatalogUiState(), action(CatalogActionKind.OPEN_CATEGORIES))
    )
    state = activate(
        navigate(state, action(CatalogActionKind.OPEN_CATEGORY, target="Супы & салаты"))
    )
    rendered = render_screen(
        state,
        RenderData(menu_page=page(has_more=True), cart_quantities={str(DISH_ID): 2}),
    )
    assert "Супы &amp; салаты" in rendered.text
    assert "Томатный &lt;суп&gt; — 12,34 € · в корзине: 2" in rendered.text
    assert any(
        button.action.kind is CatalogActionKind.NEXT_PAGE
        for row in rendered.rows
        for button in row
    )
    assert format_eur(5) == "0,05 €"
    assert format_eur(5, "USD") == "Цена недоступна"


def test_render_detail_uses_only_provided_optional_facts() -> None:
    state = CatalogUiState(
        active_screen=CatalogScreen.DISH_DETAIL,
        requested_screen=CatalogScreen.DISH_DETAIL,
        selected_dish_id=str(DISH_ID),
    )
    dish = DishDetail(
        dish_id=DISH_ID,
        name="Суп",
        category="Супы",
        dietary_tags=[],
        price_minor=900,
        currency="EUR",
        description=None,
        ingredients=[],
        allergens=[],
        available=True,
    )
    rendered = render_screen(state, RenderData(dish=dish))
    assert "Описание не указано" in rendered.text
    assert "Аллергены:" not in rendered.text
    assert "Особенности:" not in rendered.text


@pytest.mark.parametrize(
    ("cart", "available"),
    [
        (inspection(), True),
        (inspection(unavailable=True), False),
        (inspection(missing=True), False),
    ],
)
def test_render_cart_checkout_state(cart: CartInspection, available: bool) -> None:
    state = CatalogUiState(
        active_screen=CatalogScreen.CART, requested_screen=CatalogScreen.CART
    )
    rendered = render_screen(state, RenderData(cart=cart))
    assert rendered.checkout_available is available
    assert "не окончательная стоимость" in rendered.text
    actions = [button.action.kind for row in rendered.rows for button in row]
    assert (CatalogActionKind.CHECKOUT in actions) is available
    if cart.items[0].missing:
        assert "missing" in rendered.text


def test_long_values_are_bounded_and_escaped() -> None:
    state = navigate(CatalogUiState(), action(CatalogActionKind.OPEN_CATEGORIES))
    data = RenderData(
        categories=CategoryList(
            categories=[
                CategorySummary(id="x", name="<&" + "я" * 200, available_dish_count=1)
            ]
        )
    )
    rendered = render_screen(state, data)
    assert len(rendered.rows[0][0].label) <= 64


def test_callback_round_trip_and_strict_rejection() -> None:
    payload = encode_callback("token001", 35)
    assert decode_callback(payload) == ("token001", 35)
    assert len(payload.encode("utf-8")) <= 64
    for invalid in [
        "order:confirm:1",
        "cat:2:token001:0",
        "cat:1:short:0",
        "cat:1:token001:!",
        None,
    ]:
        assert decode_callback(invalid) is None
    with pytest.raises(ValueError):
        encode_callback("токен000", 0)


def test_two_phase_registry_ack_and_stale_isolation() -> None:
    state = CatalogUiState()
    rendered = render_screen(state, RenderData())
    prepared = prepare_render(state, rendered, token="token001", draft_revision=4)
    assert prepared.active_screen is CatalogScreen.HOME
    assert prepared.active_token is None
    payload = prepared.pending_render.rendered.rows[0][0].callback_data
    assert resolve_callback(prepared, payload) is None
    with pytest.raises(ValueError):
        acknowledge_render(prepared, token="wrong000")
    active = acknowledge_render(prepared, token="token001", message_id=10)
    assert resolve_callback(active, payload).kind is CatalogActionKind.OPEN_CATEGORIES

    next_state = navigate(active, action(CatalogActionKind.OPEN_CATEGORIES))
    next_render = prepare_render(
        next_state,
        render_screen(next_state, RenderData()),
        token="token002",
    )
    assert (
        resolve_callback(next_render, payload).kind is CatalogActionKind.OPEN_CATEGORIES
    )
    next_active = acknowledge_render(next_render, token="token002", message_id=10)
    assert resolve_callback(next_active, payload) is None
    assert len(next_active.active_actions) <= 80


def test_future_mutation_is_declarative_and_does_not_touch_cart() -> None:
    state = CatalogUiState(
        active_screen=CatalogScreen.DISH_LIST,
        requested_screen=CatalogScreen.DISH_LIST,
        category_id="Супы",
    )
    cart = [{"dish_id": str(OTHER_ID), "quantity": 3}]
    original = deepcopy(cart)
    prepared = prepare_render(
        state,
        render_screen(state, RenderData(menu_page=page())),
        token="token001",
        draft_revision=12,
    )
    mutation = next(
        a
        for a in prepared.pending_render.actions
        if a.kind is CatalogActionKind.INCREMENT
    )
    assert mutation.draft_revision == 12
    assert mutation.requires_acknowledged_screen is True
    assert cart == original

    active = acknowledge_render(prepared, token="token001", message_id=42)
    index = active.active_actions.index(mutation)
    payload = encode_callback("token001", index)
    assert resolve_callback(active, payload) is None
    assert resolve_callback(active, payload, message_id=41, draft_revision=12) is None
    assert resolve_callback(active, payload, message_id=42, draft_revision=11) is None
    assert (
        resolve_callback(active, payload, message_id=42, draft_revision=12) == mutation
    )


@pytest.mark.parametrize("count", [0, 1, 10, 11, 38, 39, 100])
def test_category_pages_cover_every_target(count: int) -> None:
    categories = CategoryList(
        categories=[
            CategorySummary(
                id=f"category-{i}", name=f"Category {i}", available_dish_count=1
            )
            for i in range(count)
        ]
    )
    state = CatalogUiState(
        active_screen=CatalogScreen.CATEGORIES,
        requested_screen=CatalogScreen.CATEGORIES,
    )
    found = []
    for page_index in range(max(1, (count + 9) // 10)):
        rendered = render_screen(state, RenderData(categories=categories))
        buttons = [b for row in rendered.rows for b in row]
        targets = [
            b.action.target
            for b in buttons
            if b.action.kind is CatalogActionKind.OPEN_CATEGORY
        ]
        assert len(targets) <= 10
        found.extend(targets)
        assert any(b.action.kind is CatalogActionKind.BACK for b in buttons)
        assert any(
            b.action.kind is CatalogActionKind.PREVIOUS_PAGE for b in buttons
        ) == (page_index > 0)
        has_next = (page_index + 1) * 10 < count
        assert (
            any(b.action.kind is CatalogActionKind.NEXT_PAGE for b in buttons)
            == has_next
        )
        prepared = prepare_render(state, rendered, token=f"token{page_index:03d}")
        assert len(prepared.pending_render.actions) <= 13
        assert all(
            len(b.callback_data.encode("utf-8")) <= 64
            and "category-" not in b.callback_data
            for row in prepared.pending_render.rendered.rows
            for b in row
        )
        if has_next:
            next_action = next(
                b.action
                for b in buttons
                if b.action.kind is CatalogActionKind.NEXT_PAGE
            )
            state = activate(navigate(state, next_action, total_items=count))
    assert found == [f"category-{i}" for i in range(count)]


def test_category_position_roundtrip_and_normalization() -> None:
    legacy = CatalogUiState.from_checkpoint({"active_screen": "categories"})
    assert legacy.categories_offset == 0
    state = CatalogUiState(
        active_screen=CatalogScreen.CATEGORIES,
        requested_screen=CatalogScreen.CATEGORIES,
        categories_offset=20,
        offset=5,
    )
    assert CatalogUiState.from_checkpoint(state.to_checkpoint()) == state
    state = activate(
        navigate(state, action(CatalogActionKind.OPEN_CATEGORY, target="cat"))
    )
    state = state.model_copy(update={"offset": 15})
    state = activate(navigate(state, action(CatalogActionKind.BACK)))
    assert state.categories_offset == 20
    state = navigate(
        state, action(CatalogActionKind.PREVIOUS_PAGE, offset=999), total_items=11
    )
    assert state.categories_offset == 10
    assert state.offset == 0
    data = RenderData(
        categories=CategoryList(
            categories=[
                CategorySummary(id=str(i), name=str(i), available_dish_count=1)
                for i in range(11)
            ]
        )
    )
    shrunk = state.model_copy(update={"categories_offset": 90})
    rendered = render_screen(shrunk, data)
    assert [
        b.action.target
        for row in rendered.rows
        for b in row
        if b.action.kind is CatalogActionKind.OPEN_CATEGORY
    ] == ["10"]


def test_category_previous_and_dish_offsets_are_independent() -> None:
    state = CatalogUiState(
        active_screen=CatalogScreen.CATEGORIES,
        requested_screen=CatalogScreen.CATEGORIES,
        categories_offset=10,
        offset=15,
    )
    previous = navigate(
        state, action(CatalogActionKind.PREVIOUS_PAGE, offset=0), total_items=20
    )
    assert (previous.categories_offset, previous.offset) == (0, 15)
    selected = activate(
        navigate(state, action(CatalogActionKind.OPEN_CATEGORY, target="cat"))
    )
    selected = selected.model_copy(update={"offset": 10})
    detail = activate(
        navigate(selected, action(CatalogActionKind.OPEN_DISH, target=str(DISH_ID)))
    )
    returned = navigate(detail, action(CatalogActionKind.BACK))
    assert (returned.categories_offset, returned.offset) == (10, 10)


@pytest.mark.parametrize(
    "kind", [CatalogActionKind.INCREMENT, CatalogActionKind.DECREMENT]
)
def test_mutation_blocked_during_pending_but_navigation_allowed(
    kind: CatalogActionKind,
) -> None:
    order = {
        "cart": [{"dish_id": str(DISH_ID), "quantity": 2}],
        "contacts": {"name": "synthetic"},
        "quote": None,
        "draft_revision": 12,
    }
    before = deepcopy(order)
    state = CatalogUiState(
        active_screen=CatalogScreen.DISH_LIST,
        requested_screen=CatalogScreen.DISH_LIST,
        category_id="cat",
    )
    active = acknowledge_render(
        prepare_render(
            state,
            render_screen(state, RenderData(menu_page=page())),
            token="token001",
            draft_revision=12,
        ),
        token="token001",
        message_id=42,
    )
    mutation_index = next(
        i for i, a in enumerate(active.active_actions) if a.kind is kind
    )
    navigation_index = next(
        i
        for i, a in enumerate(active.active_actions)
        if a.kind is CatalogActionKind.OPEN_DISH
    )
    mutation_payload = encode_callback("token001", mutation_index)
    nav_payload = encode_callback("token001", navigation_index)
    assert (
        resolve_callback(active, mutation_payload, message_id=42, draft_revision=12)
        is not None
    )
    pending = prepare_render(
        active,
        render_screen(active, RenderData(menu_page=page())),
        token="token002",
        draft_revision=12,
    )
    assert (
        resolve_callback(pending, mutation_payload, message_id=42, draft_revision=12)
        is None
    )
    assert resolve_callback(pending, nav_payload) is not None
    new_payload = encode_callback("token002", mutation_index)
    assert (
        resolve_callback(pending, new_payload, message_id=43, draft_revision=12) is None
    )
    updated = acknowledge_render(pending, token="token002", message_id=43)
    assert (
        resolve_callback(updated, mutation_payload, message_id=42, draft_revision=12)
        is None
    )
    assert (
        resolve_callback(updated, new_payload, message_id=43, draft_revision=12)
        is not None
    )
    assert (
        resolve_callback(updated, new_payload, message_id=43, draft_revision=11) is None
    )
    assert resolve_callback(updated, new_payload, draft_revision=12) is None
    assert order == before
