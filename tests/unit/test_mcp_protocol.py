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

    async def list_categories(self):
        from app.menu.contracts import CategoryList, CategorySummary

        return CategoryList(
            categories=[CategorySummary(id="main", name="main", available_dish_count=1)]
        )

    async def browse_menu(self, category=None, offset=0, limit=5):
        from app.menu.contracts import BrowseMenuItem, BrowseMenuResult

        category = " ".join(category.split())
        items = []
        if category == "main":
            items.append(
                BrowseMenuItem(
                    dish_id=KNOWN_DISH_ID,
                    name="Паста",
                    price_minor=1234,
                    currency="EUR",
                )
            )
        return BrowseMenuResult(
            category=category,
            items=items,
            offset=offset,
            limit=limit,
            has_more=False,
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

    async def inspect_cart(self, items: list[CartItemInput]):
        from app.menu.contracts import CartInspection, InspectedCartItem

        return CartInspection(
            items=[
                InspectedCartItem(
                    dish_id=item.dish_id,
                    name="Паста",
                    quantity=item.quantity,
                    unit_price_minor=1234,
                    line_total_minor=1234 * item.quantity,
                    available=True,
                    missing=False,
                )
                for item in items
            ],
            available_subtotal_minor=sum(1234 * item.quantity for item in items),
            has_unavailable_items=False,
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
            "list_categories",
            "browse_menu",
            "search_menu",
            "get_dish",
            "calculate_cart",
            "inspect_cart",
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

        categories = await client.call_tool("list_categories", {})
        assert categories.is_error is False
        assert categories.structured_content["categories"][0]["id"] == "main"

        page = await client.call_tool(
            "browse_menu", {"category": "main", "offset": 0, "limit": 5}
        )
        assert page.is_error is False
        assert page.structured_content["items"][0]["dish_id"] == str(KNOWN_DISH_ID)

        empty_page = await client.call_tool("browse_menu", {"category": "unknown"})
        assert empty_page.is_error is False
        assert empty_page.structured_content["items"] == []

        browse_tool = next(tool for tool in listed.tools if tool.name == "browse_menu")
        assert "category" in browse_tool.input_schema["required"]
        assert browse_tool.input_schema["properties"]["category"]["type"] == "string"
        for arguments in (
            {},
            {"category": None},
            {"category": ""},
            {"category": "   "},
            {"category": 123},
        ):
            assert (await client.call_tool("browse_menu", arguments)).is_error
        normalized = await client.call_tool("browse_menu", {"category": " main "})
        assert normalized.structured_content["category"] == "main"

        detail = await client.call_tool("get_dish", {"dish_id": str(KNOWN_DISH_ID)})
        assert detail.is_error is False
        assert detail.structured_content["available"] is True

        cart = await client.call_tool(
            "calculate_cart",
            {"items": [{"dish_id": str(KNOWN_DISH_ID), "quantity": 2}]},
        )
        assert cart.is_error is False
        assert cart.structured_content["subtotal_minor"] == 2468

        inspection = await client.call_tool(
            "inspect_cart",
            {"items": [{"dish_id": str(KNOWN_DISH_ID), "quantity": 2}]},
        )
        assert inspection.is_error is False
        assert inspection.structured_content["available_subtotal_minor"] == 2468

        empty_inspection = await client.call_tool("inspect_cart", {"items": []})
        assert empty_inspection.is_error is False
        assert empty_inspection.structured_content["items"] == []


@pytest.mark.asyncio
async def test_protocol_rejects_bounds_and_returns_safe_tool_error() -> None:
    server = create_mcp_server(fake_service_context)
    async with Client(server) as client:
        invalid = await client.call_tool(
            "calculate_cart",
            {"items": [{"dish_id": str(KNOWN_DISH_ID), "quantity": 21}]},
        )
        assert invalid.is_error is True

        invalid_page = await client.call_tool(
            "browse_menu", {"category": "main", "offset": -1, "limit": 21}
        )
        assert invalid_page.is_error is True

        invalid_inspection = await client.call_tool(
            "inspect_cart",
            {"items": [{"dish_id": str(KNOWN_DISH_ID), "quantity": 21}]},
        )
        assert invalid_inspection.is_error is True

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
