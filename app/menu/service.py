from collections import OrderedDict
from collections.abc import Sequence
from typing import Protocol
from uuid import UUID

from app.db.models import MenuItem
from app.menu.contracts import (
    DEFAULT_BROWSE_LIMIT,
    MAX_CART_LINES,
    MAX_CART_UNITS,
    MAX_ITEM_QUANTITY,
    SUPPORTED_CURRENCY,
    BrowseMenuItem,
    BrowseMenuRequest,
    BrowseMenuResult,
    CartInspection,
    CartItemInput,
    CartLine,
    CartQuote,
    CategoryList,
    CategorySummary,
    DishDetail,
    DishSummary,
    InspectedCartItem,
    MenuSearchResult,
    SearchMenuRequest,
)


class MenuRepositoryProtocol(Protocol):
    async def search(self, request: SearchMenuRequest) -> Sequence[MenuItem]: ...

    async def list_categories(self) -> Sequence[tuple[str, int]]: ...

    async def browse(self, request: BrowseMenuRequest) -> Sequence[MenuItem]: ...

    async def get(self, dish_id: UUID) -> MenuItem | None: ...

    async def get_many(self, dish_ids: Sequence[UUID]) -> Sequence[MenuItem]: ...


class MenuServiceError(Exception):
    public_message = "Не удалось обработать запрос меню."


class DishNotFoundError(MenuServiceError):
    public_message = "Блюдо не найдено."


class CartValidationError(MenuServiceError):
    def __init__(self, public_message: str) -> None:
        self.public_message = public_message
        super().__init__(public_message)


def _summary(item: MenuItem) -> DishSummary:
    return DishSummary(
        dish_id=item.id,
        name=item.name,
        category=item.category,
        dietary_tags=list(item.dietary_tags),
        price_minor=item.price_minor,
        currency=item.currency,
    )


class MenuService:
    def __init__(self, repository: MenuRepositoryProtocol) -> None:
        self._repository = repository

    async def search_menu(
        self,
        query: str | None = None,
        category: str | None = None,
        dietary_tags: list[str] | None = None,
    ) -> MenuSearchResult:
        request = SearchMenuRequest(
            query=query,
            category=category,
            dietary_tags=dietary_tags or [],
        )
        return MenuSearchResult(
            items=[_summary(item) for item in await self._repository.search(request)]
        )

    async def list_categories(self) -> CategoryList:
        categories = await self._repository.list_categories()
        return CategoryList(
            categories=[
                CategorySummary(id=name, name=name, available_dish_count=count)
                for name, count in categories
            ]
        )

    async def browse_menu(
        self,
        category: str,
        offset: int = 0,
        limit: int = DEFAULT_BROWSE_LIMIT,
    ) -> BrowseMenuResult:
        request = BrowseMenuRequest(category=category, offset=offset, limit=limit)
        rows = await self._repository.browse(request)
        has_more = len(rows) > request.limit
        return BrowseMenuResult(
            category=request.category,
            items=[
                BrowseMenuItem(
                    dish_id=item.id,
                    name=item.name,
                    price_minor=item.price_minor,
                    currency=item.currency,
                )
                for item in rows[: request.limit]
            ],
            offset=request.offset,
            limit=request.limit,
            has_more=has_more,
        )

    async def get_dish(self, dish_id: UUID) -> DishDetail:
        item = await self._repository.get(dish_id)
        if item is None:
            raise DishNotFoundError
        return DishDetail(
            **_summary(item).model_dump(),
            description=item.description,
            ingredients=list(item.ingredients),
            allergens=list(item.allergens),
            available=item.available,
        )

    async def calculate_cart(self, items: list[CartItemInput]) -> CartQuote:
        if not items:
            raise CartValidationError("Корзина не должна быть пустой.")

        quantities: OrderedDict[UUID, int] = OrderedDict()
        for item in items:
            quantities[item.dish_id] = quantities.get(item.dish_id, 0) + item.quantity
        if len(quantities) > MAX_CART_LINES:
            raise CartValidationError("В корзине слишком много разных блюд.")
        if any(quantity > MAX_ITEM_QUANTITY for quantity in quantities.values()):
            raise CartValidationError("Превышено допустимое количество одного блюда.")
        if sum(quantities.values()) > MAX_CART_UNITS:
            raise CartValidationError("В корзине превышено допустимое количество блюд.")

        menu_items = await self._repository.get_many(tuple(quantities))
        by_id = {item.id: item for item in menu_items}
        if any(dish_id not in by_id for dish_id in quantities):
            raise CartValidationError("Одно или несколько блюд не найдены.")
        if any(not by_id[dish_id].available for dish_id in quantities):
            raise CartValidationError("Одно или несколько блюд сейчас недоступны.")
        if any(by_id[dish_id].currency != SUPPORTED_CURRENCY for dish_id in quantities):
            raise CartValidationError(
                "Корзина содержит блюдо с неподдерживаемой валютой."
            )

        lines: list[CartLine] = []
        subtotal_minor = 0
        for dish_id, quantity in quantities.items():
            menu_item = by_id[dish_id]
            line_total_minor = menu_item.price_minor * quantity
            subtotal_minor += line_total_minor
            lines.append(
                CartLine(
                    dish_id=dish_id,
                    name=menu_item.name,
                    quantity=quantity,
                    unit_price_minor=menu_item.price_minor,
                    line_total_minor=line_total_minor,
                )
            )
        return CartQuote(items=lines, subtotal_minor=subtotal_minor)

    async def inspect_cart(self, items: list[CartItemInput]) -> CartInspection:
        quantities = self._validate_cart_bounds(items)
        if not items:
            return CartInspection(
                items=[],
                available_subtotal_minor=0,
                has_unavailable_items=False,
            )

        menu_items = await self._repository.get_many(tuple(quantities))
        by_id = {item.id: item for item in menu_items}
        if any(item.currency != SUPPORTED_CURRENCY for item in menu_items):
            raise CartValidationError(
                "Корзина содержит блюдо с неподдерживаемой валютой."
            )

        inspected: list[InspectedCartItem] = []
        available_subtotal_minor = 0
        has_unavailable_items = False
        for cart_item in items:
            menu_item = by_id.get(cart_item.dish_id)
            if menu_item is None:
                has_unavailable_items = True
                inspected.append(
                    InspectedCartItem(
                        dish_id=cart_item.dish_id,
                        name=None,
                        quantity=cart_item.quantity,
                        available=False,
                        missing=True,
                    )
                )
                continue
            line_total_minor = menu_item.price_minor * cart_item.quantity
            if menu_item.available:
                available_subtotal_minor += line_total_minor
            else:
                has_unavailable_items = True
            inspected.append(
                InspectedCartItem(
                    dish_id=cart_item.dish_id,
                    name=menu_item.name,
                    quantity=cart_item.quantity,
                    unit_price_minor=menu_item.price_minor,
                    line_total_minor=line_total_minor,
                    available=menu_item.available,
                    missing=False,
                )
            )
        return CartInspection(
            items=inspected,
            available_subtotal_minor=available_subtotal_minor,
            has_unavailable_items=has_unavailable_items,
        )

    @staticmethod
    def _validate_cart_bounds(items: list[CartItemInput]) -> OrderedDict[UUID, int]:
        if len(items) > MAX_CART_LINES:
            raise CartValidationError("В корзине слишком много разных блюд.")
        quantities: OrderedDict[UUID, int] = OrderedDict()
        for item in items:
            quantities[item.dish_id] = quantities.get(item.dish_id, 0) + item.quantity
        if len(quantities) > MAX_CART_LINES:
            raise CartValidationError("В корзине слишком много разных блюд.")
        if any(quantity > MAX_ITEM_QUANTITY for quantity in quantities.values()):
            raise CartValidationError("Превышено допустимое количество одного блюда.")
        if sum(quantities.values()) > MAX_CART_UNITS:
            raise CartValidationError("В корзине превышено допустимое количество блюд.")
        return quantities
