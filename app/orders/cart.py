from collections import OrderedDict
from uuid import UUID

from app.menu.contracts import MAX_CART_LINES, MAX_CART_UNITS, MAX_ITEM_QUANTITY
from app.orders.contracts import CartStateItem


class CartOperationError(ValueError):
    pass


def _quantities(items: list[CartStateItem]) -> OrderedDict[UUID, int]:
    return OrderedDict((item.dish_id, item.quantity) for item in items)


def _validated(quantities: OrderedDict[UUID, int]) -> list[CartStateItem]:
    quantities = OrderedDict((dish_id, quantity) for dish_id, quantity in quantities.items() if quantity > 0)
    if len(quantities) > MAX_CART_LINES:
        raise CartOperationError("В корзине слишком много разных блюд.")
    if any(quantity > MAX_ITEM_QUANTITY for quantity in quantities.values()):
        raise CartOperationError("Можно выбрать не более 20 порций одного блюда.")
    if sum(quantities.values()) > MAX_CART_UNITS:
        raise CartOperationError("В корзине может быть не более 100 порций.")
    return [CartStateItem(dish_id=dish_id, quantity=quantity) for dish_id, quantity in quantities.items()]


def add_items(current: list[CartStateItem], additions: list[CartStateItem]) -> list[CartStateItem]:
    values = _quantities(current)
    for item in additions:
        values[item.dish_id] = values.get(item.dish_id, 0) + item.quantity
    return _validated(values)


def set_quantity(current: list[CartStateItem], dish_id: UUID, quantity: int) -> list[CartStateItem]:
    values = _quantities(current)
    if dish_id not in values:
        raise CartOperationError("Этого блюда нет в корзине.")
    values[dish_id] = quantity
    return _validated(values)


def decrement(current: list[CartStateItem], dish_id: UUID) -> list[CartStateItem]:
    values = _quantities(current)
    if dish_id not in values:
        raise CartOperationError("Этого блюда нет в корзине.")
    values[dish_id] -= 1
    return _validated(values)


def remove_item(current: list[CartStateItem], dish_id: UUID) -> list[CartStateItem]:
    values = _quantities(current)
    if dish_id not in values:
        raise CartOperationError("Этого блюда нет в корзине.")
    del values[dish_id]
    return _validated(values)
