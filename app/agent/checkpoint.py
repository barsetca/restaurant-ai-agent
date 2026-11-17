import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from sqlalchemy.engine import make_url

logger = logging.getLogger(__name__)

_READINESS_THREAD_ID = "system:checkpoint-readiness"
_SCHEMA_SETUP_LOCK_ID = 7_346_871_223_914_032_117


def checkpoint_connection_uri(database_url: str) -> str:
    """Convert the SQLAlchemy asyncpg URL into a Psycopg connection URI."""

    url = make_url(database_url)
    if url.get_backend_name() != "postgresql":
        raise ValueError("Checkpoint persistence requires PostgreSQL")
    return url.set(drivername="postgresql").render_as_string(hide_password=False)


def _strict_serializer() -> JsonPlusSerializer:
    # State is intentionally limited to JSON primitives. Disallow importing
    # arbitrary Python modules when deserializing database-controlled msgpack.
    return JsonPlusSerializer(allowed_msgpack_modules=())


@asynccontextmanager
async def open_checkpoint_saver(
    database_url: str,
    *,
    pool_max_size: int = 10,
) -> AsyncIterator[AsyncPostgresSaver]:
    pool = AsyncConnectionPool(
        checkpoint_connection_uri(database_url),
        min_size=1,
        max_size=pool_max_size,
        kwargs={
            "autocommit": True,
            "prepare_threshold": 0,
            "row_factory": dict_row,
        },
        open=False,
    )
    await pool.open()
    try:
        await pool.wait()
        yield AsyncPostgresSaver(pool, serde=_strict_serializer())
    finally:
        await pool.close()


async def checkpoint_is_ready(checkpointer: AsyncPostgresSaver) -> bool:
    try:
        await checkpointer.aget_tuple(
            {"configurable": {"thread_id": _READINESS_THREAD_ID}}
        )
        return True
    except Exception as exc:
        logger.warning(
            "LangGraph checkpoint readiness check failed",
            extra={"exception_type": type(exc).__name__},
        )
        return False


async def setup_checkpoint_schema(database_url: str) -> None:
    """Run official saver migrations under a process-independent advisory lock."""

    async with AsyncPostgresSaver.from_conn_string(
        checkpoint_connection_uri(database_url),
        serde=_strict_serializer(),
    ) as checkpointer:
        connection = checkpointer.conn
        await connection.execute(
            "SELECT pg_advisory_lock(%s)",
            (_SCHEMA_SETUP_LOCK_ID,),
        )
        try:
            await checkpointer.setup()
        finally:
            await connection.execute(
                "SELECT pg_advisory_unlock(%s)",
                (_SCHEMA_SETUP_LOCK_ID,),
            )
