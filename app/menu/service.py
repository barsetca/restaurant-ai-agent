from collections import OrderedDict
from collections.abc import Sequence
from typing import Protocol
from uuid import UUID

from app.db.models import MenuItem
from app.menu.contracts import (
    MAX_CART_LINES,
    MAX_CART_UNITS,
    MAX_ITEM_QUANTITY,
    SUPPORTED_CURRENCY,
    CartItemInput,
    CartLine,
    CartQuote,
    DishDetail,
    DishSummary,
    MenuSearchResult,
    SearchMenuRequest,
)


class MenuRepositoryProtocol(Protocol):
    async def search(self, request: SearchMenuRequest) -> Sequence[MenuItem]: ...

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
        query: str,
        category: str | None = None,
        dietary_tags: list[str] | None = None,
    ) -> MenuSearchResult:
        request = SearchMenuRequest(
            query=query,
            category=category,
            dietary_tags=dietary_tags or [],
        )
        return MenuSearchResult(items=[_summary(item) for item in await self._repository.search(request)])

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
            raise CartValidationError("Корзина содержит блюдо с неподдерживаемой валютой.")

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
