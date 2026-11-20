from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from app.db.models import MenuItem
from app.menu.contracts import BrowseMenuRequest, CartItemInput, SearchMenuRequest
from app.menu.repository import _escape_like
from app.menu.service import CartValidationError, DishNotFoundError, MenuService


def menu_item(
    *,
    item_id: UUID | None = None,
    name: str = "Паста",
    price_minor: int = 1250,
    currency: str = "EUR",
    available: bool = True,
    dietary_tags: list[str] | None = None,
    category: str = "main",
) -> MenuItem:
    return MenuItem(
        id=item_id or uuid4(),
        source_row_id=uuid4().hex,
        name=name,
        description="Синтетическое блюдо",
        ingredients=["паста", "томат"],
        allergens=["глютен"],
        dietary_tags=dietary_tags or [],
        category=category,
        price_minor=price_minor,
        currency=currency,
        available=available,
    )


class FakeRepository:
    def __init__(self, items: list[MenuItem]) -> None:
        self.items = {item.id: item for item in items}
        self.last_search: SearchMenuRequest | None = None
        self.get_many_calls = 0

    async def search(self, request: SearchMenuRequest) -> list[MenuItem]:
        self.last_search = request
        return list(self.items.values())

    async def list_categories(self) -> list[tuple[str, int]]:
        counts: dict[str, int] = {}
        for item in self.items.values():
            if item.available:
                counts[item.category] = counts.get(item.category, 0) + 1
        return sorted(counts.items(), key=lambda pair: (pair[0].casefold(), pair[0]))

    async def browse(self, request: BrowseMenuRequest) -> list[MenuItem]:
        if request.category is None:
            return []
        matches = sorted(
            (
                item
                for item in self.items.values()
                if item.available
                and item.category.casefold() == request.category.casefold()
            ),
            key=lambda item: (item.name.casefold(), item.id),
        )
        return matches[request.offset : request.offset + request.limit + 1]

    async def get(self, dish_id: UUID) -> MenuItem | None:
        return self.items.get(dish_id)

    async def get_many(self, dish_ids: list[UUID] | tuple[UUID, ...]) -> list[MenuItem]:
        self.get_many_calls += 1
        return [self.items[item_id] for item_id in dish_ids if item_id in self.items]


@pytest.mark.asyncio
async def test_search_normalizes_filters_and_returns_integer_prices() -> None:
    item = menu_item(dietary_tags=["vegan"])
    repository = FakeRepository([item])
    service = MenuService(repository)

    response = await service.search_menu("  томатная   паста ", " Main ", [" Vegan "])
    assert response.items[0].price_minor == 1250
    assert response.items[0].dish_id == item.id
    assert repository.last_search == SearchMenuRequest(
        query="томатная паста", category="Main", dietary_tags=["vegan"]
    )


def test_contracts_accept_blank_as_browse_and_reject_duplicate_and_oversized_values() -> (
    None
):
    assert SearchMenuRequest(query="   ").query is None
    with pytest.raises(ValidationError):
        SearchMenuRequest(query="паста", dietary_tags=["Vegan", " vegan "])
    with pytest.raises(ValidationError):
        CartItemInput(dish_id=uuid4(), quantity=21)


def test_search_escapes_sql_like_wildcards() -> None:
    assert _escape_like("50%_\\") == "50\\%\\_\\\\"


@pytest.mark.asyncio
async def test_get_dish_reports_availability_and_missing() -> None:
    item = menu_item(available=False)
    service = MenuService(FakeRepository([item]))

    detail = await service.get_dish(item.id)
    assert detail.available is False
    assert detail.ingredients == ["паста", "томат"]
    with pytest.raises(DishNotFoundError):
        await service.get_dish(uuid4())


@pytest.mark.asyncio
async def test_cart_aggregates_duplicates_and_uses_integer_minor_units() -> None:
    first = menu_item(name="Паста", price_minor=1250)
    second = menu_item(name="Суп", price_minor=700)
    service = MenuService(FakeRepository([first, second]))

    quote = await service.calculate_cart(
        [
            CartItemInput(dish_id=first.id, quantity=2),
            CartItemInput(dish_id=second.id, quantity=1),
            CartItemInput(dish_id=first.id, quantity=1),
        ]
    )

    assert quote.currency == "EUR"
    assert quote.subtotal_minor == 4450
    assert [line.quantity for line in quote.items] == [3, 1]
    assert quote.items[0].line_total_minor == 3750


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("item_factory", "message"),
    [
        (lambda: None, "не найдены"),
        (lambda: menu_item(available=False), "недоступны"),
        (lambda: menu_item(currency="USD"), "валютой"),
    ],
)
async def test_cart_rejects_missing_unavailable_and_non_eur(
    item_factory, message: str
) -> None:
    dish_id = uuid4()
    item = item_factory()
    if item is not None:
        item.id = dish_id
    service = MenuService(FakeRepository([item] if item is not None else []))

    with pytest.raises(CartValidationError, match=message):
        await service.calculate_cart([CartItemInput(dish_id=dish_id, quantity=1)])


@pytest.mark.asyncio
async def test_cart_limits_apply_after_duplicate_aggregation() -> None:
    item = menu_item()
    service = MenuService(FakeRepository([item]))
    with pytest.raises(CartValidationError, match="одного блюда"):
        await service.calculate_cart(
            [
                CartItemInput(dish_id=item.id, quantity=11),
                CartItemInput(dish_id=item.id, quantity=10),
            ]
        )


@pytest.mark.asyncio
async def test_categories_and_browse_are_available_only_stable_and_paginated() -> None:
    rows = [
        menu_item(name=f"Блюдо {number:02d}", category="Основные")
        for number in range(23)
    ]
    rows.extend(
        [
            menu_item(name="Салат", category="Салаты"),
            menu_item(name="Скрытое", category="Пустая", available=False),
            menu_item(name="Недоступное", category="Основные", available=False),
        ]
    )
    service = MenuService(FakeRepository(rows))

    categories = await service.list_categories()
    assert [
        (item.id, item.name, item.available_dish_count)
        for item in categories.categories
    ] == [
        ("Основные", "Основные", 23),
        ("Салаты", "Салаты", 1),
    ]

    first = await service.browse_menu("Основные", offset=0, limit=20)
    second = await service.browse_menu("Основные", offset=20, limit=20)
    assert len(first.items) == 20
    assert first.has_more is True
    assert len(second.items) == 3
    assert second.has_more is False
    ids = [item.dish_id for item in first.items + second.items]
    assert len(ids) == len(set(ids)) == 23
    with pytest.raises(ValidationError):
        await service.browse_menu("   ")
    assert (await service.browse_menu("Нет такой категории")).items == []


@pytest.mark.asyncio
async def test_inspect_cart_preserves_order_and_reports_unavailable_and_missing() -> (
    None
):
    available = menu_item(name="Паста", price_minor=1250)
    unavailable = menu_item(name="Суп", price_minor=700, available=False)
    missing_id = uuid4()
    repository = FakeRepository([available, unavailable])
    service = MenuService(repository)

    inspection = await service.inspect_cart(
        [
            CartItemInput(dish_id=unavailable.id, quantity=2),
            CartItemInput(dish_id=missing_id, quantity=1),
            CartItemInput(dish_id=available.id, quantity=3),
        ]
    )

    assert [item.dish_id for item in inspection.items] == [
        unavailable.id,
        missing_id,
        available.id,
    ]
    assert inspection.items[0].line_total_minor == 1400
    assert inspection.items[0].available is False
    assert inspection.items[0].missing is False
    assert inspection.items[1].name is None
    assert inspection.items[1].unit_price_minor is None
    assert inspection.items[1].line_total_minor is None
    assert inspection.items[1].missing is True
    assert inspection.available_subtotal_minor == 3750
    assert inspection.has_unavailable_items is True
    assert repository.get_many_calls == 1


@pytest.mark.asyncio
async def test_inspect_cart_empty_and_existing_bounds() -> None:
    service = MenuService(FakeRepository([]))
    empty = await service.inspect_cart([])
    assert empty.items == []
    assert empty.available_subtotal_minor == 0
    assert empty.has_unavailable_items is False

    dish_id = uuid4()
    with pytest.raises(CartValidationError, match="одного блюда"):
        await service.inspect_cart(
            [
                CartItemInput(dish_id=dish_id, quantity=11),
                CartItemInput(dish_id=dish_id, quantity=10),
            ]
        )


@pytest.mark.asyncio
async def test_catalog_database_errors_are_not_masked_as_empty_results() -> None:
    class FailingRepository(FakeRepository):
        async def list_categories(self) -> list[tuple[str, int]]:
            raise RuntimeError("database unavailable")

    service = MenuService(FailingRepository([]))
    with pytest.raises(RuntimeError, match="database unavailable"):
        await service.list_categories()
