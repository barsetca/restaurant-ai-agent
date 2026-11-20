import asyncio
import os
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models import MenuItem
from app.mcp.client import MenuMcpClient, MenuMcpToolError
from app.menu.contracts import CartItemInput


def _database_url() -> str:
    url = os.getenv("TEST_MCP_DATABASE_URL", "")
    if not url:
        pytest.skip("TEST_MCP_DATABASE_URL is not set")
    database_name = urlparse(
        url.replace("postgresql+asyncpg", "postgresql")
    ).path.lower()
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
        ppid_line = next(
            (line for line in status.splitlines() if line.startswith("PPid:")), ""
        )
        if (
            ppid_line
            and int(ppid_line.split()[1]) == parent_pid
            and b"mcp_server" in command
        ):
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
    marker = uuid4().hex
    catalog_category = f"test-main-{marker}"
    starter_category = f"test-starter-{marker}"
    rows = [
        MenuItem(
            id=pasta_id,
            source_row_id=f"mcp-test-{marker}-pasta",
            name=f"Тестовая паста {marker[:8]}",
            description="Томатная паста для изолированного integration test",
            ingredients=["паста", "томат"],
            allergens=["глютен"],
            dietary_tags=["vegan"],
            category=catalog_category,
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
            category=starter_category,
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
            category=catalog_category,
            price_minor=500,
            currency="EUR",
            available=False,
        ),
    ]
    rows.extend(
        MenuItem(
            id=uuid4(),
            source_row_id=f"mcp-test-{marker}-page-{number:02d}",
            name=f"Тестовое блюдо {number:02d} {marker[:8]}",
            description="Синтетическое блюдо для проверки PostgreSQL pagination",
            ingredients=[],
            allergens=[],
            dietary_tags=[],
            category=catalog_category,
            price_minor=1000 + number,
            currency="EUR",
            available=True,
        )
        for number in range(22)
    )
    synthetic_ids = tuple(row.id for row in rows)

    try:
        async with sessions() as session:
            session.add_all(rows)
            await session.commit()
            before_count = await session.scalar(
                select(func.count()).select_from(MenuItem)
            )

        adapter = MenuMcpClient(database_url)
        async with adapter:
            assert await adapter.list_tool_names() == [
                "browse_menu",
                "calculate_cart",
                "get_dish",
                "inspect_cart",
                "list_categories",
                "search_menu",
            ]
            child_pids = _mcp_child_pids(os.getpid())
            assert child_pids, "stdio MCP subprocess was not observed"

            search = await adapter.search_menu(
                marker[:8], category=catalog_category, dietary_tags=["vegan"]
            )
            assert [item.dish_id for item in search.items] == [pasta_id]

            morphology_search = await adapter.search_menu(
                "тестовые пасты", category=catalog_category
            )
            assert pasta_id in [item.dish_id for item in morphology_search.items]

            browse = await adapter.search_menu()
            assert len(browse.items) <= 20
            assert all(item.currency == "EUR" for item in browse.items)

            categories = await adapter.list_categories()
            by_category = {item.id: item for item in categories.categories}
            assert by_category[catalog_category].name == catalog_category
            assert by_category[catalog_category].available_dish_count == 23
            assert unavailable_id not in {
                item.dish_id
                for item in (
                    await adapter.browse_menu(catalog_category, offset=0, limit=20)
                ).items
            }

            first_page = await adapter.browse_menu(catalog_category, offset=0, limit=20)
            second_page = await adapter.browse_menu(
                catalog_category, offset=20, limit=20
            )
            assert first_page.has_more is True
            assert second_page.has_more is False
            paged_ids = [item.dish_id for item in first_page.items + second_page.items]
            assert len(paged_ids) == len(set(paged_ids)) == 23

            empty_page = await adapter.browse_menu("unknown-category")
            assert empty_page.items == []

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

            missing_id = uuid4()
            inspection = await adapter.inspect_cart(
                [
                    CartItemInput(dish_id=unavailable_id, quantity=2),
                    CartItemInput(dish_id=missing_id, quantity=1),
                    CartItemInput(dish_id=pasta_id, quantity=1),
                ]
            )
            assert [item.dish_id for item in inspection.items] == [
                unavailable_id,
                missing_id,
                pasta_id,
            ]
            assert inspection.items[0].line_total_minor == 1000
            assert inspection.items[0].available is False
            assert inspection.items[1].missing is True
            assert inspection.items[1].unit_price_minor is None
            assert inspection.available_subtotal_minor == 1250
            assert inspection.has_unavailable_items is True

            async with sessions() as session:
                await session.execute(
                    update(MenuItem)
                    .where(MenuItem.id == pasta_id)
                    .values(price_minor=1300)
                )
                await session.commit()
            refreshed = await adapter.inspect_cart(
                [CartItemInput(dish_id=pasta_id, quantity=2)]
            )
            assert refreshed.items[0].unit_price_minor == 1300
            assert refreshed.available_subtotal_minor == 2600

            empty_inspection = await adapter.inspect_cart([])
            assert empty_inspection.items == []
            assert empty_inspection.available_subtotal_minor == 0

        await _wait_for_process_exit(child_pids)
        async with sessions() as session:
            after_count = await session.scalar(
                select(func.count()).select_from(MenuItem)
            )
        assert after_count == before_count
    finally:
        async with sessions() as session:
            await session.execute(
                delete(MenuItem).where(MenuItem.id.in_(synthetic_ids))
            )
            await session.commit()
        await engine.dispose()
