import json
import logging
import re
from collections.abc import Sequence
from typing import Protocol

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from pydantic import ValidationError

from app.menu.contracts import MAX_ITEM_QUANTITY
from app.orders.contracts import (
    ActionOutcome,
    ActionsOutcome,
    ApiContextualIntentResult,
    CartMutationOperation,
    ClarifyOutcome,
    ContextualOutcome,
    MAX_ACTION_OPERATIONS,
    OrderActionPlan,
    OrderContext,
    OrderIntentProposal,
    UnsupportedOutcome,
    ValidationOutcome,
    action_validation_rule,
)

logger = logging.getLogger(__name__)

INTENT_SYSTEM_PROMPT = """Ты контекстный классификатор намерений русскоязычного ресторанного агента.
Верни только структурированный outcome по схеме. Ты не изменяешь корзину и не считаешь цены.
Учитывай безопасный JSON-контекст: текущий этап, ожидаемый ввод, наличие корзины,
недавние семантические события и allowlist действий. Никогда не предлагай действие вне allowlist.
Действия: faq, browse_menu, search_menu, dish_info, add, set_quantity, decrement,
remove, clear, view_cart, checkout, cancel, new_order.
previous_order означает вопрос о ранее подтвержденном снимке заказа, а не о текущей корзине.
Для каждого блюда сохрани исходную словоформу в name, а в lookup_query верни краткую
словарную форму для поиска: именительный падеж, обычно единственное число. Не придумывай блюда.
Если намерение неоднозначно, верни clarify с кратким естественным вопросом на русском.
Если запрос не поддержан или контекста недостаточно, верни unsupported. Уточняющий текст
не должен повторять персональные данные, внутренние идентификаторы или вымышленные факты.
Для item actions заполняй items, а query/category/dietary_tags оставляй нейтральными.
Для search_menu/browse_menu используй selectors и оставляй items пустым.
Для действий без параметров оставляй items пустым, selectors — null/пустыми.
Если пользователь просит несколько разных изменений корзины, верни kind=actions и
упорядоченный operations. Multi-action разрешен только для add, set_quantity, decrement,
remove; каждое действие имеет собственные items. Не создавай операции для «не трогай»
или «оставь без изменений». Для одиночного действия верни прежний kind=action и оставь
operations пустым. Для kind=actions оставь корневые action/items/selectors нейтральными.
add означает увеличить количество: «еще одну» = quantity 1; set_quantity означает
установить итоговое количество и всегда требует quantity. Не заполняй неиспользуемые поля.
Сообщение о прошлом действии (например, «я добавлял ...») само по себе не является новой
командой изменить корзину: при неоднозначности уточни намерение.
«убери один X» = decrement; «убери X из корзины» = remove.
Вопросы о часах, доставке и ресторане = faq.

SAFE_ORDER_CONTEXT_JSON:
{context_json}"""

_EMAIL_PATTERN = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
_UUID_PATTERN = re.compile(
    r"(?i)\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b"
)
_TOKEN_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\b\d{8,12}:[A-Za-z0-9_-]{30,}\b"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]{16,}=*"),
)
_INTERNAL_TERM_PATTERN = re.compile(
    r"(?i)\b(?:uuid|thread[_ -]?id|callback|confirmation[_ -]?id|draft[_ -]?revision|session[_ -]?id)\b"
)
_SAFE_API_ERROR_FIELD = re.compile(r"^[A-Za-z0-9_.\[\]-]{1,160}$")
_REPAIRABLE_VALIDATION_RULES = frozenset(
    {
        "missing_items_for_item_action",
        "invalid_item_count_for_single_item_action",
        "missing_quantity_for_set_quantity",
        "missing_selector_for_search_menu",
    }
)
_ITEM_PAYLOAD_ACTIONS = frozenset(
    {"dish_info", "add", "set_quantity", "decrement", "remove"}
)
_SELECTOR_PAYLOAD_ACTIONS = frozenset({"search_menu", "browse_menu"})
_MAX_DEBUG_RESPONSE_JSON_CHARACTERS = 4000
QUANTITY_OUT_OF_RANGE_MESSAGE = (
    "Количество одного блюда не может превышать 20 порций. "
    "Корзина не изменена. Укажите допустимое количество"
)


def _safe_llm_error_fields(exc: Exception) -> dict[str, object]:
    fields: dict[str, object] = {}
    status_code = getattr(exc, "status_code", None)
    if isinstance(status_code, int) and 400 <= status_code <= 599:
        fields["llm_http_status"] = status_code
    for attribute, field_name in (
        ("code", "llm_api_code"),
        ("type", "llm_api_type"),
        ("param", "llm_api_param"),
    ):
        value = getattr(exc, attribute, None)
        if isinstance(value, str) and _SAFE_API_ERROR_FIELD.fullmatch(value):
            fields[field_name] = value
    return fields


def _safe_validation_error_fields(exc: Exception) -> dict[str, object]:
    if not isinstance(exc, ValidationError):
        return {}
    errors: list[dict[str, object]] = []
    for error in exc.errors(
        include_url=False,
        include_context=False,
        include_input=False,
    )[:8]:
        safe_location: list[str | int] = []
        for part in error.get("loc", ()):
            if isinstance(part, int):
                safe_location.append(part)
            elif isinstance(part, str) and _SAFE_API_ERROR_FIELD.fullmatch(part):
                safe_location.append(part)
            else:
                safe_location.append("redacted")
        error_type = error.get("type")
        if not (
            isinstance(error_type, str) and _SAFE_API_ERROR_FIELD.fullmatch(error_type)
        ):
            error_type = "redacted"
        errors.append({"loc": safe_location, "type": error_type})
    return {"llm_validation_errors": errors}


def contains_obvious_sensitive_data(value: str) -> bool:
    if _EMAIL_PATTERN.search(value) or _UUID_PATTERN.search(value):
        return True
    if any(pattern.search(value) for pattern in _TOKEN_PATTERNS):
        return True
    digits = re.sub(r"\D", "", value)
    return len(digits) >= 7


def build_intent_messages(
    message: str,
    context: OrderContext,
) -> Sequence[SystemMessage | HumanMessage]:
    return [
        SystemMessage(
            content=INTENT_SYSTEM_PROMPT.format(
                context_json=context.model_dump_json(),
            )
        ),
        HumanMessage(content=message),
    ]


class OrderIntentExtractor(Protocol):
    async def extract(
        self, message: str, context: OrderContext
    ) -> ContextualOutcome: ...


class RepairableDomainValidationError(ValueError):
    def __init__(self, validation_rule: str) -> None:
        self.validation_rule = validation_rule
        super().__init__(validation_rule)


class UnsafeStructuredOutcomeError(ValueError):
    pass


def _normalized_action_payload(
    result: ApiContextualIntentResult,
) -> dict[str, object]:
    if result.action in _ITEM_PAYLOAD_ACTIONS:
        return {
            "items": [item.model_dump(mode="python") for item in result.items],
            "query": None,
            "category": None,
            "dietary_tags": [],
        }
    if result.action in _SELECTOR_PAYLOAD_ACTIONS:
        return {
            "items": [],
            "query": result.query,
            "category": result.category,
            "dietary_tags": result.dietary_tags,
        }
    return {
        "items": [],
        "query": None,
        "category": None,
        "dietary_tags": [],
    }


def _api_result_contains_sensitive_data(result: ApiContextualIntentResult) -> bool:
    text_values = [
        result.query,
        result.category,
        result.question,
        *result.dietary_tags,
        *(item.name for item in result.items),
        *(item.lookup_query for item in result.items),
        *(item.name for operation in result.operations for item in operation.items),
        *(
            item.lookup_query
            for operation in result.operations
            for item in operation.items
        ),
    ]
    return any(
        contains_obvious_sensitive_data(value)
        for value in text_values
        if isinstance(value, str)
    )


def _safe_debug_response_json(result: ApiContextualIntentResult) -> str:
    def redacted(value: str | None) -> str | None:
        return "[REDACTED]" if value is not None else None

    payload = {
        "kind": result.kind,
        "action": result.action,
        "items": [
            {
                "name": "[REDACTED]",
                "lookup_query": redacted(item.lookup_query),
                "quantity": item.quantity,
            }
            for item in result.items
        ],
        "query": redacted(result.query),
        "category": redacted(result.category),
        "dietary_tags": ["[REDACTED]" for _ in result.dietary_tags],
        "operations": [
            {
                "action": operation.action,
                "items": [
                    {
                        "name": "[REDACTED]",
                        "lookup_query": redacted(item.lookup_query),
                        "quantity": item.quantity,
                    }
                    for item in operation.items
                ],
            }
            for operation in result.operations
        ],
        "clarification_type": result.clarification_type,
        "question": redacted(result.question),
        "reason": result.reason,
    }
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    if len(encoded) <= _MAX_DEBUG_RESPONSE_JSON_CHARACTERS:
        return encoded
    return json.dumps(
        {
            "truncated": True,
            "kind": result.kind,
            "action": result.action,
            "items_count": len(result.items),
            "dietary_tags_count": len(result.dietary_tags),
            "operations_count": len(result.operations),
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _api_result_validation_facts(
    result: ApiContextualIntentResult,
    normalized_payload: dict[str, object],
) -> tuple[bool, bool, str | None]:
    normalized_items = normalized_payload["items"]
    if not isinstance(normalized_items, list):
        raise TypeError("Normalized items must be a list")
    quantities = [
        item.get("quantity") for item in normalized_items if isinstance(item, dict)
    ]
    quantity_present = bool(quantities) and all(
        quantity is not None for quantity in quantities
    )
    quantities_in_range = all(
        quantity is None or 1 <= quantity <= MAX_ITEM_QUANTITY
        for quantity in quantities
    )
    quantity_valid = quantities_in_range and (
        result.action != "set_quantity" or quantity_present
    )

    if result.kind == "action":
        if (
            result.action is None
            or result.clarification_type is not None
            or result.question is not None
            or result.reason is not None
            or result.operations
        ):
            return quantity_present, quantity_valid, "invalid_action_variant_payload"
        if not quantities_in_range:
            return quantity_present, quantity_valid, "quantity_out_of_range"
        rule = action_validation_rule(
            result.action,
            items_count=len(normalized_items),
            quantities=quantities,
            query_present=normalized_payload["query"] is not None,
            category_present=normalized_payload["category"] is not None,
            dietary_tags_count=len(normalized_payload["dietary_tags"]),
        )
        return quantity_present, quantity_valid, rule
    if result.kind == "actions":
        root_is_neutral = (
            result.action is None
            and not result.items
            and result.query is None
            and result.category is None
            and not result.dietary_tags
            and result.clarification_type is None
            and result.question is None
            and result.reason is None
        )
        if not root_is_neutral:
            return False, False, "invalid_actions_variant_payload"
        if not 2 <= len(result.operations) <= MAX_ACTION_OPERATIONS:
            return False, False, "invalid_action_plan_count"
        all_quantities: list[int | None] = []
        for operation in result.operations:
            operation_quantities = [item.quantity for item in operation.items]
            all_quantities.extend(operation_quantities)
            if any(
                quantity is not None and not 1 <= quantity <= MAX_ITEM_QUANTITY
                for quantity in operation_quantities
            ):
                return bool(all_quantities), False, "quantity_out_of_range"
            rule = action_validation_rule(
                operation.action,
                items_count=len(operation.items),
                quantities=operation_quantities,
                query_present=False,
                category_present=False,
                dietary_tags_count=0,
            )
            if rule is not None:
                return bool(all_quantities), False, rule
        present = bool(all_quantities) and all(
            quantity is not None for quantity in all_quantities
        )
        valid = all(
            operation.action != "set_quantity"
            or all(item.quantity is not None for item in operation.items)
            for operation in result.operations
        )
        return present, valid, None
    if result.kind == "clarify":
        valid = (
            result.action is None
            and not result.items
            and result.query is None
            and result.category is None
            and not result.dietary_tags
            and not result.operations
            and result.clarification_type is not None
            and result.question is not None
            and result.reason is None
        )
        return (
            quantity_present,
            quantity_valid,
            (None if valid else "invalid_clarify_variant_payload"),
        )
    valid = (
        result.action is None
        and not result.items
        and result.query is None
        and result.category is None
        and not result.dietary_tags
        and not result.operations
        and result.clarification_type is None
        and result.question is None
        and result.reason is not None
    )
    return (
        quantity_present,
        quantity_valid,
        (None if valid else "invalid_unsupported_variant_payload"),
    )


def _has_quantity_above_max(result: ApiContextualIntentResult) -> bool:
    quantities = [item.quantity for item in result.items]
    quantities.extend(
        item.quantity for operation in result.operations for item in operation.items
    )
    return any(
        quantity is not None and quantity > MAX_ITEM_QUANTITY for quantity in quantities
    )


def api_result_to_outcome(result: ApiContextualIntentResult) -> ContextualOutcome:
    normalized_payload = _normalized_action_payload(result)
    quantity_present, quantity_valid, validation_rule = _api_result_validation_facts(
        result, normalized_payload
    )
    diagnostic_fields: dict[str, object] = {
        "kind": result.kind,
        "items_count": len(result.items)
        + sum(len(operation.items) for operation in result.operations),
        "quantity_present": quantity_present,
        "quantity_valid": quantity_valid,
        "validation_rule": validation_rule or "none",
    }
    if result.action is not None:
        diagnostic_fields["action"] = result.action
    logger.info("Order domain validation diagnostic", extra=diagnostic_fields)

    if validation_rule in _REPAIRABLE_VALIDATION_RULES:
        raise RepairableDomainValidationError(validation_rule)

    if _api_result_contains_sensitive_data(result):
        raise UnsafeStructuredOutcomeError

    if validation_rule == "quantity_out_of_range" and _has_quantity_above_max(result):
        return ValidationOutcome(validation_rule="quantity_out_of_range")

    if result.kind == "action":
        if (
            result.action is None
            or result.clarification_type is not None
            or result.question is not None
            or result.reason is not None
            or result.operations
        ):
            raise ValueError("Invalid structured action outcome")
        return ActionOutcome.model_validate(
            {"action": result.action, **normalized_payload}
        )
    if result.kind == "actions":
        if (
            result.action is not None
            or result.items
            or result.query is not None
            or result.category is not None
            or result.dietary_tags
            or result.clarification_type is not None
            or result.question is not None
            or result.reason is not None
        ):
            raise ValueError("Invalid structured actions outcome")
        return ActionsOutcome(
            operations=[
                CartMutationOperation(
                    action=operation.action,
                    items=[item.model_dump(mode="python") for item in operation.items],
                )
                for operation in result.operations
            ]
        )
    if result.kind == "clarify":
        if (
            result.action is not None
            or result.items
            or result.query is not None
            or result.category is not None
            or result.dietary_tags
            or result.operations
            or result.clarification_type is None
            or result.question is None
            or result.reason is not None
        ):
            raise ValueError("Invalid structured clarification outcome")
        return ClarifyOutcome(
            clarification_type=result.clarification_type,
            question=result.question,
        )
    if (
        result.action is not None
        or result.items
        or result.query is not None
        or result.category is not None
        or result.dietary_tags
        or result.operations
        or result.clarification_type is not None
        or result.question is not None
        or result.reason is None
    ):
        raise ValueError("Invalid structured unsupported outcome")
    return UnsupportedOutcome(reason=result.reason)


class LlmOrderIntentExtractor:
    def __init__(
        self,
        api_key: str,
        model_name: str,
        timeout_seconds: float,
        *,
        debug_response_json: bool = False,
    ) -> None:
        model = ChatOpenAI(
            api_key=api_key,
            model=model_name,
            temperature=0,
            timeout=timeout_seconds,
            max_retries=1,
        )
        self._structured = model.with_structured_output(
            ApiContextualIntentResult,
            method="json_schema",
            strict=True,
        )
        self._debug_response_json = debug_response_json

    async def extract(self, message: str, context: OrderContext) -> ContextualOutcome:
        messages = list(build_intent_messages(message, context))
        for attempt in range(2):
            result = await self._structured.ainvoke(messages)
            if not isinstance(result, ApiContextualIntentResult):
                raise TypeError("OpenAI returned an invalid order intent")
            if getattr(self, "_debug_response_json", False):
                logger.info(
                    "Order structured response debug",
                    extra={
                        "llm_attempt": attempt + 1,
                        "llm_response_json": _safe_debug_response_json(result),
                    },
                )
            try:
                return api_result_to_outcome(result)
            except RepairableDomainValidationError as exc:
                if attempt == 1:
                    raise
                messages = [
                    messages[0],
                    SystemMessage(
                        content=(
                            "Исправь только структурированный результат. "
                            f"VALIDATION_RULE={exc.validation_rule}. "
                            "Не повторяй предыдущий payload и не добавляй факты."
                        )
                    ),
                    messages[-1],
                ]
        raise RuntimeError("Unreachable order intent retry state")


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
    "что с предыдущим заказом": "previous_order",
    "я уже оформлял заказ": "previous_order",
    "что я заказывал раньше": "previous_order",
}


def deterministic_intent(message: str) -> OrderIntentProposal | None:
    normalized = re.sub(r"\s+", " ", message.casefold().strip().rstrip(".!?"))
    if normalized == "заказ":
        return OrderIntentProposal(
            action="unknown",
            confidence="low",
            clarification="Уточните: показать текущую корзину или перейти к оформлению?",
        )
    if normalized in {
        "какие аллергены у этого блюда",
        "что входит в это блюдо",
        "расскажи об этом блюде",
    }:
        return OrderIntentProposal(action="referenced_dish_info")
    action = _EXACT_ACTIONS.get(normalized)
    if action is None:
        return None
    return OrderIntentProposal(action=action)


async def safe_extract(
    extractor: OrderIntentExtractor | None,
    message: str,
    context: OrderContext | None = None,
) -> OrderIntentProposal | OrderActionPlan:
    deterministic = deterministic_intent(message)
    if deterministic is not None:
        return deterministic
    if contains_obvious_sensitive_data(message):
        return OrderIntentProposal(
            action="unknown",
            confidence="low",
            clarification=(
                "Не отправляйте контактные или секретные данные в свободном запросе. "
                "Уточните действие без них."
            ),
        )
    if extractor is None:
        return OrderIntentProposal(action="unknown", confidence="low")
    if context is None:
        from app.orders.context import ContextBuilder, ContextSource

        context = ContextBuilder().build(ContextSource())
    logger.info(
        "Order intent diagnostic",
        extra={"llm_called": True},
    )
    try:
        outcome = await extractor.extract(message, context)
    except UnsafeStructuredOutcomeError as exc:
        logger.error(
            "Order intent extraction failed",
            extra={
                "exception_type": type(exc).__name__,
                "llm_called": True,
                "llm_error": type(exc).__name__,
            },
        )
        return OrderIntentProposal(
            action="unknown",
            confidence="low",
            clarification="Уточните действие без контактных или секретных данных.",
        )
    except Exception as exc:
        logger.error(
            "Order intent extraction failed",
            extra={
                "exception_type": type(exc).__name__,
                "llm_called": True,
                "llm_error": type(exc).__name__,
                **_safe_llm_error_fields(exc),
                **_safe_validation_error_fields(exc),
            },
        )
        return OrderIntentProposal(action="unknown", confidence="low")

    outcome_kind = getattr(outcome, "kind", "invalid")
    outcome_action = outcome.action if isinstance(outcome, ActionOutcome) else None
    logger.info(
        "Order intent diagnostic",
        extra={
            "llm_called": True,
            "llm_result": (
                f"{outcome_kind}:{outcome_action}"
                if outcome_action is not None
                else (
                    f"actions:{len(outcome.operations)}"
                    if isinstance(outcome, ActionsOutcome)
                    else outcome_kind
                )
            ),
        },
    )

    if isinstance(outcome, ClarifyOutcome):
        question = outcome.question
        if contains_obvious_sensitive_data(question) or _INTERNAL_TERM_PATTERN.search(
            question
        ):
            question = (
                "Уточните, пожалуйста, что вы хотите сделать с меню или корзиной."
            )
        return OrderIntentProposal(
            action="unknown",
            confidence="low",
            clarification=question,
        )
    if isinstance(outcome, UnsupportedOutcome):
        return OrderIntentProposal(action="unknown", confidence="low")
    if isinstance(outcome, ValidationOutcome):
        if outcome.validation_rule == "quantity_out_of_range":
            return OrderIntentProposal(
                action="unknown",
                confidence="low",
                clarification=QUANTITY_OUT_OF_RANGE_MESSAGE,
            )
        return OrderIntentProposal(action="unknown", confidence="low")
    if isinstance(outcome, ActionsOutcome):
        text_values = [
            value
            for operation in outcome.operations
            for item in operation.items
            for value in (item.name, item.lookup_query)
            if isinstance(value, str)
        ]
        if any(contains_obvious_sensitive_data(value) for value in text_values):
            return OrderIntentProposal(
                action="unknown",
                confidence="low",
                clarification=(
                    "Уточните действие без контактных или секретных данных."
                ),
            )
        if any(
            operation.action not in context.allowed_actions
            for operation in outcome.operations
        ):
            return OrderIntentProposal(
                action="unknown",
                confidence="low",
                clarification=(
                    "Одно из действий сейчас недоступно. "
                    "Корзина не изменена. Уточните запрос."
                ),
            )
        return OrderActionPlan(operations=outcome.operations)
    if not isinstance(outcome, ActionOutcome):
        return OrderIntentProposal(action="unknown", confidence="low")
    action_text_values = [
        outcome.query,
        outcome.category,
        *outcome.dietary_tags,
        *(item.name for item in outcome.items),
        *(item.lookup_query for item in outcome.items),
    ]
    if any(
        contains_obvious_sensitive_data(value)
        for value in action_text_values
        if isinstance(value, str)
    ):
        return OrderIntentProposal(
            action="unknown",
            confidence="low",
            clarification="Уточните действие без контактных или секретных данных.",
        )
    if outcome.action not in context.allowed_actions:
        return OrderIntentProposal(
            action="unknown",
            confidence="low",
            clarification="Это действие сейчас недоступно. Уточните, что вы хотите сделать.",
        )
    return OrderIntentProposal(
        action=outcome.action,
        items=outcome.items,
        query=outcome.query,
        category=outcome.category,
        dietary_tags=outcome.dietary_tags,
    )
