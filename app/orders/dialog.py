import hashlib
import json
import logging
import re
import unicodedata
from secrets import token_urlsafe
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import datetime
from difflib import SequenceMatcher
from typing import Any, Protocol
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from app.menu.contracts import CartItemInput, CartQuote, DishDetail, MenuSearchResult
from app.orders.cart import (
    CartOperationError,
    add_items,
    decrement,
    remove_item,
    set_quantity,
)
from app.orders.context import ContextBuilder, ContextSource
from app.orders.contracts import (
    CartStateItem,
    ConfirmedOrder,
    ContactData,
    OrderActionPlan,
    OrderIntentProposal,
    PendingClarification,
    PendingActionPlan,
    PendingAction,
    PendingBatch,
    PendingCandidate,
    PendingDestructiveAction,
    QuoteSnapshot,
)
from app.orders.intent import (
    OrderIntentExtractor,
    contains_obvious_sensitive_data,
    deterministic_intent,
    safe_extract,
)
from app.orders.settings import OrderSettingsError, OrderSettingsRepository

logger = logging.getLogger(__name__)

MADRID = ZoneInfo("Europe/Madrid")
SAFE_ORDER_ERROR = "Не удалось обработать заказ. Попробуйте еще раз."
CONFIRMED_MESSAGE = "Данные заказа подтверждены. Заказ еще не размещен."
CHECKOUT_NOTICE = (
    "Переходим к оформлению заказа.\n\n"
    "Сейчас я попрошу вас указать имя, телефон, адрес и время доставки.\n\n"
    "Не переживайте, если допустите ошибку: перед подтверждением заказа "
    "вы сможете проверить и изменить все введенные данные."
)
MAX_CONFIRMED_SNAPSHOTS = 5
FUZZY_MATCH_THRESHOLD = 0.86
FUZZY_SEPARATION_MARGIN = 0.08

_CALLBACK_ACTIONS = {
    "confirm": "confirm",
    "edit": "edit",
    "cancel": "cancel",
    "edit_cart": "edit_cart",
    "edit_name": "edit_name",
    "edit_phone": "edit_phone",
    "edit_address": "edit_address",
    "edit_time": "edit_time",
    "return_summary": "return_summary",
    "cancel_yes": "cancel_yes",
    "cancel_back": "cancel_back",
    "time_asap": "time_asap",
    "time_manual": "time_manual",
    "cancel_checkout": "cancel_checkout",
    "confirm_new_order": "confirm_new_order",
    "confirm_clear": "confirm_clear",
    "keep_current_order": "keep_current_order",
    "clarify_view_cart": "clarify_view_cart",
    "clarify_checkout": "clarify_checkout",
}

_CLARIFICATION_CALLBACK_ACTIONS = {"clarify_view_cart", "clarify_checkout"}
_CONTACT_STAGES = {
    "collecting_name",
    "collecting_phone",
    "collecting_address",
    "collecting_time",
    "collecting_edit_name",
    "collecting_edit_phone",
    "collecting_edit_address",
    "collecting_edit_time",
}
_CHECKOUT_CALLBACK_ACTIONS = {"time_asap", "time_manual", "cancel_checkout"}

_CALLBACK_STAGES = {
    "confirm": {"awaiting_confirmation"},
    "edit": {"awaiting_confirmation"},
    "cancel": {"awaiting_confirmation"},
    "edit_cart": {"awaiting_edit_choice"},
    "edit_name": {"awaiting_edit_choice"},
    "edit_phone": {"awaiting_edit_choice"},
    "edit_address": {"awaiting_edit_choice"},
    "edit_time": {"awaiting_edit_choice"},
    "return_summary": {"awaiting_edit_choice", "editing_cart"},
    "cancel_yes": {"awaiting_cancel_confirmation"},
    "cancel_back": {"awaiting_cancel_confirmation"},
    "time_asap": {"collecting_time", "collecting_edit_time"},
    "time_manual": {"collecting_time", "collecting_edit_time"},
    "cancel_checkout": _CONTACT_STAGES,
    "confirm_new_order": {"awaiting_new_order_confirmation"},
    "confirm_clear": {"awaiting_clear_confirmation"},
    "keep_current_order": {
        "awaiting_new_order_confirmation",
        "awaiting_clear_confirmation",
    },
}

_DESTRUCTIVE_CALLBACK_ACTIONS = {
    "confirm_new_order",
    "confirm_clear",
    "keep_current_order",
    "cancel_yes",
    "cancel_back",
}


class MenuGateway(Protocol):
    async def search_menu(
        self,
        query: str | None = None,
        category: str | None = None,
        dietary_tags: list[str] | None = None,
    ) -> MenuSearchResult: ...

    async def get_dish(self, dish_id: UUID) -> DishDetail: ...

    async def calculate_cart(self, items: list[CartItemInput]) -> CartQuote: ...


MenuGatewayFactory = Callable[[], AbstractAsyncContextManager[MenuGateway]]


def _money(value: int) -> str:
    return f"{value // 100},{value % 100:02d} EUR"


def _display_name(value: str) -> str:
    return value if len(value) <= 80 else value[:77] + "..."


def _cart_from_state(state: dict[str, Any]) -> list[CartStateItem]:
    try:
        return [CartStateItem.model_validate(item) for item in state.get("cart", [])]
    except ValidationError:
        return []


def _contacts_from_state(state: dict[str, Any]) -> ContactData:
    try:
        return ContactData.model_validate(state.get("contacts") or {})
    except ValidationError:
        return ContactData()


def _dump_cart(items: list[CartStateItem]) -> list[dict[str, object]]:
    return [item.model_dump(mode="json") for item in items]


def _invalidated(state: dict[str, Any]) -> dict[str, Any]:
    return {
        "quote_snapshot": None,
        "confirmed_order": None,
        "draft_revision": int(state.get("draft_revision", 0)) + 1,
    }


def _looks_like_faq(message: str) -> bool:
    normalized = message.casefold()
    explicit_question = any(
        phrase in normalized
        for phrase in (
            "до скольких",
            "когда вы",
            "во сколько",
            "вы работаете",
            "есть ли доставка",
        )
    )
    return explicit_question or (
        "?" in message and any(character.isalpha() for character in normalized)
    )


def _format_menu(result: MenuSearchResult) -> str:
    if not result.items:
        return "По вашему запросу доступных блюд не найдено."
    lines = ["Доступные блюда:"]
    visible = result.items[:10]
    lines.extend(
        f"{index}. {_display_name(item.name)} — {_money(item.price_minor)}"
        for index, item in enumerate(visible, 1)
    )
    if len(result.items) > len(visible):
        lines.append("Уточните запрос, чтобы увидеть остальные блюда.")
    return "\n".join(lines)


def _format_dish(dish: DishDetail) -> str:
    ingredients = ", ".join(dish.ingredients) or "не указаны"
    allergens = ", ".join(dish.allergens) or "не указаны"
    availability = "доступно" if dish.available else "сейчас недоступно"
    return (
        f"{_display_name(dish.name)} — {_money(dish.price_minor)}\n"
        f"Состав: {ingredients}\nАллергены: {allergens}\nСтатус: {availability}."
    )


def _quote_fingerprint(quote: CartQuote, delivery_fee: int, revision: int) -> str:
    payload = {
        "items": [item.model_dump(mode="json") for item in quote.items],
        "subtotal_minor": quote.subtotal_minor,
        "delivery_fee_minor": delivery_fee,
        "currency": quote.currency,
        "draft_revision": revision,
    }
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _format_summary(snapshot: QuoteSnapshot, contacts: ContactData) -> str:
    lines = ["Проверьте заказ:"]
    for item in snapshot.items:
        lines.append(
            f"• {_display_name(str(item['name']))} × {item['quantity']} — {_money(int(item['line_total_minor']))}"
        )
    requested = contacts.requested_at or "как можно скорее"
    lines.extend(
        [
            f"Блюда: {_money(snapshot.subtotal_minor)}",
            f"Доставка: {_money(snapshot.delivery_fee_minor)}",
            f"Итого: {_money(snapshot.total_minor)}",
            f"Получатель: {contacts.customer_name}",
            f"Телефон: {contacts.phone}",
            f"Адрес: {contacts.address}",
            f"Время: {requested}",
            "Для подтверждения отправьте ровно: «Подтверждаю».",
        ]
    )
    return "\n".join(lines)


def _validate_name(value: str) -> str | None:
    normalized = unicodedata.normalize("NFC", value)
    if any(
        unicodedata.category(char).startswith("C")
        or unicodedata.category(char) in {"Zl", "Zp"}
        for char in normalized
    ):
        return None
    normalized = "".join(
        " " if unicodedata.category(char) == "Zs" else char
        for char in normalized
    )
    normalized = normalized.translate(str.maketrans("‐‑‒–", "----"))
    cleaned = " ".join(normalized.strip().split())
    if not 2 <= len(cleaned) <= 100:
        return None
    separators = {" ", "-", "'", "’", "·"}
    letters = 0
    previous_was_separator = True
    previous_was_letter_or_mark = False
    for char in cleaned:
        category = unicodedata.category(char)
        if category.startswith("L"):
            letters += 1
            previous_was_separator = False
            previous_was_letter_or_mark = True
        elif category.startswith("M") and previous_was_letter_or_mark:
            previous_was_separator = False
            previous_was_letter_or_mark = True
        elif char in separators and not previous_was_separator:
            previous_was_separator = True
            previous_was_letter_or_mark = False
        else:
            return None
    return cleaned if letters >= 2 and not previous_was_separator else None


def _validate_phone(value: str) -> str | None:
    if any(
        unicodedata.category(char).startswith("C")
        or unicodedata.category(char) in {"Zl", "Zp"}
        for char in value
    ):
        return None
    cleaned = re.sub(r"[\s()\-]", "", value.strip())
    if not re.fullmatch(r"\+?[0-9]{7,15}", cleaned):
        return None
    return cleaned


def _validate_address(value: str) -> str | None:
    normalized = unicodedata.normalize("NFC", value)
    if any(
        unicodedata.category(char).startswith("C")
        or unicodedata.category(char) in {"Zl", "Zp"}
        for char in normalized
    ):
        return None
    normalized = "".join(
        " " if unicodedata.category(char) == "Zs" else char
        for char in normalized
    )
    cleaned = " ".join(normalized.strip().split())
    if not 5 <= len(cleaned) <= 300:
        return None
    punctuation = {" ", ".", ",", "'", "’", "-", "/", "№", "#", "(", ")"}
    has_letter = False
    for char in cleaned:
        category = unicodedata.category(char)
        if category.startswith("L"):
            has_letter = True
        elif category.startswith(("M", "N")) or char in punctuation:
            continue
        else:
            return None
    return cleaned if has_letter else None


def _parse_requested_at(value: str, now: datetime) -> tuple[bool, str | None]:
    normalized = " ".join(value.casefold().strip().split())
    if normalized in {"как можно скорее", "как можно быстрей", "asap", "скорее"}:
        return True, None
    if any(
        word in normalized for word in ("утром", "днем", "днём", "вечером", "ночью")
    ):
        return False, None
    if not re.fullmatch(r"\d{2}\.\d{2}\.\d{4} \d{2}:\d{2}", normalized):
        return False, None
    try:
        parsed = datetime.strptime(normalized, "%d.%m.%Y %H:%M").replace(tzinfo=MADRID)
    except ValueError:
        return False, None
    if parsed <= now.astimezone(MADRID):
        return False, None
    return True, parsed.isoformat()


def _empty_draft_changes(state: dict[str, Any]) -> dict[str, Any]:
    return {
        "cart": [],
        "contacts": {},
        "quote_snapshot": None,
        "pending_action": None,
        "pending_destructive_action": None,
        "pending_clarification": None,
        "checkout_session_id": None,
        "pending_batch": None,
        "candidates": [],
        "draft_revision": int(state.get("draft_revision", 0)) + 1,
        "order_stage": "idle",
        "last_referenced_dish_id": None,
        "last_referenced_dish_name": None,
    }


def _normalized_action_text(value: str) -> str:
    return re.sub(r"\s+", " ", value.casefold().strip().rstrip(".!?"))


def _confirmation_text_intent(value: str) -> str | None:
    return {
        "подтверждаю": "confirm",
        "подтвердить": "confirm",
        "нет": "edit",
        "изменить заказ": "edit",
        "отмена": "cancel",
        "отменить": "cancel",
    }.get(_normalized_action_text(value))


def _has_active_draft(state: dict[str, Any]) -> bool:
    return bool(_cart_from_state(state)) and not (
        state.get("order_stage") == "confirmed" and state.get("confirmed_order")
    )


def _context_source(state: dict[str, Any]) -> ContextSource:
    cart = state.get("cart")
    quantities = (
        tuple(item.get("quantity") for item in cart if isinstance(item, dict))
        if isinstance(cart, list)
        else ()
    )
    archive = state.get("confirmed_order_archive")
    archive_count = len(archive) if isinstance(archive, list) else 0
    has_current = isinstance(state.get("confirmed_order"), dict)
    pending_actions: tuple[object, ...] = ()
    try:
        pending = PendingClarification.model_validate(state.get("pending_clarification"))
        pending_actions = tuple(choice.action for choice in pending.choices)
    except ValidationError:
        pass
    return ContextSource(
        order_stage=state.get("order_stage", "idle"),
        cart_quantities=quantities,
        recent_interactions=state.get("recent_interactions", []),
        last_action=state.get("last_action"),
        last_referenced_dish_name=state.get("last_referenced_dish_name"),
        has_confirmed_snapshot=has_current,
        confirmed_snapshot_count=archive_count + (1 if has_current else 0),
        pending_allowed_actions=pending_actions,
    )


def _pending_clarification(state: dict[str, Any]) -> PendingClarification | None:
    try:
        return PendingClarification.model_validate(state.get("pending_clarification"))
    except ValidationError:
        return None


def _new_pending_clarification(
    reason: str, stage: str
) -> PendingClarification:
    choices = (
        [
            {"action": "view_cart", "label": "Показать корзину"},
            {"action": "checkout", "label": "Перейти к оформлению"},
        ]
        if reason == "order_intent"
        else [{"action": "dish_info", "label": "Указать блюдо"}]
    )
    return PendingClarification(
        reason=reason,
        choices=choices,
        return_stage=stage,
        clarification_id=token_urlsafe(9),
    )


def _text_similarity(query: str, candidate: str) -> float:
    query_parts = re.findall(r"[\w]+", query.casefold(), flags=re.UNICODE)
    candidate_parts = re.findall(r"[\w]+", candidate.casefold(), flags=re.UNICODE)
    if not query_parts or not candidate_parts:
        return 0.0
    comparisons = [
        SequenceMatcher(None, " ".join(query_parts), " ".join(candidate_parts)).ratio()
    ]
    comparisons.extend(
        SequenceMatcher(None, query_part, candidate_part).ratio()
        for query_part in query_parts
        for candidate_part in candidate_parts
    )
    return max(comparisons)


class OrderDialog:
    def __init__(
        self,
        menu_factory: MenuGatewayFactory,
        settings_repository: OrderSettingsRepository,
        intent_extractor: OrderIntentExtractor | None,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._menu_factory = menu_factory
        self._settings = settings_repository
        self._intent_extractor = intent_extractor
        self._context_builder = ContextBuilder()
        self._now = now or (lambda: datetime.now(MADRID))

    async def classify(
        self, state: dict[str, Any]
    ) -> tuple[str, dict[str, Any] | None]:
        def classified(
            route: str,
            proposal: dict[str, Any] | None,
            *,
            llm_called: bool,
        ) -> tuple[str, dict[str, Any] | None]:
            logger.info(
                "Order routing diagnostic",
                extra={"llm_called": llm_called, "final_route": route},
            )
            return route, proposal

        message = state.get("user_message", "")
        stage = state.get("order_stage", "idle")
        callback_action = state.get("callback_action")
        if callback_action is not None:
            action = _CALLBACK_ACTIONS.get(callback_action, "unknown")
            return classified(
                "order",
                OrderIntentProposal(action=action).model_dump(mode="json"),
                llm_called=False,
            )
        if stage in _CONTACT_STAGES:
            return classified("order", None, llm_called=False)
        if stage == "awaiting_confirmation":
            confirmation_action = _confirmation_text_intent(message)
            if confirmation_action is not None:
                return classified(
                    "order",
                    OrderIntentProposal(action=confirmation_action).model_dump(
                        mode="json"
                    ),
                    llm_called=False,
                )
        if stage == "awaiting_new_order_confirmation":
            return classified(
                "order",
                OrderIntentProposal(
                    action="unknown",
                    confidence="low",
                    clarification=(
                        "Выберите: начать новый заказ или оставить текущий. "
                        "Корзина пока не изменена."
                    ),
                ).model_dump(mode="json"),
                llm_called=False,
            )
        if stage == "awaiting_clear_confirmation":
            return classified(
                "order",
                OrderIntentProposal(
                    action="unknown",
                    confidence="low",
                    clarification=(
                        "Выберите: очистить корзину или оставить текущий заказ. "
                        "Корзина пока не изменена."
                    ),
                ).model_dump(mode="json"),
                llm_called=False,
            )
        if stage == "awaiting_cancel_confirmation":
            return classified(
                "order",
                OrderIntentProposal(
                    action="unknown",
                    confidence="low",
                    clarification=(
                        "Выберите: отменить оформление или вернуться. "
                        "Текущий черновик пока не изменен."
                    ),
                ).model_dump(mode="json"),
                llm_called=False,
            )
        pending = _pending_clarification(state)
        if pending is not None:
            normalized_pending_answer = _normalized_action_text(message)
            if pending.reason == "order_intent":
                resolved_action = None
                if normalized_pending_answer in {
                    "корзина",
                    "показать корзину",
                    "покажи корзину",
                    "текущая корзина",
                }:
                    resolved_action = "view_cart"
                elif normalized_pending_answer in {
                    "оформление",
                    "оформить",
                    "перейти к оформлению",
                    "оформить заказ",
                }:
                    resolved_action = "checkout"
                if resolved_action is not None:
                    return classified(
                        "order",
                        OrderIntentProposal(action=resolved_action).model_dump(
                            mode="json"
                        ),
                        llm_called=False,
                    )
            guarded = deterministic_intent(message)
            if guarded is not None and not (
                guarded.action == "unknown" and guarded.clarification
            ):
                return classified(
                    "order",
                    guarded.model_dump(mode="json"),
                    llm_called=False,
                )
            if _looks_like_faq(message) and not contains_obvious_sensitive_data(message):
                return classified("faq", None, llm_called=False)
            if pending.reason == "dish_referent":
                if contains_obvious_sensitive_data(message) or not message.strip():
                    return classified(
                        "order",
                        OrderIntentProposal(
                            action="unknown",
                            confidence="low",
                            clarification="Укажите название блюда без контактных данных.",
                        ).model_dump(mode="json"),
                        llm_called=False,
                    )
                return classified(
                    "order",
                    OrderIntentProposal(
                        action="dish_info",
                        items=[{"name": message[:200]}],
                    ).model_dump(mode="json"),
                    llm_called=False,
                )
            context = self._context_builder.build(_context_source(state))
            llm_called = (
                self._intent_extractor is not None
                and not contains_obvious_sensitive_data(message)
            )
            continuation = await safe_extract(self._intent_extractor, message, context)
            if isinstance(continuation, OrderActionPlan):
                continuation = OrderIntentProposal(
                    action="unknown",
                    confidence="low",
                    clarification=(
                        "Выберите: показать текущую корзину или перейти к оформлению."
                    ),
                )
            if continuation.action == "unknown" and not continuation.clarification:
                continuation = OrderIntentProposal(
                    action="unknown",
                    confidence="low",
                    clarification=(
                        "Выберите: показать текущую корзину или перейти к оформлению."
                    ),
                )
            return classified(
                "order",
                continuation.model_dump(mode="json"),
                llm_called=llm_called,
            )
        guarded = deterministic_intent(message)
        if guarded is not None:
            return classified(
                "order",
                guarded.model_dump(mode="json"),
                llm_called=False,
            )
        if stage == "awaiting_disambiguation" and message.strip().isdigit():
            return classified("order", None, llm_called=False)
        context = self._context_builder.build(_context_source(state))
        llm_called = (
            self._intent_extractor is not None
            and not contains_obvious_sensitive_data(message)
        )
        proposal = await safe_extract(self._intent_extractor, message, context)
        if isinstance(proposal, OrderActionPlan):
            return classified(
                "order",
                proposal.model_dump(mode="json"),
                llm_called=llm_called,
            )
        if proposal.action == "faq":
            return classified(
                "faq",
                proposal.model_dump(mode="json"),
                llm_called=llm_called,
            )
        if proposal.action == "unknown":
            if proposal.clarification:
                return classified(
                    "order",
                    proposal.model_dump(mode="json"),
                    llm_called=llm_called,
                )
            return classified(
                "unknown",
                proposal.model_dump(mode="json"),
                llm_called=llm_called,
            )
        return classified(
            "order",
            proposal.model_dump(mode="json"),
            llm_called=llm_called,
        )

    async def handle(self, state: dict[str, Any]) -> dict[str, Any]:
        try:
            changes = await self._handle(state)
            next_stage = changes.get("order_stage", state.get("order_stage", "idle"))
            if next_stage in _CONTACT_STAGES and not changes.get("checkout_session_id"):
                current_session = state.get("checkout_session_id")
                changes["checkout_session_id"] = (
                    current_session
                    if isinstance(current_session, str)
                    and re.fullmatch(r"[A-Za-z0-9_-]{12}", current_session)
                    else token_urlsafe(9)
                )
            return changes
        except OrderSettingsError as exc:
            return {
                "response": exc.public_message,
                "error_code": "order_settings_unavailable",
            }
        except CartOperationError as exc:
            return {"response": str(exc), "error_code": "cart_validation"}
        except Exception as exc:
            logger.error(
                "Order dialog execution failed",
                extra={"exception_type": type(exc).__name__},
            )
            return {"response": SAFE_ORDER_ERROR, "error_code": "order_error"}

    async def _handle(self, state: dict[str, Any]) -> dict[str, Any]:
        stage = state.get("order_stage", "idle")
        callback_error = self._validate_callback(state)
        if callback_error is not None:
            return callback_error
        proposal_data = state.get("action_proposal")
        plan = (
            OrderActionPlan.model_validate(proposal_data)
            if isinstance(proposal_data, dict) and "operations" in proposal_data
            else None
        )
        proposal = (
            OrderIntentProposal.model_validate(proposal_data)
            if proposal_data and plan is None
            else OrderIntentProposal(action="unknown", confidence="low")
        )
        if (
            proposal.action == "unknown"
            and proposal.clarification
            == "Уточните: показать текущую корзину или перейти к оформлению?"
            and _cart_from_state(state)
        ):
            pending = _new_pending_clarification("order_intent", stage)
            return {
                "pending_clarification": pending.model_dump(mode="json"),
                "response": proposal.clarification,
                "error_code": "order_clarification",
            }
        contact_guards = {
            "start",
            "new_order",
            "cancel",
            "clear",
            "view_cart",
            "browse_menu",
            "search_menu",
            "referenced_dish_info",
            "previous_order",
            "clarify_view_cart",
            "clarify_checkout",
            "cancel_checkout",
        }
        if stage.startswith("collecting_") and proposal.action not in contact_guards:
            if plan is not None:
                return {
                    "response": ("Сначала завершите ввод данных. Корзина не изменена."),
                    "error_code": "batch_action_not_allowed",
                }
            return await self._handle_contact(state)
        if (
            stage == "awaiting_disambiguation"
            and state.get("user_message", "").strip().isdigit()
        ):
            return await self._handle_candidate_choice(state)

        action = proposal.action
        if action == "start":
            return self._start_response(state)
        if action in _CLARIFICATION_CALLBACK_ACTIONS:
            return await self._resolve_clarification_callback(state, action)
        if action == "cancel_checkout":
            return self._cancel_checkout(state)
        if action == "referenced_dish_info":
            return await self._show_referenced_dish(state)
        if action == "previous_order":
            result = self._show_previous_order(state)
            result.update(last_referenced_dish_id=None, last_referenced_dish_name=None)
            return result
        if action == "new_order":
            if _has_active_draft(state):
                return self._request_destructive_confirmation(
                    "new_order",
                    stage,
                    "awaiting_new_order_confirmation",
                    "Текущий черновик и корзина будут очищены. Начать новый заказ?",
                )
            return self._start_new_order(state)
        if action == "confirm_new_order":
            return self._confirm_new_order(state)
        if action == "confirm_clear":
            return self._confirm_clear(state)
        if action == "keep_current_order":
            return self._keep_current_order(state)
        if (
            action == "confirm"
            and state.get("callback_action") != "confirm"
            and state.get("user_message", "").casefold().strip()
            not in {"подтверждаю", "подтвердить"}
        ):
            return {
                "response": "Для явного подтверждения отправьте ровно: «Подтверждаю».",
                "error_code": "explicit_confirmation_required",
            }
        if state.get("order_stage") == "confirmed" and state.get("confirmed_order"):
            if action == "confirm":
                return {"response": CONFIRMED_MESSAGE, "error_code": None}
            if action == "cancel":
                return {
                    "response": "Подтвержденный заказ сохранен. Активного черновика для отмены нет.",
                    "error_code": None,
                }
            if action not in {"browse_menu", "search_menu", "dish_info", "view_cart"}:
                return {
                    "response": "Подтвержденный заказ сохранен. Чтобы продолжить, выберите «Новый заказ».",
                    "error_code": "confirmed_order_immutable",
                }
        if plan is not None:
            return await self._execute_action_plan(state, plan)
        if action == "unknown":
            changes = {
                "response": proposal.clarification
                or "Уточните, пожалуйста, что вы хотите сделать с меню или корзиной.",
                "error_code": "order_clarification",
            }
            if _pending_clarification(state) is not None:
                changes["pending_clarification"] = state["pending_clarification"]
            return changes
        if action == "browse_menu" or action == "search_menu":
            async with self._menu_factory() as menu:
                result = await menu.search_menu(
                    proposal.query, proposal.category, proposal.dietary_tags
                )
            return {
                "response": _format_menu(result),
                "error_code": None,
                "last_referenced_dish_id": None,
                "last_referenced_dish_name": None,
            }
        if action == "clear":
            if _has_active_draft(state):
                return self._request_destructive_confirmation(
                    "clear",
                    stage,
                    "awaiting_clear_confirmation",
                    "Текущая корзина будет очищена. Продолжить?",
                )
            return self._clear_cart(state, stage)
        if action == "cancel":
            if _has_active_draft(state):
                return self._request_destructive_confirmation(
                    "cancel",
                    stage,
                    "awaiting_cancel_confirmation",
                    "Отменить оформление и очистить активный черновик?",
                )
            changes = _empty_draft_changes(state)
            changes.update(
                confirmed_order=state.get("confirmed_order"),
                response="Оформление отменено, черновик очищен.",
                error_code=None,
            )
            return changes
        if action == "edit":
            return {
                "order_stage": "awaiting_edit_choice",
                "response": "Что вы хотите изменить?",
                "error_code": None,
            }
        if action == "edit_cart":
            return {
                "order_stage": "editing_cart",
                "response": "Измените корзину обычной командой, затем выберите «Вернуться к итогу».",
                "error_code": None,
            }
        if action in {"edit_name", "edit_phone", "edit_address", "edit_time"}:
            prompts = {
                "edit_name": ("collecting_edit_name", "Укажите новое имя получателя."),
                "edit_phone": (
                    "collecting_edit_phone",
                    "Укажите новый номер телефона.",
                ),
                "edit_address": (
                    "collecting_edit_address",
                    "Укажите новый адрес доставки.",
                ),
                "edit_time": (
                    "collecting_edit_time",
                    "Выберите время доставки: «Как можно скорее» или «Выбрать дату и время».",
                ),
            }
            next_stage, prompt = prompts[action]
            return {
                "order_stage": next_stage,
                "checkout_session_id": token_urlsafe(9),
                "response": prompt,
                "error_code": None,
            }
        if action == "return_summary":
            if not _cart_from_state(state):
                return {
                    "order_stage": "editing_cart",
                    "response": "Корзина пуста. Добавьте хотя бы одно блюдо.",
                    "error_code": "empty_cart",
                }
            return await self._prepare_quote(state, _contacts_from_state(state))
        if action == "cancel_yes":
            pending_data = state.get("pending_destructive_action")
            if pending_data is not None:
                try:
                    pending = PendingDestructiveAction.model_validate(pending_data)
                except ValidationError:
                    return {
                        "response": "Подтверждение отмены устарело. Текущий черновик сохранен.",
                        "error_code": "pending_destructive_action_missing",
                    }
                if pending.action != "cancel":
                    return {
                        "response": "Это действие больше недоступно. Текущий черновик сохранен.",
                        "error_code": "pending_destructive_action_invalid",
                    }
            changes = _empty_draft_changes(state)
            changes.update(
                confirmed_order=state.get("confirmed_order"),
                response="Оформление отменено, черновик очищен.",
                error_code=None,
            )
            return changes
        if action == "cancel_back":
            pending_data = state.get("pending_destructive_action")
            if pending_data is not None:
                try:
                    pending = PendingDestructiveAction.model_validate(pending_data)
                except ValidationError:
                    return {
                        "response": "Это действие устарело. Текущий черновик сохранен.",
                        "error_code": "pending_destructive_action_missing",
                    }
                if pending.return_stage != "awaiting_confirmation":
                    return {
                        "order_stage": pending.return_stage,
                        "pending_destructive_action": None,
                        "response": "Текущий заказ сохранен. Можно продолжить оформление.",
                        "error_code": None,
                    }
            return {
                "order_stage": "awaiting_confirmation",
                "pending_destructive_action": None,
                "response": _format_summary(
                    QuoteSnapshot.model_validate(state.get("quote_snapshot")),
                    _contacts_from_state(state),
                ),
                "error_code": None,
            }
        if action == "checkout":
            if not _cart_from_state(state):
                return {
                    "response": "Корзина пуста. Сначала добавьте блюда.",
                    "error_code": "empty_cart",
                }
            contacts = _contacts_from_state(state)
            next_stage, prompt = self._next_contact_prompt(contacts)
            if next_stage is None:
                return await self._prepare_quote(state, contacts)
            return {
                "order_stage": next_stage,
                "checkout_session_id": token_urlsafe(9),
                "response": f"{CHECKOUT_NOTICE}\n\n{prompt}",
                "error_code": None,
            }
        if action == "view_cart":
            cart = _cart_from_state(state)
            if not cart:
                return {"response": "Корзина пуста.", "error_code": None}
            async with self._menu_factory() as menu:
                quote = await menu.calculate_cart(
                    [
                        CartItemInput(dish_id=item.dish_id, quantity=item.quantity)
                        for item in cart
                    ]
                )
            lines = (
                ["Корзина:"]
                + [
                    f"• {_display_name(line.name)} × {line.quantity} — {_money(line.line_total_minor)}"
                    for line in quote.items
                ]
                + [f"Сумма блюд: {_money(quote.subtotal_minor)}"]
            )
            return {"response": "\n".join(lines), "error_code": None}
        if action == "confirm":
            return await self._confirm(state)
        if action in {"dish_info", "add", "set_quantity", "decrement", "remove"}:
            return await self._execute_item_action(state, proposal)
        return {"response": "Уточните запрос.", "error_code": "order_clarification"}

    @staticmethod
    def _validate_callback(state: dict[str, Any]) -> dict[str, Any] | None:
        action = state.get("callback_action")
        if action is None:
            return None
        if action in _CLARIFICATION_CALLBACK_ACTIONS:
            try:
                pending = PendingClarification.model_validate(
                    state.get("pending_clarification")
                )
            except ValidationError:
                return {
                    "response": "Эта кнопка устарела. Используйте действия из последнего сообщения.",
                    "error_code": "stale_callback",
                }
            expected_action = {
                "clarify_view_cart": "view_cart",
                "clarify_checkout": "checkout",
            }[action]
            allowed = {choice.action for choice in pending.choices}
            if (
                state.get("callback_revision") != int(state.get("draft_revision", 0))
                or state.get("callback_confirmation_id") != pending.clarification_id
                or expected_action not in allowed
            ):
                return {
                    "response": "Эта кнопка устарела. Используйте действия из последнего сообщения.",
                    "error_code": "stale_callback",
                }
            return None
        if action not in _CALLBACK_STAGES:
            return {
                "response": "Эта кнопка не поддерживается.",
                "error_code": "invalid_callback",
            }
        expected_revision = state.get("callback_revision")
        current_revision = int(state.get("draft_revision", 0))
        if (
            expected_revision != current_revision
            or state.get("order_stage", "idle") not in _CALLBACK_STAGES[action]
        ):
            return {
                "response": "Эта кнопка устарела. Используйте действия из последнего сообщения.",
                "error_code": "stale_callback",
            }
        if action in _DESTRUCTIVE_CALLBACK_ACTIONS:
            try:
                pending = PendingDestructiveAction.model_validate(
                    state.get("pending_destructive_action")
                )
            except ValidationError:
                return {
                    "response": "Эта кнопка устарела. Используйте действия из последнего сообщения.",
                    "error_code": "stale_callback",
                }
            if state.get("callback_confirmation_id") != pending.confirmation_id:
                return {
                    "response": "Эта кнопка устарела. Используйте действия из последнего сообщения.",
                    "error_code": "stale_callback",
                }
        if action in _CHECKOUT_CALLBACK_ACTIONS:
            checkout_session_id = state.get("checkout_session_id")
            if (
                not isinstance(checkout_session_id, str)
                or state.get("callback_confirmation_id") != checkout_session_id
            ):
                return {
                    "response": "Эта кнопка устарела. Используйте действия из последнего сообщения.",
                    "error_code": "stale_callback",
                }
        return None

    @staticmethod
    def _cancel_checkout(state: dict[str, Any]) -> dict[str, Any]:
        contacts = _contacts_from_state(state)
        has_contact_data = any(
            value is not None
            for value in (
                contacts.customer_name,
                contacts.phone,
                contacts.address,
                contacts.requested_at,
            )
        )
        changes = (
            _invalidated(state)
            if has_contact_data or state.get("quote_snapshot") is not None
            else {}
        )
        changes.update(
            contacts={},
            quote_snapshot=None,
            pending_clarification=None,
            checkout_session_id=None,
            order_stage="idle",
            response="Оформление отменено. Корзина сохранена.",
            error_code=None,
        )
        return changes

    async def _resolve_clarification_callback(
        self, state: dict[str, Any], action: str
    ) -> dict[str, Any]:
        resolved = "view_cart" if action == "clarify_view_cart" else "checkout"
        continued = dict(state)
        continued["callback_action"] = None
        continued["action_proposal"] = OrderIntentProposal(action=resolved).model_dump(
            mode="json"
        )
        continued["pending_clarification"] = None
        result = await self._handle(continued)
        result["pending_clarification"] = None
        return result

    async def _show_referenced_dish(self, state: dict[str, Any]) -> dict[str, Any]:
        raw_id = state.get("last_referenced_dish_id")
        try:
            dish_id = UUID(raw_id) if isinstance(raw_id, str) else None
        except ValueError:
            dish_id = None
        if dish_id is None:
            pending = _new_pending_clarification(
                "dish_referent", state.get("order_stage", "idle")
            )
            return {
                "pending_clarification": pending.model_dump(mode="json"),
                "response": "Уточните, о каком блюде идет речь.",
                "error_code": "order_clarification",
            }
        async with self._menu_factory() as menu:
            dish = await menu.get_dish(dish_id)
        return {
            "last_referenced_dish_id": str(dish.dish_id),
            "last_referenced_dish_name": _display_name(dish.name),
            "response": _format_dish(dish),
            "error_code": None,
        }

    @staticmethod
    def _show_previous_order(state: dict[str, Any]) -> dict[str, Any]:
        snapshot_data = state.get("confirmed_order")
        if not isinstance(snapshot_data, dict):
            archive = state.get("confirmed_order_archive")
            snapshot_data = archive[-1] if isinstance(archive, list) and archive else None
        try:
            snapshot = ConfirmedOrder.model_validate(snapshot_data)
        except ValidationError:
            return {
                "response": (
                    "В этом диалоге нет сохраненного подтвержденного снимка заказа. "
                    "Я не могу подтвердить размещение или статус доставки."
                ),
                "error_code": None,
            }
        lines = ["Есть сохраненный подтвержденный снимок заказа:"]
        lines.extend(
            f"• {_display_name(str(item['name']))} × {item['quantity']}"
            for item in snapshot.items
        )
        lines.extend(
            [
                f"Итого: {_money(snapshot.total_minor)}",
                "Это checkpoint-снимок: заказ еще не размещен, статус приготовления или доставки недоступен.",
            ]
        )
        return {"response": "\n".join(lines), "error_code": None}

    @staticmethod
    def _start_response(state: dict[str, Any]) -> dict[str, Any]:
        if state.get("order_stage") == "confirmed" and state.get("confirmed_order"):
            response = (
                "Подтвержденный заказ сохранен и еще не размещен. "
                "Чтобы оформить следующий, отправьте «Новый заказ»."
            )
        elif state.get("order_stage", "idle") != "idle" or _cart_from_state(state):
            response = (
                "У вас есть незавершенный заказ. Можно продолжить оформление "
                "или отправить «Отмена», чтобы очистить черновик."
            )
        else:
            response = "Здравствуйте! Можно посмотреть меню или начать новый заказ."
        return {"response": response, "error_code": None}

    @staticmethod
    def _start_new_order(state: dict[str, Any]) -> dict[str, Any]:
        archive = [
            ConfirmedOrder.model_validate(item).model_dump(mode="json")
            for item in state.get("confirmed_order_archive", [])
        ]
        current_data = state.get("confirmed_order")
        if (
            len(archive) + (1 if current_data is not None else 0)
            >= MAX_CONFIRMED_SNAPSHOTS
        ):
            return {
                "response": (
                    "Сохранено максимальное число подтвержденных заказов (5). "
                    "Новый заказ пока нельзя начать без удаления сохраненных снимков."
                ),
                "error_code": "confirmed_archive_full",
            }
        if current_data is not None:
            current = ConfirmedOrder.model_validate(current_data).model_dump(
                mode="json"
            )
            if (
                not archive
                or archive[-1]["confirmation_key"] != current["confirmation_key"]
            ):
                archive.append(current)
        changes = _empty_draft_changes(state)
        changes.update(
            confirmed_order=None,
            confirmed_order_archive=archive,
            recent_interactions=[],
            last_action=None,
            last_referenced_dish_name=None,
            response="Новый заказ начат. Корзина пуста — выберите блюда.",
            error_code=None,
        )
        return changes

    @staticmethod
    def _confirm_new_order(state: dict[str, Any]) -> dict[str, Any]:
        pending_data = state.get("pending_destructive_action")
        try:
            pending = PendingDestructiveAction.model_validate(pending_data)
        except ValidationError:
            return {
                "response": "Подтверждение нового заказа устарело. Текущий черновик сохранен.",
                "error_code": "pending_destructive_action_missing",
            }
        if pending.action != "new_order":
            return {
                "response": "Это действие больше недоступно. Текущий черновик сохранен.",
                "error_code": "pending_destructive_action_invalid",
            }
        return OrderDialog._start_new_order(state)

    @staticmethod
    def _confirm_clear(state: dict[str, Any]) -> dict[str, Any]:
        pending_data = state.get("pending_destructive_action")
        try:
            pending = PendingDestructiveAction.model_validate(pending_data)
        except ValidationError:
            return {
                "response": "Подтверждение очистки устарело. Текущая корзина сохранена.",
                "error_code": "pending_destructive_action_missing",
            }
        if pending.action != "clear":
            return {
                "response": "Это действие больше недоступно. Текущая корзина сохранена.",
                "error_code": "pending_destructive_action_invalid",
            }
        return OrderDialog._clear_cart(state, pending.return_stage)

    @staticmethod
    def _keep_current_order(state: dict[str, Any]) -> dict[str, Any]:
        pending_data = state.get("pending_destructive_action")
        try:
            pending = PendingDestructiveAction.model_validate(pending_data)
        except ValidationError:
            return {
                "response": "Это действие устарело. Текущий черновик сохранен.",
                "error_code": "pending_destructive_action_missing",
            }
        return {
            "order_stage": pending.return_stage,
            "pending_destructive_action": None,
            "response": "Текущий заказ сохранен. Можно продолжить работу с корзиной.",
            "error_code": None,
        }

    @staticmethod
    def _request_destructive_confirmation(
        action: str,
        return_stage: str,
        confirmation_stage: str,
        response: str,
    ) -> dict[str, Any]:
        pending = PendingDestructiveAction.model_validate(
            {
                "action": action,
                "return_stage": return_stage,
                "confirmation_id": token_urlsafe(9),
            }
        )
        return {
            "order_stage": confirmation_stage,
            "pending_destructive_action": pending.model_dump(mode="json"),
            "response": response,
            "error_code": None,
        }

    @staticmethod
    def _clear_cart(state: dict[str, Any], return_stage: str) -> dict[str, Any]:
        changes = _invalidated(state)
        editing_cart = return_stage == "editing_cart"
        changes.update(
            cart=[],
            contacts=state.get("contacts", {}) if editing_cart else {},
            order_stage="editing_cart" if editing_cart else "idle",
            pending_action=None,
            pending_destructive_action=None,
            pending_batch=None,
            candidates=[],
            response="Корзина очищена."
            + (" Добавьте блюда или вернитесь к итогу." if editing_cart else ""),
            error_code=None,
        )
        return changes

    async def _resolve_menu_item(
        self, menu: MenuGateway, queries: list[str]
    ) -> tuple[DishDetail | None, list[PendingCandidate]]:
        by_id = {}
        for query in dict.fromkeys(item for item in queries if item):
            result = await menu.search_menu(query)
            for item in result.items:
                by_id[item.dish_id] = item
        exact = [
            item
            for item in by_id.values()
            if any(item.name.casefold() == query.casefold() for query in queries)
        ]
        candidates = exact or list(by_id.values())
        if len(candidates) == 1:
            return await menu.get_dish(candidates[0].dish_id), []
        return None, [
            PendingCandidate(dish_id=item.dish_id, name=item.name)
            for item in candidates
        ]

    async def _resolve_cart_item(
        self, menu: MenuGateway, cart: list[CartStateItem], queries: list[str]
    ) -> tuple[DishDetail | None, list[PendingCandidate]]:
        cart_ids = {item.dish_id for item in cart}
        by_id: dict[UUID, DishDetail] = {}
        for query in dict.fromkeys(item for item in queries if item):
            result = await menu.search_menu(query)
            for item in result.items:
                if item.dish_id in cart_ids:
                    by_id[item.dish_id] = await menu.get_dish(item.dish_id)
        # A dish may become unavailable after it was added. It must still be
        # removable from the cart even though search_menu returns available rows only.
        cart_dishes: list[DishDetail] = []
        for item in cart:
            dish = await menu.get_dish(item.dish_id)
            cart_dishes.append(dish)
            if any(query.casefold() in dish.name.casefold() for query in queries):
                by_id[dish.dish_id] = dish
        exact = [
            dish
            for dish in by_id.values()
            if any(dish.name.casefold() == query.casefold() for query in queries)
        ]
        matches = exact or list(by_id.values())
        if len(matches) == 1:
            return matches[0], []
        if not matches:
            scored = [
                (
                    max(_text_similarity(query, dish.name) for query in queries),
                    dish,
                )
                for dish in cart_dishes
            ]
            scored.sort(key=lambda entry: entry[0], reverse=True)
            if scored and scored[0][0] >= FUZZY_MATCH_THRESHOLD:
                runner_up = scored[1][0] if len(scored) > 1 else 0.0
                if scored[0][0] - runner_up >= FUZZY_SEPARATION_MARGIN:
                    closest = scored[0][1]
                    return None, [
                        PendingCandidate(dish_id=closest.dish_id, name=closest.name)
                    ]
        return None, [
            PendingCandidate(dish_id=dish.dish_id, name=dish.name) for dish in matches
        ]

    async def _execute_item_action(
        self,
        state: dict[str, Any],
        proposal: OrderIntentProposal,
        resolved_dish_ids: list[UUID | None] | None = None,
    ) -> dict[str, Any]:
        cart = _cart_from_state(state)
        resolved: list[tuple[DishDetail, int]] = []
        resolved_ids = list(resolved_dish_ids or [None] * len(proposal.items))
        async with self._menu_factory() as menu:
            for index, proposed in enumerate(proposal.items):
                if resolved_ids[index] is not None:
                    dish = await menu.get_dish(resolved_ids[index])
                    candidates = []
                else:
                    queries = [proposed.name]
                    if (
                        proposed.lookup_query
                        and proposed.lookup_query.casefold() != proposed.name.casefold()
                    ):
                        queries.append(proposed.lookup_query)
                    if proposal.action in {"set_quantity", "decrement", "remove"}:
                        dish, candidates = await self._resolve_cart_item(
                            menu, cart, queries
                        )
                    else:
                        dish, candidates = await self._resolve_menu_item(menu, queries)
                if dish is None:
                    if not candidates:
                        return {
                            "response": f"Не удалось однозначно найти «{proposed.name}».",
                            "error_code": "dish_not_found",
                        }
                    pending = PendingAction(
                        action=proposal.action, quantity=proposed.quantity
                    )
                    batch = PendingBatch(
                        proposal=proposal,
                        resolved_dish_ids=resolved_ids,
                        unresolved_index=index,
                        return_stage=(
                            "editing_cart"
                            if state.get("order_stage") == "editing_cart"
                            else "idle"
                        ),
                    )
                    choices = "\n".join(
                        f"{i}. {_display_name(item.name)}"
                        for i, item in enumerate(candidates, 1)
                    )
                    return {
                        "order_stage": "awaiting_disambiguation",
                        "pending_action": pending.model_dump(mode="json"),
                        "pending_batch": batch.model_dump(mode="json"),
                        "candidates": [
                            item.model_dump(mode="json") for item in candidates
                        ],
                        "response": f"Уточните блюдо номером:\n{choices}",
                        "error_code": "dish_ambiguous",
                    }
                if not dish.available and proposal.action in {"add", "dish_info"}:
                    return {
                        "response": f"{dish.name} сейчас недоступно.",
                        "error_code": "dish_unavailable",
                    }
                resolved_ids[index] = dish.dish_id
                resolved.append((dish, proposed.quantity or 1))

        if proposal.action == "dish_info":
            if len(resolved) != 1:
                return {
                    "response": "Уточните одно блюдо для подробной информации.",
                    "error_code": "order_clarification",
                }
            return {
                "last_referenced_dish_id": str(resolved[0][0].dish_id),
                "last_referenced_dish_name": _display_name(resolved[0][0].name),
                "response": _format_dish(resolved[0][0]),
                "error_code": None,
            }
        if proposal.action == "add":
            new_cart = add_items(
                cart,
                [
                    CartStateItem(dish_id=dish.dish_id, quantity=quantity)
                    for dish, quantity in resolved
                ],
            )
            response = (
                "Добавлено: "
                + ", ".join(f"{dish.name} × {quantity}" for dish, quantity in resolved)
                + "."
            )
        else:
            if len(resolved) != 1:
                return {
                    "response": "Изменяйте одно блюдо за раз.",
                    "error_code": "order_clarification",
                }
            dish, quantity = resolved[0]
            if proposal.action == "set_quantity":
                if proposal.items[0].quantity is None:
                    return {
                        "response": "Укажите новое количество блюда.",
                        "error_code": "order_clarification",
                    }
                new_cart = set_quantity(cart, dish.dish_id, quantity)
                response = f"Количество «{dish.name}» изменено: {quantity}."
            elif proposal.action == "decrement":
                new_cart = decrement(cart, dish.dish_id)
                response = f"Количество «{dish.name}» уменьшено на один."
            else:
                new_cart = remove_item(cart, dish.dish_id)
                response = f"«{dish.name}» удалено из корзины."
        changes = _invalidated(state)
        editing_cart = state.get("order_stage") == "editing_cart"
        if editing_cart:
            response += " Когда закончите, выберите «Вернуться к итогу»."
        changes.update(
            cart=_dump_cart(new_cart),
            order_stage="editing_cart" if editing_cart else "idle",
            last_referenced_dish_id=str(resolved[-1][0].dish_id),
            last_referenced_dish_name=_display_name(resolved[-1][0].name),
            pending_action=None,
            pending_batch=None,
            candidates=[],
            response=response,
            error_code=None,
        )
        return changes

    async def _execute_action_plan(
        self,
        state: dict[str, Any],
        plan: OrderActionPlan,
        resolved_dish_ids: list[list[UUID | None]] | None = None,
    ) -> dict[str, Any]:
        cart = _cart_from_state(state)
        resolved_ids = (
            [list(operation_ids) for operation_ids in resolved_dish_ids]
            if resolved_dish_ids is not None
            else [[None] * len(operation.items) for operation in plan.operations]
        )
        resolved: list[list[tuple[DishDetail, int]]] = []
        async with self._menu_factory() as menu:
            for operation_index, operation in enumerate(plan.operations):
                operation_resolved: list[tuple[DishDetail, int]] = []
                for item_index, proposed in enumerate(operation.items):
                    selected_id = resolved_ids[operation_index][item_index]
                    if selected_id is not None:
                        dish = await menu.get_dish(selected_id)
                        candidates: list[PendingCandidate] = []
                    else:
                        queries = [proposed.name]
                        if (
                            proposed.lookup_query
                            and proposed.lookup_query.casefold()
                            != proposed.name.casefold()
                        ):
                            queries.append(proposed.lookup_query)
                        if operation.action in {
                            "set_quantity",
                            "decrement",
                            "remove",
                        }:
                            dish, candidates = await self._resolve_cart_item(
                                menu, cart, queries
                            )
                        else:
                            dish, candidates = await self._resolve_menu_item(
                                menu, queries
                            )
                    if dish is None:
                        if not candidates:
                            return {
                                "response": (
                                    f"Не удалось однозначно найти «{proposed.name}». "
                                    "Корзина не изменена. Уточните название блюда."
                                ),
                                "error_code": "batch_dish_not_found",
                            }
                        pending = PendingAction(
                            action=operation.action,
                            quantity=proposed.quantity,
                        )
                        pending_plan = PendingActionPlan(
                            plan=plan,
                            resolved_dish_ids=resolved_ids,
                            unresolved_operation_index=operation_index,
                            unresolved_item_index=item_index,
                            return_stage=(
                                "editing_cart"
                                if state.get("order_stage") == "editing_cart"
                                else "idle"
                            ),
                        )
                        choices = "\n".join(
                            f"{index}. {_display_name(candidate.name)}"
                            for index, candidate in enumerate(candidates, 1)
                        )
                        return {
                            "order_stage": "awaiting_disambiguation",
                            "pending_action": pending.model_dump(mode="json"),
                            "pending_batch": pending_plan.model_dump(mode="json"),
                            "candidates": [
                                candidate.model_dump(mode="json")
                                for candidate in candidates
                            ],
                            "response": (
                                "Корзина не изменена. Уточните блюдо номером:\n"
                                f"{choices}"
                            ),
                            "error_code": "batch_dish_ambiguous",
                        }
                    if not dish.available and operation.action == "add":
                        return {
                            "response": (
                                f"{dish.name} сейчас недоступно. Корзина не изменена."
                            ),
                            "error_code": "batch_dish_unavailable",
                        }
                    resolved_ids[operation_index][item_index] = dish.dish_id
                    operation_resolved.append((dish, proposed.quantity or 1))
                resolved.append(operation_resolved)

        first_operation_by_dish: dict[UUID, int] = {}
        for operation_index, operation_resolved in enumerate(resolved):
            for dish, _ in operation_resolved:
                previous = first_operation_by_dish.setdefault(
                    dish.dish_id, operation_index
                )
                if previous != operation_index:
                    return {
                        "response": (
                            f"Для «{dish.name}» указано несколько изменений. "
                            "Корзина не изменена. Уточните итоговое действие."
                        ),
                        "error_code": "batch_conflict",
                    }

        candidate_cart = list(cart)
        descriptions: list[str] = []
        try:
            for operation, operation_resolved in zip(
                plan.operations, resolved, strict=True
            ):
                if operation.action == "add":
                    candidate_cart = add_items(
                        candidate_cart,
                        [
                            CartStateItem(
                                dish_id=dish.dish_id,
                                quantity=quantity,
                            )
                            for dish, quantity in operation_resolved
                        ],
                    )
                    descriptions.extend(
                        f"добавлено «{dish.name}» × {quantity}"
                        for dish, quantity in operation_resolved
                    )
                    continue
                dish, quantity = operation_resolved[0]
                if operation.action == "set_quantity":
                    candidate_cart = set_quantity(
                        candidate_cart, dish.dish_id, quantity
                    )
                    descriptions.append(
                        f"«{dish.name}» — итоговое количество {quantity}"
                    )
                elif operation.action == "decrement":
                    candidate_cart = decrement(candidate_cart, dish.dish_id)
                    descriptions.append(f"«{dish.name}» уменьшено на один")
                else:
                    candidate_cart = remove_item(candidate_cart, dish.dish_id)
                    descriptions.append(f"«{dish.name}» удалено")
        except CartOperationError as exc:
            return {
                "response": (
                    f"Корзина не изменена. {exc} Уточните изменения и повторите запрос."
                ),
                "error_code": "batch_cart_validation",
            }

        changes = _invalidated(state)
        editing_cart = state.get("order_stage") == "editing_cart"
        response = "Изменения применены: " + "; ".join(descriptions) + "."
        if editing_cart:
            response += " Когда закончите, выберите «Вернуться к итогу»."
        last_dish = resolved[-1][-1][0]
        changes.update(
            cart=_dump_cart(candidate_cart),
            order_stage="editing_cart" if editing_cart else "idle",
            last_referenced_dish_id=str(last_dish.dish_id),
            last_referenced_dish_name=_display_name(last_dish.name),
            pending_action=None,
            pending_batch=None,
            candidates=[],
            response=response,
            error_code=None,
        )
        return changes

    async def _handle_candidate_choice(self, state: dict[str, Any]) -> dict[str, Any]:
        candidates = [
            PendingCandidate.model_validate(item)
            for item in state.get("candidates", [])
        ]
        choice = int(state["user_message"].strip())
        if choice < 1 or choice > len(candidates):
            return {
                "response": f"Выберите номер от 1 до {len(candidates)}.",
                "error_code": "dish_ambiguous",
            }
        selected = candidates[choice - 1]
        batch_data = state.get("pending_batch")
        if batch_data is None:
            return {
                "order_stage": "idle",
                "pending_action": None,
                "candidates": [],
                "response": "Запрос устарел. Повторите изменение корзины целиком.",
                "error_code": "pending_batch_missing",
            }
        if isinstance(batch_data, dict) and "plan" in batch_data:
            pending_plan = PendingActionPlan.model_validate(batch_data)
            resolved_plan_ids = [
                list(operation_ids) for operation_ids in pending_plan.resolved_dish_ids
            ]
            resolved_plan_ids[pending_plan.unresolved_operation_index][
                pending_plan.unresolved_item_index
            ] = selected.dish_id
            clean_state = dict(state)
            clean_state["order_stage"] = pending_plan.return_stage
            clean_state["action_proposal"] = pending_plan.plan.model_dump(mode="json")
            return await self._execute_action_plan(
                clean_state,
                pending_plan.plan,
                resolved_plan_ids,
            )
        batch = PendingBatch.model_validate(batch_data)
        resolved_ids = list(batch.resolved_dish_ids)
        resolved_ids[batch.unresolved_index] = selected.dish_id
        proposal = batch.proposal
        clean_state = dict(state)
        clean_state["order_stage"] = batch.return_stage
        clean_state["action_proposal"] = proposal.model_dump(mode="json")
        return await self._execute_item_action(clean_state, proposal, resolved_ids)

    @staticmethod
    def _next_contact_prompt(contacts: ContactData) -> tuple[str | None, str]:
        if contacts.customer_name is None:
            return "collecting_name", "Как вас зовут?"
        if contacts.phone is None:
            return "collecting_phone", "Укажите номер телефона."
        if contacts.address is None:
            return "collecting_address", "Укажите адрес доставки."
        return (
            "collecting_time",
            "Выберите время доставки: «Как можно скорее» или «Выбрать дату и время».",
        )

    async def _handle_contact(self, state: dict[str, Any]) -> dict[str, Any]:
        stage = state.get("order_stage")
        value = state.get("user_message", "")
        contacts = _contacts_from_state(state)
        editing = isinstance(stage, str) and stage.startswith("collecting_edit_")
        if stage in {"collecting_name", "collecting_edit_name"}:
            validated = _validate_name(value)
            if validated is None:
                return {
                    "response": "Укажите имя буквами (от 2 до 100 символов).",
                    "error_code": "invalid_contact",
                }
            contacts.customer_name = validated
            if editing:
                return await self._finish_contact_edit(
                    state, contacts, "customer_name", validated
                )
            next_stage, prompt = "collecting_phone", "Укажите номер телефона."
        elif stage in {"collecting_phone", "collecting_edit_phone"}:
            validated = _validate_phone(value)
            if validated is None:
                return {
                    "response": "Укажите корректный номер телефона (7–15 цифр).",
                    "error_code": "invalid_contact",
                }
            contacts.phone = validated
            if editing:
                return await self._finish_contact_edit(
                    state, contacts, "phone", validated
                )
            next_stage, prompt = "collecting_address", "Укажите адрес доставки."
        elif stage in {"collecting_address", "collecting_edit_address"}:
            validated = _validate_address(value)
            if validated is None:
                return {
                    "response": "Укажите полный адрес доставки.",
                    "error_code": "invalid_contact",
                }
            contacts.address = validated
            if editing:
                return await self._finish_contact_edit(
                    state, contacts, "address", validated
                )
            next_stage, prompt = (
                "collecting_time",
                "Выберите время доставки: «Как можно скорее» или «Выбрать дату и время».",
            )
        else:
            if value.casefold().strip() == "выбрать дату и время":
                return {
                    "response": "Введите дату и время в формате ДД.ММ.ГГГГ ЧЧ:ММ.",
                    "error_code": None,
                }
            valid, requested_at = _parse_requested_at(value, self._now())
            if not valid:
                return {
                    "response": "Введите будущее время в формате ДД.ММ.ГГГГ ЧЧ:ММ или выберите «Как можно скорее».",
                    "error_code": "invalid_requested_at",
                }
            contacts.requested_at = requested_at
            if editing:
                return await self._finish_contact_edit(
                    state, contacts, "requested_at", requested_at
                )
            changes = _invalidated(state)
            quote_state = dict(state)
            quote_state.update(changes)
            quote_state["contacts"] = contacts.model_dump(mode="json")
            quote_changes = await self._prepare_quote(quote_state, contacts)
            changes.update(
                quote_changes,
                contacts=contacts.model_dump(mode="json"),
            )
            return changes
        changes = _invalidated(state)
        changes.update(
            contacts=contacts.model_dump(mode="json"),
            order_stage=next_stage,
            response=prompt,
            error_code=None,
        )
        return changes

    async def _finish_contact_edit(
        self,
        state: dict[str, Any],
        contacts: ContactData,
        field: str,
        value: str | None,
    ) -> dict[str, Any]:
        previous_contacts = _contacts_from_state(state)
        changed = getattr(previous_contacts, field) != value
        changes = _invalidated(state) if changed else {}
        quote_state = dict(state)
        quote_state.update(changes)
        quote_state["contacts"] = contacts.model_dump(mode="json")
        quote_changes = await self._prepare_quote(quote_state, contacts)
        changes.update(quote_changes, contacts=contacts.model_dump(mode="json"))
        return changes

    async def _prepare_quote(
        self, state: dict[str, Any], contacts: ContactData
    ) -> dict[str, Any]:
        cart = _cart_from_state(state)
        async with self._menu_factory() as menu:
            quote = await menu.calculate_cart(
                [
                    CartItemInput(dish_id=item.dish_id, quantity=item.quantity)
                    for item in cart
                ]
            )
        delivery_fee = await self._settings.get_delivery_fee_minor()
        revision = int(state.get("draft_revision", 0))
        snapshot = QuoteSnapshot(
            items=[line.model_dump(mode="json") for line in quote.items],
            subtotal_minor=quote.subtotal_minor,
            delivery_fee_minor=delivery_fee,
            total_minor=quote.subtotal_minor + delivery_fee,
            draft_revision=revision,
            fingerprint=_quote_fingerprint(quote, delivery_fee, revision),
        )
        return {
            "quote_snapshot": snapshot.model_dump(mode="json"),
            "checkout_session_id": None,
            "order_stage": "awaiting_confirmation",
            "response": _format_summary(snapshot, contacts),
            "error_code": None,
        }

    async def _confirm(self, state: dict[str, Any]) -> dict[str, Any]:
        if state.get("order_stage") == "confirmed" and state.get("confirmed_order"):
            return {"response": CONFIRMED_MESSAGE, "error_code": None}
        if state.get("order_stage") != "awaiting_confirmation":
            return {
                "response": "Сначала завершите оформление и проверьте итоговую сводку.",
                "error_code": "confirmation_not_ready",
            }
        contacts = _contacts_from_state(state)
        previous = QuoteSnapshot.model_validate(state.get("quote_snapshot"))
        refreshed = await self._prepare_quote(state, contacts)
        current = QuoteSnapshot.model_validate(refreshed["quote_snapshot"])
        if (
            current.fingerprint != previous.fingerprint
            or current.draft_revision != previous.draft_revision
        ):
            revised_state = dict(state)
            revised_state["draft_revision"] = int(state.get("draft_revision", 0)) + 1
            revised = await self._prepare_quote(revised_state, contacts)
            revised["draft_revision"] = revised_state["draft_revision"]
            revised["response"] += "\nДанные изменились — подтвердите еще раз."
            return revised
        confirmed = ConfirmedOrder(
            **current.model_dump(),
            customer_name=contacts.customer_name or "",
            phone=contacts.phone or "",
            address=contacts.address or "",
            requested_at=contacts.requested_at,
            confirmation_key=uuid4().hex,
            confirmed_at=datetime.now(ZoneInfo("UTC")).isoformat(),
        )
        return {
            "confirmed_order": confirmed.model_dump(mode="json"),
            "order_stage": "confirmed",
            "response": CONFIRMED_MESSAGE,
            "error_code": None,
        }
