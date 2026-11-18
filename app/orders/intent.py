import logging
import re
from typing import Protocol

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from app.orders.contracts import OrderIntentProposal

logger = logging.getLogger(__name__)

INTENT_SYSTEM_PROMPT = """Ты классификатор намерений для русскоязычного ресторанного агента.
Верни только структурированный объект по схеме. Ты не изменяешь корзину и не считаешь цены.
Действия: faq, browse_menu, search_menu, dish_info, add, set_quantity, decrement,
remove, clear, view_cart, checkout, confirm, edit, cancel, new_order, unknown.
Для каждого блюда сохрани исходную словоформу в name, а в lookup_query верни краткую
словарную форму для поиска: именительный падеж, обычно единственное число. Не придумывай блюда.
Если намерение или блюдо неоднозначны, confidence=low и поясни clarification.
«убери один X» = decrement; «убери X из корзины» = remove.
Вопросы о часах, доставке и ресторане = faq."""


class OrderIntentExtractor(Protocol):
    async def extract(self, message: str) -> OrderIntentProposal: ...


class LlmOrderIntentExtractor:
    def __init__(self, api_key: str, model_name: str, timeout_seconds: float) -> None:
        model = ChatOpenAI(
            api_key=api_key,
            model=model_name,
            temperature=0,
            timeout=timeout_seconds,
            max_retries=1,
        )
        self._structured = model.with_structured_output(
            OrderIntentProposal,
            method="json_schema",
            strict=True,
        )

    async def extract(self, message: str) -> OrderIntentProposal:
        result = await self._structured.ainvoke(
            [SystemMessage(content=INTENT_SYSTEM_PROMPT), HumanMessage(content=message)]
        )
        if not isinstance(result, OrderIntentProposal):
            raise TypeError("OpenAI returned an invalid order intent")
        return result


_EXACT_ACTIONS: dict[str, str] = {
    "/start": "start",
    "новый заказ": "new_order",
    "оформить еще один заказ": "new_order",
    "оформить ещё один заказ": "new_order",
    "хочу заказать еще": "new_order",
    "хочу заказать ещё": "new_order",
    "покажи меню": "browse_menu",
    "показать меню": "browse_menu",
    "меню": "browse_menu",
    "покажи корзину": "view_cart",
    "покажи мой заказ": "view_cart",
    "моя корзина": "view_cart",
    "корзина": "view_cart",
    "сколько это стоит": "view_cart",
    "сколько это все будет стоить": "view_cart",
    "очисти корзину": "clear",
    "очистить корзину": "clear",
    "оформить заказ": "checkout",
    "хочу оформить заказ": "checkout",
    "давай оформим": "checkout",
    "перейти к оформлению": "checkout",
    "подтверждаю": "confirm",
    "редактировать": "edit",
    "изменить заказ": "edit",
    "отмена": "cancel",
    "отменить оформление": "cancel",
}


def deterministic_intent(message: str) -> OrderIntentProposal | None:
    normalized = re.sub(r"\s+", " ", message.casefold().strip().rstrip(".!?"))
    if normalized == "заказ":
        return OrderIntentProposal(
            action="unknown",
            confidence="low",
            clarification="Уточните: показать текущую корзину или перейти к оформлению?",
        )
    action = _EXACT_ACTIONS.get(normalized)
    if action is None:
        return None
    return OrderIntentProposal(action=action)


async def safe_extract(
    extractor: OrderIntentExtractor | None,
    message: str,
) -> OrderIntentProposal:
    deterministic = deterministic_intent(message)
    if deterministic is not None:
        return deterministic
    if extractor is None:
        return OrderIntentProposal(action="unknown", confidence="low")
    try:
        proposal = await extractor.extract(message)
    except Exception as exc:
        logger.error(
            "Order intent extraction failed",
            extra={"exception_type": type(exc).__name__},
        )
        return OrderIntentProposal(action="unknown", confidence="low")
    if proposal.confidence != "high":
        return OrderIntentProposal(
            action="unknown",
            confidence="low",
            clarification=proposal.clarification,
        )
    return proposal
