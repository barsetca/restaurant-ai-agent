import asyncio
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

import app.mcp.client as client_module
from app.mcp.client import (
    MenuMcpClient,
    MenuMcpProtocolError,
    MenuMcpTimeoutError,
    MenuMcpToolError,
)
from app.menu.contracts import CartItemInput


class FakeClient:
    instances: list["FakeClient"] = []
    responses: dict[str, object] = {}
    enter_delay = 0.0
    exit_delay = 0.0

    def __init__(self, parameters, **kwargs) -> None:
        self.parameters = parameters
        self.kwargs = kwargs
        self.closed = False
        self.__class__.instances.append(self)

    async def __aenter__(self):
        await asyncio.sleep(self.enter_delay)
        return self

    async def __aexit__(self, *_args):
        await asyncio.sleep(self.exit_delay)
        self.closed = True

    async def list_tools(self):
        return SimpleNamespace(
            tools=[
                SimpleNamespace(name="search_menu"),
                SimpleNamespace(name="get_dish"),
                SimpleNamespace(name="list_categories"),
                SimpleNamespace(name="browse_menu"),
                SimpleNamespace(name="inspect_cart"),
                SimpleNamespace(name="calculate_cart"),
            ]
        )

    async def call_tool(self, name, arguments):
        response = self.responses[name]
        if isinstance(response, BaseException):
            raise response
        if callable(response):
            return await response(arguments)
        return response


@pytest.fixture(autouse=True)
def fake_client(monkeypatch):
    FakeClient.instances = []
    FakeClient.responses = {}
    FakeClient.enter_delay = 0.0
    FakeClient.exit_delay = 0.0
    monkeypatch.setattr(client_module, "Client", FakeClient)
    monkeypatch.setattr(client_module, "stdio_client", lambda parameters: parameters)


def result(data: dict | None = None, *, is_error: bool = False):
    return SimpleNamespace(is_error=is_error, structured_content=data)


@pytest.mark.asyncio
async def test_adapter_uses_limited_environment_and_validates_results() -> None:
    dish_id = uuid4()
    FakeClient.responses = {
        "search_menu": result(
            {
                "items": [
                    {
                        "dish_id": str(dish_id),
                        "name": "Паста",
                        "category": "main",
                        "dietary_tags": ["vegan"],
                        "price_minor": 1200,
                        "currency": "EUR",
                    }
                ]
            }
        ),
        "get_dish": result(
            {
                "dish_id": str(dish_id),
                "name": "Паста",
                "category": "main",
                "dietary_tags": ["vegan"],
                "price_minor": 1200,
                "currency": "EUR",
                "description": None,
                "ingredients": [],
                "allergens": [],
                "available": True,
            }
        ),
        "calculate_cart": result(
            {
                "items": [
                    {
                        "dish_id": str(dish_id),
                        "name": "Паста",
                        "quantity": 2,
                        "unit_price_minor": 1200,
                        "line_total_minor": 2400,
                    }
                ],
                "subtotal_minor": 2400,
                "currency": "EUR",
            }
        ),
        "list_categories": result(
            {"categories": [{"id": "main", "name": "main", "available_dish_count": 1}]}
        ),
        "browse_menu": result(
            {
                "category": "main",
                "items": [
                    {
                        "dish_id": str(dish_id),
                        "name": "Паста",
                        "price_minor": 1200,
                        "currency": "EUR",
                    }
                ],
                "offset": 0,
                "limit": 5,
                "has_more": False,
            }
        ),
        "inspect_cart": result(
            {
                "items": [
                    {
                        "dish_id": str(dish_id),
                        "name": "Паста",
                        "quantity": 2,
                        "unit_price_minor": 1200,
                        "line_total_minor": 2400,
                        "available": True,
                        "missing": False,
                    }
                ],
                "available_subtotal_minor": 2400,
                "has_unavailable_items": False,
                "currency": "EUR",
            }
        ),
    }

    database_url = "postgresql+asyncpg://user:synthetic@db:5432/restaurant_test"
    async with MenuMcpClient(database_url) as adapter:
        assert await adapter.list_tool_names() == [
            "browse_menu",
            "calculate_cart",
            "get_dish",
            "inspect_cart",
            "list_categories",
            "search_menu",
        ]
        assert (await adapter.search_menu("паста")).items[0].price_minor == 1200
        assert (await adapter.list_categories()).categories[0].id == "main"
        assert (await adapter.browse_menu("main")).items[0].dish_id == dish_id
        assert (await adapter.get_dish(dish_id)).available is True
        quote = await adapter.calculate_cart(
            [CartItemInput(dish_id=dish_id, quantity=2)]
        )
        assert quote.subtotal_minor == 2400
        inspection = await adapter.inspect_cart(
            [CartItemInput(dish_id=dish_id, quantity=2)]
        )
        assert inspection.available_subtotal_minor == 2400

    fake = FakeClient.instances[0]
    assert fake.parameters.env == {"DATABASE_URL": database_url, "LOG_LEVEL": "INFO"}
    assert fake.closed is True


@pytest.mark.asyncio
async def test_adapter_maps_tool_and_protocol_errors_without_logging_secrets(
    caplog,
) -> None:
    FakeClient.responses["search_menu"] = result(is_error=True)
    async with MenuMcpClient("postgresql+asyncpg://u:secret-value@db/test") as adapter:
        with pytest.raises(MenuMcpToolError):
            await adapter.search_menu("паста")

    FakeClient.responses["search_menu"] = RuntimeError("secret-value")
    async with MenuMcpClient("postgresql+asyncpg://u:secret-value@db/test") as adapter:
        with pytest.raises(MenuMcpProtocolError):
            await adapter.search_menu("паста")
    assert "secret-value" not in caplog.text


@pytest.mark.asyncio
async def test_adapter_enforces_startup_tool_and_shutdown_timeouts() -> None:
    FakeClient.enter_delay = 0.05
    with pytest.raises(MenuMcpTimeoutError, match="startup"):
        async with MenuMcpClient(
            "postgresql+asyncpg://u:x@db/test", startup_timeout_seconds=0.001
        ):
            pass

    FakeClient.enter_delay = 0.0

    async def slow_response(_arguments):
        await asyncio.sleep(0.05)

    FakeClient.responses["search_menu"] = slow_response
    async with MenuMcpClient(
        "postgresql+asyncpg://u:x@db/test", tool_timeout_seconds=0.001
    ) as adapter:
        with pytest.raises(MenuMcpTimeoutError, match="request"):
            await adapter.search_menu("паста")

    FakeClient.exit_delay = 0.05
    with pytest.raises(MenuMcpTimeoutError, match="shutdown"):
        async with MenuMcpClient(
            "postgresql+asyncpg://u:x@db/test", shutdown_timeout_seconds=0.001
        ):
            pass


@pytest.mark.asyncio
async def test_adapter_rejects_malformed_structured_output() -> None:
    FakeClient.responses["get_dish"] = result({"dish_id": str(UUID(int=0))})
    async with MenuMcpClient("postgresql+asyncpg://u:x@db/test") as adapter:
        with pytest.raises(MenuMcpProtocolError, match="invalid structured"):
            await adapter.get_dish(UUID(int=0))
