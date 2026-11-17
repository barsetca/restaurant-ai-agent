import re

from app.agent.state import AgentIntent

MAX_USER_MESSAGE_CHARACTERS = 2000

# These patterns intentionally require an action/request to order. Merely asking
# about ordering (for example, "Как оформить заказ?") remains an FAQ route.
_ORDER_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\b(?:хочу|хотел(?:а)?\s+бы|мы\s+хотим)\s+(?:себе\s+)?заказать\b",
        r"\b(?:давай|давайте)\s+(?:я\s+)?(?:закажу|закажем|заказывать)\b",
        r"\b(?:хочу|хотел(?:а)?\s+бы|мы\s+хотим)\s+оформить\s+заказ\b",
        r"\b(?:добавь|добавьте|положи|положите)\b.{0,100}\b(?:в\s+корзин\w*|к\s+заказ\w*)\b",
        r"\b(?:оформи|оформите)\s+(?:мне\s+|нам\s+)?заказ\b",
        r"\b(?:закажи|закажите)\b",
    )
)


def route_message(message: str) -> AgentIntent:
    normalized = " ".join(message.casefold().split())
    if (
        not normalized
        or len(message) > MAX_USER_MESSAGE_CHARACTERS
        or not any(character.isalnum() for character in normalized)
    ):
        return "unknown"
    if any(pattern.search(normalized) for pattern in _ORDER_PATTERNS):
        return "order"
    return "faq"
