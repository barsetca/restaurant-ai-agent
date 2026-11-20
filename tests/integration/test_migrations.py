import asyncio
import os
import subprocess
import sys
from urllib.parse import urlparse

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import create_async_engine

from app.db import models  # noqa: F401
from app.db.base import Base


EXPECTED_TABLES = {
    "menu_items",
    "restaurant_settings",
    "orders",
    "order_items",
    "conversation_state",
    "faq_sync",
    "unanswered_questions",
    "calendar_jobs",
}
CHECKPOINT_TABLES = {
    "checkpoint_migrations",
    "checkpoints",
    "checkpoint_blobs",
    "checkpoint_writes",
}


def _test_database_url() -> str:
    url = os.getenv("TEST_DATABASE_URL", "")
    if not url:
        pytest.skip("TEST_DATABASE_URL is not set")
    database_name = urlparse(
        url.replace("postgresql+asyncpg", "postgresql")
    ).path.lower()
    if "test" not in database_name:
        pytest.fail(
            "TEST_DATABASE_URL must point to a database whose name contains 'test'"
        )
    return url


def _alembic(url: str, revision: str) -> subprocess.CompletedProcess[str]:
    environment = {**os.environ, "DATABASE_URL": url}
    return subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", revision],
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )


@pytest.mark.integration
def test_clean_and_repeated_alembic_upgrade() -> None:
    url = _test_database_url()
    # The dedicated test database must be provisioned empty by the caller.
    first = _alembic(url, "head")
    assert first.returncode == 0, first.stderr
    repeated = _alembic(url, "head")
    assert repeated.returncode == 0, repeated.stderr


async def _inspect_schema(url: str) -> None:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as connection:

            def inspect_sync(sync_connection) -> None:
                inspector = inspect(sync_connection)
                all_tables = set(inspector.get_table_names())
                checkpoint_tables = all_tables & CHECKPOINT_TABLES
                assert checkpoint_tables in (set(), CHECKPOINT_TABLES)
                tables = all_tables - {"alembic_version"} - CHECKPOINT_TABLES
                assert tables == EXPECTED_TABLES

                for table in EXPECTED_TABLES:
                    assert inspector.get_pk_constraint(table)["constrained_columns"]

                order_item_fks = {
                    (tuple(fk["constrained_columns"]), fk["referred_table"])
                    for fk in inspector.get_foreign_keys("order_items")
                }
                assert order_item_fks == {
                    (("menu_item_id",), "menu_items"),
                    (("order_id",), "orders"),
                }
                calendar_fks = inspector.get_foreign_keys("calendar_jobs")
                assert len(calendar_fks) == 1
                assert calendar_fks[0]["referred_table"] == "orders"

                order_unique = {
                    tuple(item["column_names"])
                    for item in inspector.get_unique_constraints("orders")
                }
                menu_unique = {
                    tuple(item["column_names"])
                    for item in inspector.get_unique_constraints("menu_items")
                }
                assert ("confirmation_key",) in order_unique
                assert ("source_row_id",) in menu_unique

                check_names = {
                    check["name"]
                    for table in (
                        "menu_items",
                        "orders",
                        "order_items",
                        "calendar_jobs",
                    )
                    for check in inspector.get_check_constraints(table)
                }
                assert {
                    "ck_menu_items_price_nonnegative",
                    "ck_orders_subtotal_nonnegative",
                    "ck_orders_delivery_fee_nonnegative",
                    "ck_orders_total_nonnegative",
                    "ck_order_items_quantity_positive",
                    "ck_order_items_unit_price_nonnegative",
                    "ck_order_items_line_total_nonnegative",
                    "ck_calendar_jobs_attempt_count_nonnegative",
                } <= check_names

                index_names = {
                    index["name"]
                    for table in EXPECTED_TABLES
                    for index in inspector.get_indexes(table)
                }
                assert {
                    "ix_orders_telegram_user_id",
                    "ix_orders_status",
                    "ix_order_items_order_id",
                    "ix_order_items_menu_item_id",
                    "ix_conversation_state_user_id",
                    "ix_unanswered_questions_created_at",
                    "ix_calendar_jobs_order_id",
                    "ix_calendar_jobs_status_next_attempt",
                } <= index_names

                def include_name(name, type_, parent_names):
                    del parent_names
                    return not (type_ == "table" and name in CHECKPOINT_TABLES)

                context = MigrationContext.configure(
                    sync_connection,
                    opts={"include_name": include_name},
                )
                assert compare_metadata(context, Base.metadata) == []

            await connection.run_sync(inspect_sync)
    finally:
        await engine.dispose()


@pytest.mark.integration
def test_schema_constraints_indexes_and_orm_match() -> None:
    asyncio.run(_inspect_schema(_test_database_url()))
