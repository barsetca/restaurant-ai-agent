import asyncio
import logging
import sys
from contextlib import AsyncExitStack
from types import TracebackType
from typing import Any, Self
from uuid import UUID

from mcp import Client
from mcp.client.stdio import StdioServerParameters, stdio_client
from pydantic import ValidationError

from app.menu.contracts import (
    DEFAULT_BROWSE_LIMIT,
    BrowseMenuResult,
    CartInspection,
    CartItemInput,
    CartQuote,
    CategoryList,
    DishDetail,
    MenuSearchResult,
)

logger = logging.getLogger(__name__)


class MenuMcpClientError(RuntimeError):
    """Safe application-facing MCP adapter failure."""


class MenuMcpTimeoutError(MenuMcpClientError):
    pass


class MenuMcpToolError(MenuMcpClientError):
    pass


class MenuMcpProtocolError(MenuMcpClientError):
    pass


class MenuMcpClient:
    def __init__(
        self,
        database_url: str,
        *,
        startup_timeout_seconds: float = 10.0,
        tool_timeout_seconds: float = 15.0,
        shutdown_timeout_seconds: float = 5.0,
        log_level: str = "INFO",
    ) -> None:
        self._database_url = database_url
        self._startup_timeout_seconds = startup_timeout_seconds
        self._tool_timeout_seconds = tool_timeout_seconds
        self._shutdown_timeout_seconds = shutdown_timeout_seconds
        self._log_level = log_level
        self._stack: AsyncExitStack | None = None
        self._client: Client | None = None

    async def __aenter__(self) -> Self:
        if self._stack is not None:
            raise RuntimeError("MenuMcpClient is already open")
        stack = AsyncExitStack()
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-m", "mcp_server"],
            env={"DATABASE_URL": self._database_url, "LOG_LEVEL": self._log_level},
        )
        client = Client(
            stdio_client(parameters),
            read_timeout_seconds=self._tool_timeout_seconds,
        )
        try:
            async with asyncio.timeout(self._startup_timeout_seconds):
                await stack.enter_async_context(client)
        except TimeoutError as exc:
            await stack.aclose()
            logger.warning("Menu MCP startup timed out")
            raise MenuMcpTimeoutError("Menu MCP startup timed out") from exc
        except Exception as exc:
            await stack.aclose()
            logger.error(
                "Menu MCP startup failed",
                extra={"exception_type": type(exc).__name__},
            )
            raise MenuMcpClientError("Menu MCP startup failed") from exc
        self._stack = stack
        self._client = client
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc, traceback
        stack, self._stack = self._stack, None
        self._client = None
        if stack is None:
            return
        try:
            async with asyncio.timeout(self._shutdown_timeout_seconds):
                await stack.aclose()
        except TimeoutError:
            logger.error("Menu MCP shutdown timed out")
            raise MenuMcpTimeoutError("Menu MCP shutdown timed out") from None

    def _connected_client(self) -> Client:
        if self._client is None:
            raise RuntimeError("MenuMcpClient must be used as an async context manager")
        return self._client

    async def list_tool_names(self) -> list[str]:
        try:
            async with asyncio.timeout(self._tool_timeout_seconds):
                result = await self._connected_client().list_tools()
        except TimeoutError as exc:
            raise MenuMcpTimeoutError("Menu MCP request timed out") from exc
        return sorted(tool.name for tool in result.tools)

    async def _call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            async with asyncio.timeout(self._tool_timeout_seconds):
                result = await self._connected_client().call_tool(name, arguments)
        except TimeoutError as exc:
            logger.warning("Menu MCP tool call timed out", extra={"endpoint": name})
            raise MenuMcpTimeoutError("Menu MCP request timed out") from exc
        except Exception as exc:
            logger.error(
                "Menu MCP protocol call failed",
                extra={"endpoint": name, "exception_type": type(exc).__name__},
            )
            raise MenuMcpProtocolError("Menu MCP protocol call failed") from exc
        if result.is_error:
            raise MenuMcpToolError("Menu MCP tool rejected the request")
        if not isinstance(result.structured_content, dict):
            raise MenuMcpProtocolError("Menu MCP returned invalid structured data")
        return result.structured_content

    async def search_menu(
        self,
        query: str | None = None,
        category: str | None = None,
        dietary_tags: list[str] | None = None,
    ) -> MenuSearchResult:
        arguments: dict[str, Any] = {}
        if query is not None:
            arguments["query"] = query
        if category is not None:
            arguments["category"] = category
        if dietary_tags is not None:
            arguments["dietary_tags"] = dietary_tags
        return self._validate(
            MenuSearchResult, await self._call("search_menu", arguments)
        )

    async def list_categories(self) -> CategoryList:
        return self._validate(CategoryList, await self._call("list_categories", {}))

    async def browse_menu(
        self,
        category: str,
        offset: int = 0,
        limit: int = DEFAULT_BROWSE_LIMIT,
    ) -> BrowseMenuResult:
        arguments: dict[str, Any] = {"offset": offset, "limit": limit}
        if category is not None:
            arguments["category"] = category
        return self._validate(
            BrowseMenuResult,
            await self._call("browse_menu", arguments),
        )

    async def get_dish(self, dish_id: UUID) -> DishDetail:
        return self._validate(
            DishDetail,
            await self._call("get_dish", {"dish_id": str(dish_id)}),
        )

    async def calculate_cart(self, items: list[CartItemInput]) -> CartQuote:
        return self._validate(
            CartQuote,
            await self._call(
                "calculate_cart",
                {"items": [item.model_dump(mode="json") for item in items]},
            ),
        )

    async def inspect_cart(self, items: list[CartItemInput]) -> CartInspection:
        return self._validate(
            CartInspection,
            await self._call(
                "inspect_cart",
                {"items": [item.model_dump(mode="json") for item in items]},
            ),
        )

    @staticmethod
    def _validate(model: type[Any], data: dict[str, Any]) -> Any:
        try:
            return model.model_validate(data)
        except ValidationError as exc:
            raise MenuMcpProtocolError(
                "Menu MCP returned invalid structured data"
            ) from exc
