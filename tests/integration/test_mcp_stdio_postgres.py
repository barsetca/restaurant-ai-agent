import asyncio
import os
from pathlib import Path
from urllib.parse import urlparse
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models import MenuItem
from app.mcp.client import MenuMcpClient, MenuMcpToolError
from app.menu.contracts import CartItemInput


def _database_url() -> str:
    url = os.getenv("TEST_MCP_DATABASE_URL", "")
    if not url:
        pytest.skip("TEST_MCP_DATABASE_URL is not set")
    database_name = urlparse(url.replace("postgresql+asyncpg", "postgresql")).path.lower()
    if "test" not in database_name:
        pytest.fail("TEST_MCP_DATABASE_URL must use a database containing 'test'")
    return url


def _mcp_child_pids(parent_pid: int) -> set[int]:
    matches: set[int] = set()
    for proc_dir in Path("/proc").glob("[0-9]*"):
        try:
            status = (proc_dir / "status").read_text(encoding="utf-8")
            command = (proc_dir / "cmdline").read_bytes().replace(b"\0", b" ")
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        ppid_line = next((line for line in status.splitlines() if line.startswith("PPid:")), "")
        if ppid_line and int(ppid_line.split()[1]) == parent_pid and b"mcp_server" in command:
            matches.add(int(proc_dir.name))
    return matches


async def _wait_for_process_exit(pids: set[int]) -> None:
    for _ in range(50):
        if all(not Path(f"/proc/{pid}").exists() for pid in pids):
            return
        await asyncio.sleep(0.02)
    assert all(not Path(f"/proc/{pid}").exists() for pid in pids)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_real_stdio_tools_read_postgres_and_process_exits() -> None:
    database_url = _database_url()
    engine = create_async_engine(database_url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    pasta_id, soup_id, unavailable_id = uuid4(), uuid4(), uuid4()
    synthetic_ids = (pasta_id, soup_id, unavailable_id)
    marker = uuid4().hex
    rows = [
        MenuItem(
            id=pasta_id,
            source_row_id=f"mcp-test-{marker}-pasta",
            name=f"Тестовая паста {marker[:8]}",
            description="Томатная паста для изолированного integration test",
            ingredients=["паста", "томат"],
            allergens=["глютен"],
            dietary_tags=["vegan"],
            category="test-main",
            price_minor=1250,
            currency="EUR",
            available=True,
        ),
        MenuItem(
            id=soup_id,
            source_row_id=f"mcp-test-{marker}-soup",
            name=f"Тестовый суп {marker[:8]}",
            description="Суп для изолированного integration test",
            ingredients=["овощи"],
            allergens=[],
            dietary_tags=["vegan", "gluten-free"],
            category="test-starter",
            price_minor=700,
            currency="EUR",
            available=True,
        ),
        MenuItem(
            id=unavailable_id,
            source_row_id=f"mcp-test-{marker}-off",
            name=f"Недоступное блюдо {marker[:8]}",
            description=None,
            ingredients=[],
            allergens=[],
            dietary_tags=[],
            category="test-main",
            price_minor=500,
            currency="EUR",
            available=False,
        ),
    ]

    try:
        async with sessions() as session:
            session.add_all(rows)
            await session.commit()
            before_count = await session.scalar(select(func.count()).select_from(MenuItem))

        adapter = MenuMcpClient(database_url)
        async with adapter:
            assert await adapter.list_tool_names() == [
                "calculate_cart",
                "get_dish",
                "search_menu",
            ]
            child_pids = _mcp_child_pids(os.getpid())
            assert child_pids, "stdio MCP subprocess was not observed"

            search = await adapter.search_menu(
                marker[:8], category="test-main", dietary_tags=["vegan"]
            )
            assert [item.dish_id for item in search.items] == [pasta_id]

            detail = await adapter.get_dish(unavailable_id)
            assert detail.available is False
            assert detail.price_minor == 500

            quote = await adapter.calculate_cart(
                [
                    CartItemInput(dish_id=pasta_id, quantity=2),
                    CartItemInput(dish_id=soup_id, quantity=1),
                    CartItemInput(dish_id=pasta_id, quantity=1),
                ]
            )
            assert quote.subtotal_minor == 4450
            assert [line.quantity for line in quote.items] == [3, 1]

            with pytest.raises(MenuMcpToolError):
                await adapter.calculate_cart(
                    [CartItemInput(dish_id=unavailable_id, quantity=1)]
                )

        await _wait_for_process_exit(child_pids)
        async with sessions() as session:
            after_count = await session.scalar(select(func.count()).select_from(MenuItem))
        assert after_count == before_count
    finally:
        async with sessions() as session:
            await session.execute(delete(MenuItem).where(MenuItem.id.in_(synthetic_ids)))
            await session.commit()
        await engine.dispose()
