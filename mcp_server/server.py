import logging
import os
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from typing import Annotated, Any
from uuid import UUID

from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field, ValidationError

from app.db.session import create_engine
from app.menu.contracts import (
    DEFAULT_BROWSE_LIMIT,
    MAX_BROWSE_LIMIT,
    MAX_CART_LINES,
    MAX_DIETARY_TAGS,
    BrowseCategory,
    BrowseMenuResult,
    CartInspection,
    CartItemInput,
    CartQuote,
    CategoryList,
    CategoryFilter,
    DietaryTag,
    DishDetail,
    MenuSearchResult,
    SearchQuery,
)
from app.menu.repository import MenuRepository
from app.menu.service import MenuService, MenuServiceError

logger = logging.getLogger(__name__)

CartItems = Annotated[
    list[CartItemInput], Field(min_length=1, max_length=MAX_CART_LINES)
]
InspectableCartItems = Annotated[list[CartItemInput], Field(max_length=MAX_CART_LINES)]
DietaryTags = Annotated[list[DietaryTag], Field(max_length=MAX_DIETARY_TAGS)]
BrowseOffset = Annotated[int, Field(ge=0)]
BrowseLimit = Annotated[int, Field(ge=1, le=MAX_BROWSE_LIMIT)]


@dataclass(frozen=True)
class AppContext:
    menu_service: MenuService


ServiceContextFactory = Callable[[], AbstractAsyncContextManager[MenuService]]


@asynccontextmanager
async def _database_service_context() -> AsyncIterator[MenuService]:
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL is required for the menu MCP server")
    engine = create_engine(database_url)
    try:
        yield MenuService(MenuRepository(engine))
    finally:
        await engine.dispose()


def create_mcp_server(
    service_context_factory: ServiceContextFactory = _database_service_context,
) -> MCPServer:
    @asynccontextmanager
    async def lifespan(_server: MCPServer) -> AsyncIterator[AppContext]:
        async with service_context_factory() as service:
            yield AppContext(menu_service=service)

    server = MCPServer(
        "restaurant-menu",
        instructions=(
            "Read-only restaurant menu tools. Prices are current PostgreSQL values "
            "in integer minor units; cart inspection has no side effects and "
            "calculate_cart returns subtotal without delivery."
        ),
        lifespan=lifespan,
    )

    def service_from(ctx: Context) -> MenuService:
        app_context = ctx.request_context.lifespan_context
        if not isinstance(app_context, AppContext):
            raise RuntimeError("Menu MCP lifespan is unavailable")
        return app_context.menu_service

    async def execute_tool(awaitable: Any) -> Any:
        try:
            return await awaitable
        except MenuServiceError as exc:
            raise ToolError(exc.public_message) from None
        except ValidationError:
            raise ToolError("Некорректные параметры инструмента меню.") from None
        except Exception as exc:
            logger.error(
                "Menu MCP tool execution failed",
                extra={"exception_type": type(exc).__name__},
            )
            raise ToolError("Меню временно недоступно. Попробуйте позже.") from None

    @server.tool()
    async def search_menu(
        ctx: Context,
        query: SearchQuery | None = None,
        category: CategoryFilter | None = None,
        dietary_tags: DietaryTags | None = None,
    ) -> MenuSearchResult:
        """Search available dishes using current PostgreSQL menu data."""
        return await execute_tool(
            service_from(ctx).search_menu(query, category, dietary_tags)
        )

    @server.tool()
    async def list_categories(ctx: Context) -> CategoryList:
        """List categories containing available dishes with current dish counts."""
        return await execute_tool(service_from(ctx).list_categories())

    @server.tool()
    async def browse_menu(
        ctx: Context,
        category: BrowseCategory,
        offset: BrowseOffset = 0,
        limit: BrowseLimit = DEFAULT_BROWSE_LIMIT,
    ) -> BrowseMenuResult:
        """Page through available dishes in one category using stable ordering."""
        return await execute_tool(
            service_from(ctx).browse_menu(category, offset, limit)
        )

    @server.tool()
    async def get_dish(dish_id: UUID, ctx: Context) -> DishDetail:
        """Return current details, price, dietary data, and availability for one dish."""
        return await execute_tool(service_from(ctx).get_dish(dish_id))

    @server.tool()
    async def calculate_cart(items: CartItems, ctx: Context) -> CartQuote:
        """Validate all cart items and calculate an EUR subtotal without delivery."""
        return await execute_tool(service_from(ctx).calculate_cart(items))

    @server.tool()
    async def inspect_cart(items: InspectableCartItems, ctx: Context) -> CartInspection:
        """Inspect current prices and availability without changing cart state."""
        return await execute_tool(service_from(ctx).inspect_cart(items))

    return server


mcp = create_mcp_server()
