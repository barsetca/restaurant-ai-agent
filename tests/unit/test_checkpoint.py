from unittest.mock import AsyncMock

from app.agent.checkpoint import checkpoint_connection_uri, checkpoint_is_ready


def test_checkpoint_connection_uri_converts_asyncpg_without_losing_components() -> None:
    uri = checkpoint_connection_uri(
        "postgresql+asyncpg://user:synthetic@db.example:5432/restaurant?sslmode=disable"
    )
    assert uri == (
        "postgresql://user:synthetic@db.example:5432/restaurant?sslmode=disable"
    )


async def test_checkpoint_readiness_uses_non_user_probe() -> None:
    checkpointer = AsyncMock()
    checkpointer.aget_tuple.return_value = None

    assert await checkpoint_is_ready(checkpointer) is True
    config = checkpointer.aget_tuple.await_args.args[0]
    assert config == {"configurable": {"thread_id": "system:checkpoint-readiness"}}


async def test_checkpoint_readiness_fails_closed() -> None:
    checkpointer = AsyncMock()
    checkpointer.aget_tuple.side_effect = ConnectionError("synthetic")
    assert await checkpoint_is_ready(checkpointer) is False
