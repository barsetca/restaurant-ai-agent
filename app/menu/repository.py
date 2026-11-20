from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from uuid import UUID

from sqlalchemy import case, func, literal_column, or_, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.db.models import MenuItem
from app.menu.contracts import BrowseMenuRequest, MAX_SEARCH_RESULTS, SearchMenuRequest


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class MenuRepository:
    def __init__(self, engine: AsyncEngine) -> None:
        self._session_factory = async_sessionmaker(engine, expire_on_commit=False)

    @asynccontextmanager
    async def _read_session(self) -> AsyncIterator[AsyncSession]:
        async with self._session_factory() as session:
            async with session.begin():
                await session.execute(text("SET TRANSACTION READ ONLY"))
                yield session

    async def search(self, request: SearchMenuRequest) -> Sequence[MenuItem]:
        name_lower = func.lower(MenuItem.name)
        category_lower = func.lower(MenuItem.category)
        statement = select(MenuItem).where(MenuItem.available.is_(True))
        if request.query is not None:
            escaped = _escape_like(request.query.casefold())
            contains = f"%{escaped}%"
            starts = f"{escaped}%"
            description_lower = func.lower(func.coalesce(MenuItem.description, ""))
            ingredients_lower = func.lower(
                func.array_to_string(MenuItem.ingredients, " ")
            )
            russian = literal_column("'russian'::regconfig")
            searchable = func.concat_ws(
                " ",
                MenuItem.name,
                func.coalesce(MenuItem.description, ""),
                func.array_to_string(MenuItem.ingredients, " "),
                MenuItem.category,
            )
            search_vector = func.to_tsvector(russian, searchable)
            search_query = func.plainto_tsquery(russian, request.query)
            full_text_match = search_vector.op("@@")(search_query)
            statement = statement.where(
                or_(
                    name_lower.like(contains, escape="\\"),
                    description_lower.like(contains, escape="\\"),
                    ingredients_lower.like(contains, escape="\\"),
                    category_lower.like(contains, escape="\\"),
                    full_text_match,
                )
            ).order_by(
                case(
                    (name_lower == request.query.casefold(), 0),
                    (name_lower.like(starts, escape="\\"), 1),
                    else_=2,
                ),
                func.ts_rank_cd(search_vector, search_query).desc(),
                name_lower,
                MenuItem.id,
            )
        else:
            statement = statement.order_by(name_lower, MenuItem.id)
        statement = statement.limit(MAX_SEARCH_RESULTS)
        if request.category is not None:
            statement = statement.where(category_lower == request.category.casefold())
        if request.dietary_tags:
            statement = statement.where(
                MenuItem.dietary_tags.contains(request.dietary_tags)
            )

        async with self._read_session() as session:
            result = await session.execute(statement)
            return tuple(result.scalars().all())

    async def list_categories(self) -> Sequence[tuple[str, int]]:
        statement = (
            select(MenuItem.category, func.count(MenuItem.id))
            .where(MenuItem.available.is_(True))
            .group_by(MenuItem.category)
            .order_by(func.lower(MenuItem.category), MenuItem.category)
        )
        async with self._read_session() as session:
            result = await session.execute(statement)
            return tuple((category, count) for category, count in result.all())

    async def browse(self, request: BrowseMenuRequest) -> Sequence[MenuItem]:
        if request.category is None:
            return ()
        statement = (
            select(MenuItem)
            .where(
                MenuItem.available.is_(True),
                func.lower(MenuItem.category) == request.category.casefold(),
            )
            .order_by(func.lower(MenuItem.name), MenuItem.id)
            .offset(request.offset)
            .limit(request.limit + 1)
        )
        async with self._read_session() as session:
            result = await session.execute(statement)
            return tuple(result.scalars().all())

    async def get(self, dish_id: UUID) -> MenuItem | None:
        async with self._read_session() as session:
            result = await session.execute(
                select(MenuItem).where(MenuItem.id == dish_id)
            )
            return result.scalar_one_or_none()

    async def get_many(self, dish_ids: Sequence[UUID]) -> Sequence[MenuItem]:
        async with self._read_session() as session:
            result = await session.execute(
                select(MenuItem).where(MenuItem.id.in_(dish_ids))
            )
            return tuple(result.scalars().all())
