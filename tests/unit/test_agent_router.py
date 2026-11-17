import pytest

from app.agent.router import route_message


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("Хочу заказать пиццу", "order"),
        ("Добавь две пиццы в корзину", "order"),
        ("Оформи мне заказ", "order"),
        ("Хочу оформить заказ", "order"),
        ("Как оформить заказ?", "faq"),
        ("Я пока думаю о заказе и хочу узнать часы работы", "faq"),
        ("Во сколько открывается ресторан?", "faq"),
        ("", "unknown"),
        ("   ", "unknown"),
        ("?!", "unknown"),
    ],
)
def test_route_message(message: str, expected: str) -> None:
    assert route_message(message) == expected


def test_overlong_message_is_unknown() -> None:
    assert route_message("а" * 2001) == "unknown"
