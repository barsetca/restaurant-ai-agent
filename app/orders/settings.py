from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.db.models import RestaurantSetting

DELIVERY_FEE_KEY = "delivery_fee_minor"


class OrderSettingsError(RuntimeError):
    public_message = "Оформление заказа временно недоступно. Попробуйте позже."


class OrderSettingsRepository:
    def __init__(self, engine: AsyncEngine) -> None:
        self._session_factory = async_sessionmaker(engine, expire_on_commit=False)

    @asynccontextmanager
    async def _read_session(self) -> AsyncIterator[AsyncSession]:
        async with self._session_factory() as session:
            async with session.begin():
                await session.execute(text("SET TRANSACTION READ ONLY"))
                yield session

    async def get_delivery_fee_minor(self) -> int:
        async with self._read_session() as session:
            value = await session.scalar(
                select(RestaurantSetting.value).where(RestaurantSetting.key == DELIVERY_FEE_KEY)
            )
        try:
            fee = int(value) if value is not None else -1
        except (TypeError, ValueError):
            fee = -1
        if fee < 0 or str(fee) != value:
            raise OrderSettingsError
        return fee
