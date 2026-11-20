import asyncio
import os
from urllib.parse import urlparse
from uuid import uuid4

import pytest
from psycopg import AsyncConnection

from app.agent.checkpoint import (
    checkpoint_connection_uri,
    open_checkpoint_saver,
    setup_checkpoint_schema,
)
from app.agent.graph import build_agent_graph
from app.agent.service import AgentService

CHECKPOINT_TABLES = {
    "checkpoint_migrations",
    "checkpoints",
    "checkpoint_blobs",
    "checkpoint_writes",
}


def _database_url() -> str:
    url = os.getenv("TEST_CHECKPOINT_DATABASE_URL", "")
    if not url:
        pytest.skip("TEST_CHECKPOINT_DATABASE_URL is not set")
    database_name = urlparse(
        url.replace("postgresql+asyncpg", "postgresql")
    ).path.lower()
    if "test" not in database_name:
        pytest.fail(
            "TEST_CHECKPOINT_DATABASE_URL must use a database containing 'test'"
        )
    return url


@pytest.mark.integration
async def test_official_postgres_checkpoint_persists_isolates_and_recovers() -> None:
    database_url = _database_url()
    thread_a = f"checkpoint-test:{uuid4().hex}"
    thread_b = f"checkpoint-test:{uuid4().hex}"

    await setup_checkpoint_schema(database_url)
    await setup_checkpoint_schema(database_url)

    dsn = checkpoint_connection_uri(database_url)
    async with await AsyncConnection.connect(dsn) as connection:
        result = await connection.execute(
            "SELECT tablename FROM pg_tables "
            "WHERE schemaname = 'public' AND tablename = ANY(%s)",
            (list(CHECKPOINT_TABLES),),
        )
        assert {row[0] for row in await result.fetchall()} == CHECKPOINT_TABLES

    first_assistant = type("Assistant", (), {})()

    async def first_answer(question: str) -> str:
        return f"FAQ:{question}"

    first_assistant.answer = first_answer

    async with open_checkpoint_saver(database_url) as first_saver:
        first_graph = build_agent_graph(first_assistant, checkpointer=first_saver)
        first_service = AgentService(first_graph, persistence_required=True)
        assert await first_service.handle_message("Первый", thread_a) == "FAQ:Первый"
        assert await first_service.handle_message("Второй", thread_a) == "FAQ:Второй"
        assert await first_service.handle_message("Другой", thread_b) == "FAQ:Другой"

        state_a = await first_graph.aget_state(
            {"configurable": {"thread_id": thread_a}}
        )
        state_b = await first_graph.aget_state(
            {"configurable": {"thread_id": thread_b}}
        )
        assert state_a.values["turn_count"] == 2
        assert state_b.values["turn_count"] == 1

    second_assistant = type("Assistant", (), {})()

    async def second_answer(question: str) -> str:
        return f"RESTORED:{question}"

    second_assistant.answer = second_answer

    async with open_checkpoint_saver(database_url) as second_saver:
        second_graph = build_agent_graph(second_assistant, checkpointer=second_saver)
        second_service = AgentService(second_graph, persistence_required=True)
        try:
            assert await second_service.handle_message("После restart", thread_a) == (
                "RESTORED:После restart"
            )
            restored = await second_graph.aget_state(
                {"configurable": {"thread_id": thread_a}}
            )
            isolated = await second_graph.aget_state(
                {"configurable": {"thread_id": thread_b}}
            )
            assert restored.values["turn_count"] == 3
            assert restored.values["user_message"] == "После restart"
            assert isolated.values["turn_count"] == 1

            await asyncio.gather(
                second_service.handle_message("Параллельный один", thread_a),
                second_service.handle_message("Параллельный два", thread_a),
            )
            after_concurrent = await second_graph.aget_state(
                {"configurable": {"thread_id": thread_a}}
            )
            assert after_concurrent.values["turn_count"] == 5
        finally:
            await second_saver.adelete_thread(thread_a)
            await second_saver.adelete_thread(thread_b)
