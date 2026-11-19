import json
from contextlib import asynccontextmanager
from copy import deepcopy
from uuid import uuid4

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from openai.lib._parsing._completions import type_to_response_format_param
from pydantic import ValidationError

from app.agent.graph import build_agent_graph
from app.agent.service import AgentService
from app.orders.context import ContextBuilder, ContextSource
from app.orders.contracts import (
    ActionOutcome,
    ActionsOutcome,
    ApiContextualIntentResult,
    ClarifyOutcome,
    ContextualIntentResult,
    OrderActionPlan,
    OrderIntentProposal,
    UnsupportedOutcome,
    ValidationOutcome,
)
from app.orders.dialog import OrderDialog, _context_source
from app.orders.intent import (
    LlmOrderIntentExtractor,
    QUANTITY_OUT_OF_RANGE_MESSAGE,
    RepairableDomainValidationError,
    _safe_debug_response_json,
    _safe_validation_error_fields,
    api_result_to_outcome,
    build_intent_messages,
    safe_extract,
)


class FakeSettings:
    async def get_delivery_fee_minor(self) -> int:
        return 350


def test_pending_clarification_overrides_expected_input_and_allowlist() -> None:
    context = ContextBuilder().build(
        ContextSource(
            order_stage="collecting_phone",
            pending_allowed_actions=("view_cart", "checkout", "confirm"),
        )
    )
    assert context.expected_input == "clarification_choice"
    assert context.allowed_actions == ["view_cart", "checkout"]


@pytest.mark.parametrize(
    "stage",
    [
        "collecting_name",
        "collecting_phone",
        "collecting_address",
        "collecting_time",
        "collecting_edit_name",
        "collecting_edit_phone",
        "collecting_edit_address",
        "collecting_edit_time",
    ],
)
def test_contact_context_has_no_llm_actions(stage: str) -> None:
    context = ContextBuilder().build(ContextSource(order_stage=stage))
    assert context.allowed_actions == []


class SequenceStructured:
    def __init__(self, *results: object) -> None:
        self.results = list(results)
        self.calls: list[list[object]] = []

    async def ainvoke(self, messages):
        self.calls.append(list(messages))
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def extractor_with_results(
    *results: object,
    debug_response_json: bool = False,
) -> tuple[LlmOrderIntentExtractor, SequenceStructured]:
    structured = SequenceStructured(*results)
    extractor = object.__new__(LlmOrderIntentExtractor)
    extractor._structured = structured
    extractor._debug_response_json = debug_response_json
    return extractor, structured


def api_action(
    action: str,
    *,
    quantity: int | None = None,
    item_name: str = "Синтетическое блюдо",
    query: str | None = None,
    category: str | None = None,
    dietary_tags: list[str] | None = None,
) -> ApiContextualIntentResult:
    return ApiContextualIntentResult(
        kind="action",
        action=action,
        items=[
            {
                "name": item_name,
                "lookup_query": "синтетическое блюдо",
                "quantity": quantity,
            }
        ],
        query=query,
        category=category,
        dietary_tags=dietary_tags or [],
        operations=[],
        clarification_type=None,
        question=None,
        reason=None,
    )


def api_actions(*operations: dict[str, object]) -> ApiContextualIntentResult:
    return ApiContextualIntentResult(
        kind="actions",
        action=None,
        items=[],
        query=None,
        category=None,
        dietary_tags=[],
        operations=list(operations),
        clarification_type=None,
        question=None,
        reason=None,
    )


@asynccontextmanager
async def unused_menu_factory():
    yield object()


def test_context_builder_is_bounded_allowlisted_and_pure() -> None:
    with pytest.raises(ValidationError):
        ContextSource.model_validate({"contacts": {"phone": "+34600000001"}})

    raw_history = [
        {
            "source": "text",
            "semantic": "add",
            "outcome": "handled",
            "dish_name": f"Блюдо {index}",
        }
        for index in range(12)
    ]
    raw_history.append(
        {
            "source": "text",
            "semantic": "add",
            "outcome": "handled",
            "dish_name": "+34 600 000 001",
        }
    )
    state = {
        "order_stage": "collecting_name",
        "cart": [{"dish_id": str(uuid4()), "quantity": 20} for _ in range(25)],
        "contacts": {
            "customer_name": "Иван Секретный",
            "phone": "+34600000001",
            "address": "Calle Privada 10",
        },
        "callback_confirmation_id": "session00001",
        "recent_interactions": raw_history,
        "last_action": "add",
        "last_referenced_dish_name": "+34 600 000 001",
        "confirmed_order": {"phone": "+34600000002"},
        "confirmed_order_archive": [{} for _ in range(9)],
    }
    original = deepcopy(state)

    context = ContextBuilder().build(_context_source(state))

    assert state == original
    assert context.order_stage == "collecting_name"
    assert context.expected_input == "customer_name"
    assert context.has_active_cart is True
    assert context.cart_line_count == 20
    assert context.cart_unit_count == 100
    assert context.last_action == "add"
    assert context.last_referenced_dish_name is None
    assert len(context.recent_interactions) == 9
    assert context.recent_interactions[0].dish_name == "Блюдо 3"
    assert context.confirmed_snapshot_count == 5
    encoded = context.model_dump_json()
    for forbidden in (
        "Иван Секретный",
        "+34600000001",
        "Calle Privada 10",
        "session00001",
        "dish_id",
    ):
        assert forbidden not in encoded


def test_context_builder_legacy_defaults_are_safe() -> None:
    context = ContextBuilder().build(
        ContextSource(
            order_stage="legacy-invalid-stage",
            recent_interactions=[{"raw": "private text"}],
            last_action="not-an-action",
        )
    )
    assert context.order_stage == "idle"
    assert context.expected_input == "free_text"
    assert context.recent_interactions == []
    assert context.last_action is None
    assert context.cart_line_count == 0


def test_contextual_outcomes_are_strict_and_action_specific() -> None:
    with pytest.raises(ValidationError):
        ActionOutcome(action="confirm")
    with pytest.raises(ValidationError):
        ActionOutcome(action="add")
    with pytest.raises(ValidationError):
        ActionOutcome(action="search_menu")
    with pytest.raises(ValidationError):
        ActionOutcome.model_validate({"action": "faq", "unexpected": True})
    with pytest.raises(ValidationError):
        ActionOutcome(action="faq", query="unused payload")

    clarified = ClarifyOutcome(
        clarification_type="ambiguous_intent",
        question="  Показать корзину   или перейти к оформлению? ",
    )
    assert clarified.question == "Показать корзину или перейти к оформлению?"
    wrapped = ContextualIntentResult.model_validate(
        {"outcome": clarified.model_dump(mode="json")}
    )
    assert isinstance(wrapped.outcome, ClarifyOutcome)
    assert UnsupportedOutcome(reason="not_supported").kind == "unsupported"
    schema = ContextualIntentResult.model_json_schema()
    outcome_schema = schema["properties"]["outcome"]
    assert outcome_schema["discriminator"]["propertyName"] == "kind"
    assert set(outcome_schema["discriminator"]["mapping"]) == {
        "action",
        "actions",
        "clarify",
        "unsupported",
        "validation_error",
    }


def test_bound_openai_response_schema_uses_supported_strict_subset() -> None:
    extractor = LlmOrderIntentExtractor(
        "sk-synthetic-schema-test-key",
        "gpt-4.1-mini",
        1.0,
    )
    binding = extractor._structured.first
    response_format = binding.kwargs["response_format"]
    binding_metadata = binding.kwargs["ls_structured_output_format"]["kwargs"]
    assert response_format is ApiContextualIntentResult
    assert binding_metadata == {"method": "json_schema", "strict": True}

    api_response_format = type_to_response_format_param(response_format)
    assert api_response_format["type"] == "json_schema"
    assert api_response_format["json_schema"]["strict"] is True
    schema = api_response_format["json_schema"]["schema"]

    forbidden_keywords = {
        "allOf",
        "default",
        "dependentRequired",
        "dependentSchemas",
        "discriminator",
        "else",
        "if",
        "maxItems",
        "maxLength",
        "maximum",
        "minItems",
        "minLength",
        "minimum",
        "multipleOf",
        "not",
        "oneOf",
        "pattern",
        "patternProperties",
        "then",
        "uniqueItems",
    }

    def assert_supported(node: object) -> None:
        if isinstance(node, dict):
            assert forbidden_keywords.isdisjoint(node)
            if node.get("type") == "object":
                properties = node.get("properties", {})
                assert node.get("additionalProperties") is False
                assert set(node.get("required", [])) == set(properties)
            for value in node.values():
                assert_supported(value)
        elif isinstance(node, list):
            for value in node:
                assert_supported(value)

    assert_supported(schema)


def test_api_response_is_validated_by_existing_domain_contracts() -> None:
    action = api_result_to_outcome(
        ApiContextualIntentResult(
            kind="action",
            action="view_cart",
            items=[],
            query=None,
            category=None,
            dietary_tags=[],
            operations=[],
            clarification_type=None,
            question=None,
            reason=None,
        )
    )
    assert action == ActionOutcome(action="view_cart")

    add = api_result_to_outcome(
        ApiContextualIntentResult(
            kind="action",
            action="add",
            items=[
                {
                    "name": "Брускетту",
                    "lookup_query": "брускетта",
                    "quantity": 2,
                }
            ],
            query=None,
            category=None,
            dietary_tags=[],
            operations=[],
            clarification_type=None,
            question=None,
            reason=None,
        )
    )
    assert add == ActionOutcome(
        action="add",
        items=[
            {
                "name": "Брускетту",
                "lookup_query": "брускетта",
                "quantity": 2,
            }
        ],
    )

    clarification = api_result_to_outcome(
        ApiContextualIntentResult(
            kind="clarify",
            action=None,
            items=[],
            query=None,
            category=None,
            dietary_tags=[],
            operations=[],
            clarification_type="retrospective_intent",
            question="Показать текущую корзину?",
            reason=None,
        )
    )
    assert isinstance(clarification, ClarifyOutcome)

    quantity_error = api_result_to_outcome(
        ApiContextualIntentResult(
            kind="action",
            action="add",
            items=[
                {
                    "name": "Брускетта",
                    "lookup_query": "брускетта",
                    "quantity": 21,
                }
            ],
            query=None,
            category=None,
            dietary_tags=[],
            operations=[],
            clarification_type=None,
            question=None,
            reason=None,
        )
    )
    assert quantity_error == ValidationOutcome(validation_rule="quantity_out_of_range")

    with pytest.raises(RepairableDomainValidationError) as repairable:
        api_result_to_outcome(
            ApiContextualIntentResult(
                kind="action",
                action="set_quantity",
                items=[
                    {
                        "name": "Синтетическое блюдо",
                        "lookup_query": "синтетическое блюдо",
                        "quantity": None,
                    }
                ],
                query=None,
                category=None,
                dietary_tags=[],
                operations=[],
                clarification_type=None,
                question=None,
                reason=None,
            )
        )
    assert repairable.value.validation_rule == "missing_quantity_for_set_quantity"

    with pytest.raises(ValidationError) as missing_quantity:
        ActionOutcome(
            action="set_quantity",
            items=[
                {
                    "name": "Синтетическое блюдо",
                    "lookup_query": "синтетическое блюдо",
                    "quantity": None,
                }
            ],
        )
    assert _safe_validation_error_fields(missing_quantity.value) == {
        "llm_validation_errors": [{"loc": [], "type": "value_error"}]
    }


def test_api_multi_action_response_is_validated_by_domain_contracts() -> None:
    outcome = api_result_to_outcome(
        api_actions(
            {
                "action": "set_quantity",
                "items": [
                    {
                        "name": "Первое блюдо",
                        "lookup_query": "первое блюдо",
                        "quantity": 1,
                    }
                ],
            },
            {
                "action": "remove",
                "items": [
                    {
                        "name": "Второе блюдо",
                        "lookup_query": "второе блюдо",
                        "quantity": None,
                    }
                ],
            },
        )
    )
    assert isinstance(outcome, ActionsOutcome)
    assert [operation.action for operation in outcome.operations] == [
        "set_quantity",
        "remove",
    ]


@pytest.mark.asyncio
async def test_valid_multi_action_result_uses_one_llm_call() -> None:
    extractor, structured = extractor_with_results(
        api_actions(
            {
                "action": "add",
                "items": [
                    {
                        "name": "Первое блюдо",
                        "lookup_query": "первое блюдо",
                        "quantity": 2,
                    }
                ],
            },
            {
                "action": "remove",
                "items": [
                    {
                        "name": "Второе блюдо",
                        "lookup_query": "второе блюдо",
                        "quantity": None,
                    }
                ],
            },
        )
    )
    proposal = await safe_extract(
        extractor,
        "Синтетический составной запрос",
        ContextBuilder().build(ContextSource()),
    )
    assert isinstance(proposal, OrderActionPlan)
    assert [operation.action for operation in proposal.operations] == [
        "add",
        "remove",
    ]
    assert len(structured.calls) == 1


@pytest.mark.asyncio
async def test_multi_action_missing_quantity_retries_once_and_revalidates() -> None:
    def result(quantity: int | None) -> ApiContextualIntentResult:
        return api_actions(
            {
                "action": "set_quantity",
                "items": [
                    {
                        "name": "Первое блюдо",
                        "lookup_query": "первое блюдо",
                        "quantity": quantity,
                    }
                ],
            },
            {
                "action": "remove",
                "items": [
                    {
                        "name": "Второе блюдо",
                        "lookup_query": "второе блюдо",
                        "quantity": None,
                    }
                ],
            },
        )

    extractor, structured = extractor_with_results(result(None), result(2))
    outcome = await extractor.extract(
        "Синтетический составной запрос",
        ContextBuilder().build(ContextSource()),
    )
    assert isinstance(outcome, ActionsOutcome)
    assert outcome.operations[0].items[0].quantity == 2
    assert len(structured.calls) == 2
    assert "VALIDATION_RULE=missing_quantity_for_set_quantity" in str(
        structured.calls[1][1].content
    )


@pytest.mark.asyncio
async def test_multi_action_hard_limits_fail_closed_without_retry() -> None:
    over_quantity = api_actions(
        {
            "action": "add",
            "items": [
                {
                    "name": "Первое блюдо",
                    "lookup_query": "первое блюдо",
                    "quantity": 21,
                }
            ],
        },
        {
            "action": "remove",
            "items": [
                {
                    "name": "Второе блюдо",
                    "lookup_query": "второе блюдо",
                    "quantity": None,
                }
            ],
        },
    )
    extractor, structured = extractor_with_results(over_quantity)
    rejected = await safe_extract(
        extractor,
        "Синтетический составной запрос",
        ContextBuilder().build(ContextSource()),
    )
    assert rejected.action == "unknown"
    assert rejected.clarification == QUANTITY_OUT_OF_RANGE_MESSAGE
    assert len(structured.calls) == 1

    too_many = api_actions(
        *[
            {
                "action": "add",
                "items": [
                    {
                        "name": f"Блюдо {index}",
                        "lookup_query": f"блюдо {index}",
                        "quantity": 1,
                    }
                ],
            }
            for index in range(9)
        ]
    )
    count_extractor, count_structured = extractor_with_results(too_many)
    count_rejected = await safe_extract(
        count_extractor,
        "Синтетический составной запрос",
        ContextBuilder().build(ContextSource()),
    )
    assert count_rejected.action == "unknown"
    assert len(count_structured.calls) == 1


@pytest.mark.asyncio
async def test_multi_action_stage_allowlist_and_pii_guard_fail_closed(caplog) -> None:
    disallowed_extractor, disallowed_structured = extractor_with_results(
        api_actions(
            {
                "action": "add",
                "items": [
                    {
                        "name": "Первое блюдо",
                        "lookup_query": "первое блюдо",
                        "quantity": 1,
                    }
                ],
            },
            {
                "action": "remove",
                "items": [
                    {
                        "name": "Второе блюдо",
                        "lookup_query": "второе блюдо",
                        "quantity": None,
                    }
                ],
            },
        )
    )
    disallowed = await safe_extract(
        disallowed_extractor,
        "Синтетический составной запрос",
        ContextBuilder().build(ContextSource(order_stage="confirmed")),
    )
    assert disallowed.action == "unknown"
    assert len(disallowed_structured.calls) == 1

    private_value = "+34 600 000 001"
    pii_extractor, _ = extractor_with_results(
        api_actions(
            {
                "action": "add",
                "items": [
                    {
                        "name": private_value,
                        "lookup_query": "первое блюдо",
                        "quantity": 1,
                    }
                ],
            },
            {
                "action": "remove",
                "items": [
                    {
                        "name": "Второе блюдо",
                        "lookup_query": "второе блюдо",
                        "quantity": None,
                    }
                ],
            },
        ),
        debug_response_json=True,
    )
    with caplog.at_level("INFO", logger="app.orders.intent"):
        rejected = await safe_extract(
            pii_extractor,
            "Синтетический составной запрос",
            ContextBuilder().build(ContextSource()),
        )
    assert rejected.action == "unknown"
    assert private_value not in caplog.text


@pytest.mark.asyncio
async def test_valid_domain_outcome_uses_one_llm_call() -> None:
    extractor, structured = extractor_with_results(
        api_action("set_quantity", quantity=2)
    )
    outcome = await extractor.extract(
        "Синтетический запрос",
        ContextBuilder().build(ContextSource()),
    )
    assert outcome.action == "set_quantity"
    assert outcome.items[0].quantity == 2
    assert len(structured.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "quantity"),
    [
        ("add", 1),
        ("set_quantity", 2),
        ("remove", None),
    ],
)
async def test_item_actions_drop_unused_selectors_without_retry(
    action: str,
    quantity: int | None,
) -> None:
    extractor, structured = extractor_with_results(
        api_action(
            action,
            quantity=quantity,
            query="unused query",
            category="Unused category",
            dietary_tags=["unused tag"],
        )
    )
    outcome = await extractor.extract(
        "Синтетический запрос",
        ContextBuilder().build(ContextSource()),
    )
    assert outcome.action == action
    assert outcome.query is None
    assert outcome.category is None
    assert outcome.dietary_tags == []
    assert len(structured.calls) == 1


def test_search_menu_preserves_selectors_and_drops_unused_items() -> None:
    result = api_action(
        "search_menu",
        quantity=1,
        query="синтетический поиск",
        category="Синтетическая категория",
        dietary_tags=["синтетический тег"],
    )
    outcome = api_result_to_outcome(result)
    assert outcome == ActionOutcome(
        action="search_menu",
        query="синтетический поиск",
        category="Синтетическая категория",
        dietary_tags=["синтетический тег"],
    )


def test_unknown_api_action_is_rejected() -> None:
    with pytest.raises(ValidationError):
        api_action("unknown_action")


@pytest.mark.asyncio
async def test_missing_quantity_retries_once_with_safe_rule_feedback(caplog) -> None:
    private_label = "Private Dish Label"
    extractor, structured = extractor_with_results(
        api_action("set_quantity", quantity=None, item_name=private_label),
        api_action("set_quantity", quantity=2),
    )
    with caplog.at_level("INFO", logger="app.orders.intent"):
        outcome = await extractor.extract(
            "Синтетический запрос",
            ContextBuilder().build(ContextSource()),
        )
    assert outcome.items[0].quantity == 2
    assert len(structured.calls) == 2
    retry_feedback = str(structured.calls[1][1].content)
    assert "VALIDATION_RULE=missing_quantity_for_set_quantity" in retry_feedback
    assert private_label not in retry_feedback
    diagnostics = [
        record
        for record in caplog.records
        if record.getMessage() == "Order domain validation diagnostic"
    ]
    assert [record.validation_rule for record in diagnostics] == [
        "missing_quantity_for_set_quantity",
        "none",
    ]
    assert all(private_label not in record.getMessage() for record in diagnostics)
    safe_diagnostics = json.dumps(
        [
            {
                "kind": record.kind,
                "action": record.action,
                "items_count": record.items_count,
                "quantity_present": record.quantity_present,
                "quantity_valid": record.quantity_valid,
                "validation_rule": record.validation_rule,
            }
            for record in diagnostics
        ]
    )
    assert private_label not in safe_diagnostics


@pytest.mark.asyncio
async def test_debug_response_json_is_disabled_by_default(caplog) -> None:
    extractor, _ = extractor_with_results(api_action("view_cart"))
    with caplog.at_level("INFO", logger="app.orders.intent"):
        await extractor.extract(
            "Синтетический запрос",
            ContextBuilder().build(ContextSource()),
        )
    assert not any(
        record.getMessage() == "Order structured response debug"
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_dev_debug_logs_redacted_json_for_initial_and_retry(caplog) -> None:
    private_label = "Private Dish Label"
    extractor, structured = extractor_with_results(
        api_action("set_quantity", quantity=None, item_name=private_label),
        api_action("set_quantity", quantity=2, item_name=private_label),
        debug_response_json=True,
    )
    with caplog.at_level("INFO", logger="app.orders.intent"):
        outcome = await extractor.extract(
            "Синтетический запрос",
            ContextBuilder().build(ContextSource()),
        )
    assert outcome.items[0].quantity == 2
    assert len(structured.calls) == 2
    records = [
        record
        for record in caplog.records
        if record.getMessage() == "Order structured response debug"
    ]
    assert [record.llm_attempt for record in records] == [1, 2]
    payloads = [json.loads(record.llm_response_json) for record in records]
    assert [payload["items"][0]["quantity"] for payload in payloads] == [None, 2]
    assert all(payload["items"][0]["name"] == "[REDACTED]" for payload in payloads)
    assert private_label not in json.dumps(payloads)


@pytest.mark.asyncio
async def test_debug_json_redacts_pii_and_secret_like_strings(caplog) -> None:
    pii_label = "+34 600 000 001"
    secret_query = "sk-synthetic_secret_value_123456789"
    extractor, _ = extractor_with_results(
        api_action(
            "add",
            quantity=1,
            item_name=pii_label,
            query=secret_query,
        ),
        debug_response_json=True,
    )
    with caplog.at_level("INFO", logger="app.orders.intent"):
        proposal = await safe_extract(
            extractor,
            "Синтетический запрос",
            ContextBuilder().build(ContextSource()),
        )
    assert proposal.action == "unknown"
    record = next(
        record
        for record in caplog.records
        if record.getMessage() == "Order structured response debug"
    )
    assert pii_label not in record.llm_response_json
    assert secret_query not in record.llm_response_json
    assert "[REDACTED]" in record.llm_response_json


def test_debug_response_json_is_size_bounded() -> None:
    result = ApiContextualIntentResult(
        kind="action",
        action="add",
        items=[
            {
                "name": f"Private item {index}",
                "lookup_query": f"private lookup {index}",
                "quantity": 1,
            }
            for index in range(200)
        ],
        query=None,
        category=None,
        dietary_tags=[],
        operations=[],
        clarification_type=None,
        question=None,
        reason=None,
    )
    encoded = _safe_debug_response_json(result)
    payload = json.loads(encoded)
    assert len(encoded) < 4000
    assert payload == {
        "truncated": True,
        "kind": "action",
        "action": "add",
        "items_count": 200,
        "dietary_tags_count": 0,
        "operations_count": 0,
    }


@pytest.mark.asyncio
async def test_missing_quantity_second_failure_fails_closed() -> None:
    extractor, structured = extractor_with_results(
        api_action("set_quantity", quantity=None),
        api_action("set_quantity", quantity=None),
    )
    proposal = await safe_extract(
        extractor,
        "Синтетический запрос",
        ContextBuilder().build(ContextSource()),
    )
    assert proposal.action == "unknown"
    assert proposal.clarification is None
    assert len(structured.calls) == 2


@pytest.mark.asyncio
async def test_quantity_above_limit_is_not_retried_and_fails_closed() -> None:
    extractor, structured = extractor_with_results(
        api_action("set_quantity", quantity=21)
    )
    proposal = await safe_extract(
        extractor,
        "Синтетический запрос",
        ContextBuilder().build(ContextSource()),
    )
    assert proposal.action == "unknown"
    assert proposal.clarification == QUANTITY_OUT_OF_RANGE_MESSAGE
    assert len(structured.calls) == 1


@pytest.mark.asyncio
async def test_single_quantity_validation_routes_to_order_without_cart_mutation() -> (
    None
):
    extractor, structured = extractor_with_results(
        api_action("set_quantity", quantity=25)
    )
    order = OrderDialog(unused_menu_factory, FakeSettings(), extractor)
    original_cart = [{"dish_id": str(uuid4()), "quantity": 2}]
    state = {
        "order_stage": "idle",
        "cart": original_cart,
        "draft_revision": 4,
        "user_message": "Синтетическое изменение количества",
    }

    route, proposal = await order.classify(state)
    assert route == "order"
    assert proposal["clarification"] == QUANTITY_OUT_OF_RANGE_MESSAGE
    changes = await order.handle({**state, "action_proposal": proposal})

    assert changes == {
        "response": QUANTITY_OUT_OF_RANGE_MESSAGE,
        "error_code": "order_clarification",
    }
    assert state["cart"] == original_cart
    assert len(structured.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_index", [0, 1])
async def test_multi_quantity_validation_in_any_position_is_atomic(
    invalid_index: int,
) -> None:
    operations = [
        {
            "action": "add",
            "items": [
                {
                    "name": "Первое блюдо",
                    "lookup_query": "первое блюдо",
                    "quantity": 1,
                }
            ],
        },
        {
            "action": "set_quantity",
            "items": [
                {
                    "name": "Второе блюдо",
                    "lookup_query": "второе блюдо",
                    "quantity": 25,
                }
            ],
        },
    ]
    if invalid_index == 0:
        operations.reverse()
    extractor, structured = extractor_with_results(api_actions(*operations))
    order = OrderDialog(unused_menu_factory, FakeSettings(), extractor)
    original_cart = [{"dish_id": str(uuid4()), "quantity": 2}]
    state = {
        "order_stage": "idle",
        "cart": original_cart,
        "draft_revision": 6,
        "user_message": "Синтетический составной запрос",
    }

    route, proposal = await order.classify(state)
    assert route == "order"
    changes = await order.handle({**state, "action_proposal": proposal})

    assert changes["response"] == QUANTITY_OUT_OF_RANGE_MESSAGE
    assert "cart" not in changes
    assert state["cart"] == original_cart
    assert len(structured.calls) == 1


@pytest.mark.asyncio
async def test_quantity_at_max_is_valid_for_single_and_multi_outcomes() -> None:
    single_extractor, single_calls = extractor_with_results(
        api_action("set_quantity", quantity=20)
    )
    single = await single_extractor.extract(
        "Синтетический запрос",
        ContextBuilder().build(ContextSource()),
    )
    assert isinstance(single, ActionOutcome)
    assert single.items[0].quantity == 20
    assert len(single_calls.calls) == 1

    multi_extractor, multi_calls = extractor_with_results(
        api_actions(
            {
                "action": "add",
                "items": [
                    {
                        "name": "Первое блюдо",
                        "lookup_query": "первое блюдо",
                        "quantity": 1,
                    }
                ],
            },
            {
                "action": "set_quantity",
                "items": [
                    {
                        "name": "Второе блюдо",
                        "lookup_query": "второе блюдо",
                        "quantity": 20,
                    }
                ],
            },
        )
    )
    multi = await multi_extractor.extract(
        "Синтетический составной запрос",
        ContextBuilder().build(ContextSource()),
    )
    assert isinstance(multi, ActionsOutcome)
    assert multi.operations[-1].items[0].quantity == 20
    assert len(multi_calls.calls) == 1


@pytest.mark.asyncio
async def test_unrelated_domain_validation_error_keeps_generic_safe_handling() -> None:
    extractor, structured = extractor_with_results(
        api_action(
            "search_menu",
            query="синтетический поиск",
            dietary_tags=["tag", "TAG"],
        )
    )
    proposal = await safe_extract(
        extractor,
        "Синтетический запрос",
        ContextBuilder().build(ContextSource()),
    )
    assert proposal.action == "unknown"
    assert proposal.clarification is None
    assert len(structured.calls) == 1


@pytest.mark.asyncio
async def test_api_error_is_not_retried() -> None:
    extractor, structured = extractor_with_results(RuntimeError("synthetic"))
    proposal = await safe_extract(
        extractor,
        "Синтетический запрос",
        ContextBuilder().build(ContextSource()),
    )
    assert proposal.action == "unknown"
    assert len(structured.calls) == 1


@pytest.mark.asyncio
async def test_repaired_but_disallowed_action_is_rejected_by_stage_allowlist() -> None:
    extractor, structured = extractor_with_results(api_action("add", quantity=1))
    context = ContextBuilder().build(ContextSource(order_stage="confirmed"))
    proposal = await safe_extract(extractor, "Синтетический запрос", context)
    assert proposal.action == "unknown"
    assert proposal.clarification
    assert len(structured.calls) == 1


@pytest.mark.asyncio
async def test_retried_result_still_passes_output_pii_guard() -> None:
    extractor, structured = extractor_with_results(
        api_action("set_quantity", quantity=None),
        api_action("set_quantity", quantity=2, item_name="+34 600 000 001"),
    )
    proposal = await safe_extract(
        extractor,
        "Синтетический запрос",
        ContextBuilder().build(ContextSource()),
    )
    assert proposal.action == "unknown"
    assert "контактных" in (proposal.clarification or "")
    assert len(structured.calls) == 2


@pytest.mark.asyncio
async def test_retried_action_replay_does_not_repeat_mutation() -> None:
    missing_item = ApiContextualIntentResult(
        kind="action",
        action="add",
        items=[],
        query=None,
        category=None,
        dietary_tags=[],
        operations=[],
        clarification_type=None,
        question=None,
        reason=None,
    )
    extractor, structured = extractor_with_results(
        missing_item,
        api_action("add", quantity=1),
    )

    class RetryingMutationDialog:
        def __init__(self) -> None:
            self.mutations = 0

        async def classify(self, state):
            outcome = await extractor.extract(
                state["user_message"],
                ContextBuilder().build(_context_source(state)),
            )
            proposal = OrderIntentProposal(
                action=outcome.action,
                items=outcome.items,
                query=outcome.query,
                category=outcome.category,
                dietary_tags=outcome.dietary_tags,
            )
            return "order", proposal.model_dump(mode="json")

        async def handle(self, state):
            del state
            self.mutations += 1
            return {
                "response": "synthetic mutation",
                "cart": [{"dish_id": str(uuid4()), "quantity": 1}],
                "draft_revision": 1,
                "error_code": None,
            }

    dialog = RetryingMutationDialog()
    graph = build_agent_graph(
        None,
        checkpointer=InMemorySaver(),
        order_dialog=dialog,
    )
    service = AgentService(graph, persistence_required=True)
    first = await service.handle_message_result(
        "Синтетический запрос",
        "retry-replay",
        update_id=501,
    )
    replay = await service.handle_message_result(
        "Другой текст того же update",
        "retry-replay",
        update_id=501,
    )
    state = await graph.aget_state({"configurable": {"thread_id": "retry-replay"}})
    assert first.text == replay.text == "synthetic mutation"
    assert len(structured.calls) == 2
    assert dialog.mutations == 1
    assert state.values["cart"][0]["quantity"] == 1


def test_llm_prompt_contains_only_allowlisted_context_projection() -> None:
    private_uuid = str(uuid4())
    context = ContextBuilder().build(
        _context_source(
            {
                "order_stage": "idle",
                "cart": [{"dish_id": private_uuid, "quantity": 2}],
                "contacts": {
                    "customer_name": "Мария Приватная",
                    "phone": "+34600000003",
                    "address": "Calle Oculta 20",
                },
                "confirmed_order": {
                    "confirmation_key": "private-confirmation-key",
                    "phone": "+34600000004",
                },
                "callback_confirmation_id": "session00002",
                "conversation_id": "telegram:v1:private-thread",
            }
        )
    )
    messages = build_intent_messages("Что можно сделать дальше?", context)
    captured = "\n".join(str(message.content) for message in messages)
    for forbidden in (
        private_uuid,
        "Мария Приватная",
        "+34600000003",
        "Calle Oculta 20",
        "private-confirmation-key",
        "session00002",
        "private-thread",
    ):
        assert forbidden not in captured
    assert '"has_active_cart":true' in captured
    assert '"cart_unit_count":2' in captured


@pytest.mark.asyncio
async def test_context_changes_extractor_result_and_disallowed_action_fails_safe() -> (
    None
):
    class ContextAwareExtractor:
        def __init__(self) -> None:
            self.contexts = []

        async def extract(self, message, context):
            del message
            self.contexts.append(context)
            if context.has_active_cart:
                return ClarifyOutcome(
                    clarification_type="ambiguous_intent",
                    question="Показать текущую корзину или перейти к оформлению?",
                )
            return ActionOutcome(action="browse_menu")

    extractor = ContextAwareExtractor()
    order = OrderDialog(unused_menu_factory, FakeSettings(), extractor)
    route, proposal = await order.classify(
        {
            "order_stage": "idle",
            "cart": [{"dish_id": str(uuid4()), "quantity": 1}],
            "user_message": "Что дальше",
        }
    )
    assert route == "order"
    assert proposal["action"] == "unknown"
    assert "текущую корзину" in proposal["clarification"]
    assert extractor.contexts[-1].cart_unit_count == 1

    no_cart_route, no_cart_proposal = await order.classify(
        {"order_stage": "idle", "cart": [], "user_message": "Что дальше"}
    )
    assert no_cart_route == "order"
    assert no_cart_proposal["action"] == "browse_menu"

    confirmed_context = ContextBuilder().build(
        _context_source({"order_stage": "confirmed", "confirmed_order": {}})
    )

    class DisallowedExtractor:
        async def extract(self, message, context):
            del message, context
            return ActionOutcome(action="add", items=[{"name": "Брускетта"}])

    rejected = await safe_extract(
        DisallowedExtractor(),
        "Добавь блюдо",
        confirmed_context,
    )
    assert rejected.action == "unknown"
    assert rejected.clarification


@pytest.mark.asyncio
async def test_obvious_pii_gate_does_not_call_extractor() -> None:
    class NeverExtractor:
        async def extract(self, message, context):
            raise AssertionError((message, context))

    context = ContextBuilder().build(ContextSource())
    proposal = await safe_extract(
        NeverExtractor(),
        "Позвоните мне +34 600 000 001",
        context,
    )
    assert proposal.action == "unknown"
    assert "контактные" in (proposal.clarification or "")


@pytest.mark.asyncio
async def test_malformed_outcome_and_unsafe_clarification_fail_closed() -> None:
    context = ContextBuilder().build(ContextSource())

    class MalformedExtractor:
        async def extract(self, message, current_context):
            del message, current_context
            return {"kind": "action", "action": "add"}

    malformed = await safe_extract(MalformedExtractor(), "Сделай что-нибудь", context)
    assert malformed.action == "unknown"
    assert malformed.clarification is None

    class UnsafeClarificationExtractor:
        async def extract(self, message, current_context):
            del message, current_context
            return ClarifyOutcome(
                clarification_type="ambiguous_intent",
                question="Подтвердите callback session_id session00001",
            )

    clarified = await safe_extract(
        UnsafeClarificationExtractor(),
        "Что дальше",
        context,
    )
    assert clarified.action == "unknown"
    assert clarified.clarification == (
        "Уточните, пожалуйста, что вы хотите сделать с меню или корзиной."
    )


@pytest.mark.asyncio
async def test_retrospective_clarification_does_not_mutate_cart() -> None:
    class RetrospectiveExtractor:
        async def extract(self, message, context):
            assert context.has_active_cart is True
            assert message == "Я добавлял брускетту и лимонад"
            return ClarifyOutcome(
                clarification_type="retrospective_intent",
                question="Вы хотите проверить корзину или добавить эти блюда снова?",
            )

    dialog = OrderDialog(unused_menu_factory, FakeSettings(), RetrospectiveExtractor())
    saver = InMemorySaver()
    graph = build_agent_graph(None, checkpointer=saver, order_dialog=dialog)
    config = {"configurable": {"thread_id": "retrospective"}}
    original_cart = [{"dish_id": str(uuid4()), "quantity": 1}]
    await graph.aupdate_state(
        config,
        {"order_stage": "idle", "cart": original_cart, "draft_revision": 4},
    )
    service = AgentService(graph, persistence_required=True)

    result = await service.handle_message_result(
        "Я добавлял брускетту и лимонад",
        "retrospective",
        update_id=1,
    )
    state = await graph.aget_state(config)
    assert "добавить эти блюда снова" in result.text
    assert state.values["cart"] == original_cart
    assert state.values["draft_revision"] == 4
    assert "pending_clarification" not in state.values
    assert state.values["recent_interactions"][-1] == {
        "source": "text",
        "semantic": "unknown",
        "outcome": "clarified",
        "dish_name": None,
    }


@pytest.mark.asyncio
async def test_contact_text_is_always_captured_without_contextual_extractor() -> None:
    class NeverExtractor:
        async def extract(self, message, context):
            raise AssertionError((message, context))

    dialog = OrderDialog(unused_menu_factory, FakeSettings(), NeverExtractor())
    for stage, message in (
        ("collecting_name", "Покажи салаты"),
        ("collecting_phone", "Какие у вас часы работы?"),
        ("collecting_address", "Мой адрес доставки"),
        ("collecting_time", "Отмена"),
        ("collecting_edit_name", "Новый заказ"),
        ("collecting_edit_phone", "+34 600 000 001"),
        ("collecting_edit_address", "Calle Sintética 10"),
        ("collecting_edit_time", "Как можно скорее"),
    ):
        route, proposal = await dialog.classify(
            {"order_stage": stage, "user_message": message}
        )
        assert route == "order"
        assert proposal is None

    name_route, name_proposal = await dialog.classify(
        {"order_stage": "collecting_name", "user_message": "Иван Тестов"}
    )
    assert name_route == "order"
    assert name_proposal is None
    name_result = await dialog.handle(
        {
            "order_stage": "collecting_name",
            "contacts": {},
            "user_message": "Иван Тестов",
        }
    )
    assert name_result["contacts"]["customer_name"] == "Иван Тестов"
    assert name_result["order_stage"] == "collecting_phone"


@pytest.mark.asyncio
async def test_faq_like_text_during_checkout_is_field_validation() -> None:
    class Faq:
        async def answer(self, question):
            raise AssertionError(question)

    class NeverExtractor:
        async def extract(self, message, context):
            raise AssertionError((message, context))

    dialog = OrderDialog(unused_menu_factory, FakeSettings(), NeverExtractor())
    saver = InMemorySaver()
    graph = build_agent_graph(Faq(), checkpointer=saver, order_dialog=dialog)
    config = {"configurable": {"thread_id": "faq-checkout"}}
    cart = [{"dish_id": str(uuid4()), "quantity": 2}]
    contacts = {"customer_name": "Иван Тестов"}
    await graph.aupdate_state(
        config,
        {
            "order_stage": "collecting_phone",
            "cart": cart,
            "contacts": contacts,
            "draft_revision": 2,
        },
    )
    service = AgentService(graph, persistence_required=True)
    result = await service.handle_message_result(
        "Какие у вас часы работы?",
        "faq-checkout",
        update_id=10,
    )
    state = await graph.aget_state(config)
    assert result.text == "Укажите корректный номер телефона (7–15 цифр)."
    assert state.values["order_stage"] == "collecting_phone"
    assert state.values["cart"] == cart
    assert state.values["contacts"] == contacts
    assert state.values["recent_interactions"][-1]["semantic"] == "contact_input"


@pytest.mark.asyncio
async def test_checkout_pii_is_not_forwarded_after_cancel_or_written_to_logs(
    caplog,
) -> None:
    captured: list[tuple[str, str]] = []

    class SpyExtractor:
        async def extract(self, message, context):
            captured.append((message, context.model_dump_json()))
            return UnsupportedOutcome(reason="not_supported")

    class NeverFaq:
        async def answer(self, question):
            raise AssertionError(question)

    dialog = OrderDialog(unused_menu_factory, FakeSettings(), SpyExtractor())
    saver = InMemorySaver()
    graph = build_agent_graph(NeverFaq(), checkpointer=saver, order_dialog=dialog)
    config = {"configurable": {"thread_id": "pii-isolation"}}
    await graph.aupdate_state(
        config,
        {
            "order_stage": "collecting_name",
            "cart": [{"dish_id": str(uuid4()), "quantity": 1}],
            "contacts": {},
            "draft_revision": 1,
            "checkout_session_id": "session00001",
        },
    )
    service = AgentService(graph, persistence_required=True)
    private_values = (
        "Zoraya Sentinel",
        "+34999111222",
        "Calle Privacidad 77",
    )
    with caplog.at_level("INFO"):
        for update_id, value in enumerate(private_values, start=1):
            result = await service.handle_message_result(
                value, "pii-isolation", update_id=update_id
            )
        cancelled = await service.handle_message_result(
            "Отменить оформление",
            "pii-isolation",
            update_id=4,
            callback_action="cancel_checkout",
            callback_revision=result.draft_revision,
            callback_confirmation_id=result.checkout_session_id,
        )
        await service.handle_message_result(
            "Синтетический неизвестный запрос",
            "pii-isolation",
            update_id=5,
        )

    assert cancelled.text == "Оформление отменено. Корзина сохранена."
    assert len(captured) == 1
    assert captured[0][0] == "Синтетический неизвестный запрос"
    for private_value in private_values:
        assert private_value not in captured[0][1]
        assert private_value not in caplog.text


@pytest.mark.asyncio
async def test_semantic_history_is_fifo_callback_aware_and_replay_safe() -> None:
    class SemanticDialog:
        async def classify(self, state):
            action = state.get("callback_action") or "view_cart"
            return "order", {"action": action}

        async def handle(self, state):
            return {
                "response": f"raw response {state.get('user_message')}",
                "error_code": None,
            }

    saver = InMemorySaver()
    graph = build_agent_graph(None, checkpointer=saver, order_dialog=SemanticDialog())
    service = AgentService(graph, persistence_required=True)
    for update_id in range(1, 13):
        await service.handle_message(
            f"private raw message {update_id}",
            "semantic-history",
            update_id=update_id,
        )
    before_replay = await graph.aget_state(
        {"configurable": {"thread_id": "semantic-history"}}
    )
    assert len(before_replay.values["recent_interactions"]) == 10
    assert all(
        event
        == {
            "source": "text",
            "semantic": "view_cart",
            "outcome": "handled",
            "dish_name": None,
        }
        for event in before_replay.values["recent_interactions"]
    )
    assert "private raw" not in json.dumps(
        before_replay.values["recent_interactions"], ensure_ascii=False
    )

    await service.handle_message(
        "different replay text",
        "semantic-history",
        update_id=12,
    )
    after_replay = await graph.aget_state(
        {"configurable": {"thread_id": "semantic-history"}}
    )
    assert (
        after_replay.values["recent_interactions"]
        == before_replay.values["recent_interactions"]
    )

    await service.handle_message_result(
        "",
        "semantic-history",
        update_id=13,
        callback_action="edit",
        callback_revision=0,
    )
    after_callback = await graph.aget_state(
        {"configurable": {"thread_id": "semantic-history"}}
    )
    assert after_callback.values["recent_interactions"][-1]["source"] == "callback"
    assert after_callback.values["recent_interactions"][-1]["semantic"] == "edit"
    assert after_callback.values["last_action"] == "edit"

    restarted_graph = build_agent_graph(
        None,
        checkpointer=saver,
        order_dialog=SemanticDialog(),
    )
    restarted_service = AgentService(restarted_graph, persistence_required=True)
    await restarted_service.handle_message(
        "after restart",
        "semantic-history",
        update_id=14,
    )
    after_restart = await restarted_graph.aget_state(
        {"configurable": {"thread_id": "semantic-history"}}
    )
    assert len(after_restart.values["recent_interactions"]) == 10
    assert after_restart.values["recent_interactions"][-1]["semantic"] == "view_cart"


@pytest.mark.asyncio
async def test_new_order_resets_transient_context_but_keeps_fresh_semantic_event() -> (
    None
):
    dialog = OrderDialog(unused_menu_factory, FakeSettings(), None)
    saver = InMemorySaver()
    graph = build_agent_graph(None, checkpointer=saver, order_dialog=dialog)
    config = {"configurable": {"thread_id": "new-order-context"}}
    await graph.aupdate_state(
        config,
        {
            "order_stage": "idle",
            "cart": [],
            "draft_revision": 2,
            "last_action": "dish_info",
            "last_referenced_dish_name": "Брускетта",
            "recent_interactions": [
                {
                    "source": "text",
                    "semantic": "dish_info",
                    "outcome": "handled",
                    "dish_name": "Брускетта",
                }
            ],
        },
    )
    service = AgentService(graph, persistence_required=True)
    await service.handle_message("Новый заказ", "new-order-context", update_id=20)
    state = await graph.aget_state(config)
    assert state.values["last_referenced_dish_name"] is None
    assert state.values["last_action"] == "new_order"
    assert state.values["recent_interactions"] == [
        {
            "source": "text",
            "semantic": "new_order",
            "outcome": "handled",
            "dish_name": None,
        }
    ]
    (OrderActionPlan,)
