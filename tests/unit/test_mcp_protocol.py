from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import uuid4

import pytest
from mcp import Client

from app.menu.contracts import CartItemInput
from app.menu.service import CartValidationError
from mcp_server.server import create_mcp_server


class FakeMenuService:
    async def search_menu(self, query, category=None, dietary_tags=None):
        from app.menu.contracts import DishSummary, MenuSearchResult

        if query == "explode":
            raise RuntimeError("postgresql://user:synthetic-secret@database/internal")
        return MenuSearchResult(
            items=[
                DishSummary(
                    dish_id=KNOWN_DISH_ID,
                    name=f"Паста {query}",
                    category=category or "main",
                    dietary_tags=dietary_tags or [],
                    price_minor=1234,
                    currency="EUR",
                )
            ]
        )

    async def get_dish(self, dish_id):
        from app.menu.contracts import DishDetail

        return DishDetail(
            dish_id=dish_id,
            name="Паста",
            category="main",
            dietary_tags=["vegan"],
            price_minor=1234,
            currency="EUR",
            description="Synthetic",
            ingredients=["томат"],
            allergens=[],
            available=True,
        )

    async def calculate_cart(self, items: list[CartItemInput]):
        from app.menu.contracts import CartLine, CartQuote

        if items[0].quantity == 20:
            raise CartValidationError("Синтетическая ошибка корзины.")
        return CartQuote(
            items=[
                CartLine(
                    dish_id=items[0].dish_id,
                    name="Паста",
                    quantity=items[0].quantity,
                    unit_price_minor=1234,
                    line_total_minor=1234 * items[0].quantity,
                )
            ],
            subtotal_minor=1234 * items[0].quantity,
        )


KNOWN_DISH_ID = uuid4()


@asynccontextmanager
async def fake_service_context() -> AsyncIterator[FakeMenuService]:
    yield FakeMenuService()


@pytest.mark.asyncio
async def test_protocol_lists_and_calls_all_tools_with_structured_output() -> None:
    server = create_mcp_server(fake_service_context)
    async with Client(server) as client:
        listed = await client.list_tools()
        assert {tool.name for tool in listed.tools} == {
            "search_menu",
            "get_dish",
            "calculate_cart",
        }
        search_tool = next(tool for tool in listed.tools if tool.name == "search_menu")
        assert search_tool.output_schema is not None
        assert set(search_tool.input_schema["properties"]) == {
            "query",
            "category",
            "dietary_tags",
        }
        assert "query" not in search_tool.input_schema.get("required", [])

        search = await client.call_tool(
            "search_menu",
            {"query": "томат", "category": "main", "dietary_tags": ["vegan"]},
        )
        assert search.is_error is False
        assert search.structured_content["items"][0]["price_minor"] == 1234

        browse = await client.call_tool("search_menu", {})
        assert browse.is_error is False

        detail = await client.call_tool("get_dish", {"dish_id": str(KNOWN_DISH_ID)})
        assert detail.is_error is False
        assert detail.structured_content["available"] is True

        cart = await client.call_tool(
            "calculate_cart",
            {"items": [{"dish_id": str(KNOWN_DISH_ID), "quantity": 2}]},
        )
        assert cart.is_error is False
        assert cart.structured_content["subtotal_minor"] == 2468


@pytest.mark.asyncio
async def test_protocol_rejects_bounds_and_returns_safe_tool_error() -> None:
    server = create_mcp_server(fake_service_context)
    async with Client(server) as client:
        invalid = await client.call_tool(
            "calculate_cart",
            {"items": [{"dish_id": str(KNOWN_DISH_ID), "quantity": 21}]},
        )
        assert invalid.is_error is True

        business_error = await client.call_tool(
            "calculate_cart",
            {"items": [{"dish_id": str(KNOWN_DISH_ID), "quantity": 20}]},
        )
        assert business_error.is_error is True
        text = business_error.content[0].text
        assert "Синтетическая ошибка корзины" in text
        assert "Traceback" not in text


@pytest.mark.asyncio
async def test_protocol_hides_unexpected_database_error_details(caplog) -> None:
    server = create_mcp_server(fake_service_context)
    async with Client(server) as client:
        failed = await client.call_tool("search_menu", {"query": "explode"})
    assert failed.is_error is True
    text = failed.content[0].text
    assert "Меню временно недоступно" in text
    assert "synthetic-secret" not in text
    assert "synthetic-secret" not in caplog.text
    assert any(record.exception_type == "RuntimeError" for record in caplog.records)
