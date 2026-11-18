import argparse
import asyncio
from dataclasses import asdict, dataclass
import json
import logging
from uuid import UUID, uuid5

from sqlalchemy import func, or_, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.db.models import MenuItem
from app.db.session import create_engine

logger = logging.getLogger(__name__)

DEV_MENU_NAMESPACE = UUID("98de4c59-4663-4bc8-b3b7-1942f26b1855")
DEV_MENU_SOURCE_PREFIX = "dev-menu-"
SEED_ADVISORY_LOCK_KEY = 7_041_503_202_611_18


@dataclass(frozen=True)
class SeedMenuItem:
    source_row_id: str
    name: str
    description: str
    ingredients: tuple[str, ...]
    allergens: tuple[str, ...]
    dietary_tags: tuple[str, ...]
    category: str
    price_minor: int
    available: bool = True
    currency: str = "EUR"

    @property
    def id(self) -> UUID:
        return uuid5(DEV_MENU_NAMESPACE, self.source_row_id)

    def values(self) -> dict[str, object]:
        values = asdict(self)
        values["id"] = self.id
        values["ingredients"] = list(self.ingredients)
        values["allergens"] = list(self.allergens)
        values["dietary_tags"] = list(self.dietary_tags)
        return values


DEV_MENU_ITEMS = (
    SeedMenuItem(
        source_row_id="dev-menu-001",
        name="Брускетта с томатами",
        description="Поджаренная чиабатта с томатами, базиликом и оливковым маслом.",
        ingredients=("чиабатта", "томаты", "базилик", "чеснок", "оливковое масло"),
        allergens=("глютен",),
        dietary_tags=("vegan",),
        category="Закуски",
        price_minor=750,
    ),
    SeedMenuItem(
        source_row_id="dev-menu-002",
        name="Запечённый камамбер",
        description="Камамбер с розмарином, грушей и тостами из багета.",
        ingredients=("камамбер", "груша", "розмарин", "багет"),
        allergens=("молоко", "глютен"),
        dietary_tags=("vegetarian",),
        category="Закуски",
        price_minor=1050,
    ),
    SeedMenuItem(
        source_row_id="dev-menu-003",
        name="Креветки в чесночном масле",
        description="Тигровые креветки с чесноком, петрушкой и сливочным маслом.",
        ingredients=("креветки", "чеснок", "петрушка", "сливочное масло"),
        allergens=("ракообразные", "молоко"),
        dietary_tags=(),
        category="Закуски",
        price_minor=1250,
    ),
    SeedMenuItem(
        source_row_id="dev-menu-004",
        name="Греческий салат",
        description="Томаты, огурцы, сладкий перец, оливки и сыр фета.",
        ingredients=("томаты", "огурцы", "сладкий перец", "оливки", "фета", "красный лук"),
        allergens=("молоко",),
        dietary_tags=("vegetarian",),
        category="Салаты",
        price_minor=950,
    ),
    SeedMenuItem(
        source_row_id="dev-menu-005",
        name="Тёплый салат с курицей",
        description="Куриное филе, салатные листья, запечённый картофель и горчичная заправка.",
        ingredients=("куриное филе", "салатные листья", "картофель", "томаты", "горчичная заправка"),
        allergens=("горчица",),
        dietary_tags=(),
        category="Салаты",
        price_minor=1150,
    ),
    SeedMenuItem(
        source_row_id="dev-menu-006",
        name="Салат с печёной свёклой",
        description="Печёная свёкла, руккола, апельсин и грецкий орех с цитрусовой заправкой.",
        ingredients=("свёкла", "руккола", "апельсин", "грецкий орех", "оливковое масло"),
        allergens=("орехи",),
        dietary_tags=("vegan",),
        category="Салаты",
        price_minor=900,
    ),
    SeedMenuItem(
        source_row_id="dev-menu-007",
        name="Бефстроганов с картофельным пюре",
        description="Говядина в сливочно-грибном соусе с картофельным пюре.",
        ingredients=(
            "говядина",
            "шампиньоны",
            "сливки",
            "лук",
            "пшеничная мука",
            "картофель",
            "сливочное масло",
        ),
        allergens=("молоко", "глютен"),
        dietary_tags=(),
        category="Основные блюда",
        price_minor=1850,
    ),
    SeedMenuItem(
        source_row_id="dev-menu-008",
        name="Лосось с овощами",
        description="Филе лосося с сезонными овощами и лимонно-сливочным соусом.",
        ingredients=("лосось", "кабачок", "морковь", "брокколи", "лимон", "сливки"),
        allergens=("рыба", "молоко"),
        dietary_tags=(),
        category="Основные блюда",
        price_minor=2100,
    ),
    SeedMenuItem(
        source_row_id="dev-menu-009",
        name="Ризотто с лесными грибами",
        description="Рис арборио с лесными грибами, пармезаном и тимьяном.",
        ingredients=("рис арборио", "лесные грибы", "пармезан", "сливочное масло", "тимьян"),
        allergens=("молоко",),
        dietary_tags=("vegetarian",),
        category="Основные блюда",
        price_minor=1550,
    ),
    SeedMenuItem(
        source_row_id="dev-menu-010",
        name="Рататуй с кускусом",
        description="Тушёные баклажаны, кабачки, перец и томаты с рассыпчатым кускусом.",
        ingredients=("баклажан", "кабачок", "сладкий перец", "томаты", "кускус", "оливковое масло"),
        allergens=("глютен",),
        dietary_tags=("vegan",),
        category="Основные блюда",
        price_minor=1350,
    ),
    SeedMenuItem(
        source_row_id="dev-menu-011",
        name="Утиная ножка с апельсиновым соусом",
        description="Томлёная утиная ножка с апельсиновым соусом и картофельным гратеном.",
        ingredients=("утиная ножка", "апельсин", "картофель", "сливки", "тимьян"),
        allergens=("молоко",),
        dietary_tags=(),
        category="Основные блюда",
        price_minor=2250,
        available=False,
    ),
    SeedMenuItem(
        source_row_id="dev-menu-012",
        name="Тирамису",
        description="Классический десерт с маскарпоне, кофе и какао.",
        ingredients=("маскарпоне", "савоярди", "яйца", "кофе", "какао"),
        allergens=("молоко", "яйца", "глютен"),
        dietary_tags=("vegetarian",),
        category="Десерты",
        price_minor=750,
    ),
    SeedMenuItem(
        source_row_id="dev-menu-013",
        name="Панна-котта с ягодами",
        description="Сливочная панна-котта с соусом из лесных ягод.",
        ingredients=("сливки", "ваниль", "желатин", "лесные ягоды"),
        allergens=("молоко",),
        dietary_tags=(),
        category="Десерты",
        price_minor=700,
    ),
    SeedMenuItem(
        source_row_id="dev-menu-014",
        name="Домашний лимонад",
        description="Лимонад из лимона, апельсина и свежей мяты, 400 мл.",
        ingredients=("лимон", "апельсин", "мята", "сахар", "газированная вода"),
        allergens=(),
        dietary_tags=("vegan", "alcohol_free"),
        category="Напитки",
        price_minor=450,
    ),
    SeedMenuItem(
        source_row_id="dev-menu-015",
        name="Эспрессо",
        description="Двойной эспрессо из свежемолотой арабики.",
        ingredients=("кофе арабика", "вода"),
        allergens=(),
        dietary_tags=("vegan", "alcohol_free"),
        category="Напитки",
        price_minor=300,
    ),
)


class SeedConflictError(RuntimeError):
    pass


def validate_seed_data(items: tuple[SeedMenuItem, ...] = DEV_MENU_ITEMS) -> None:
    if len(items) != 15:
        raise ValueError("development menu seed must contain exactly 15 items")
    if len({item.id for item in items}) != len(items):
        raise ValueError("development menu seed IDs must be unique")
    if len({item.source_row_id for item in items}) != len(items):
        raise ValueError("development menu source IDs must be unique")
    for item in items:
        if not item.source_row_id.startswith(DEV_MENU_SOURCE_PREFIX):
            raise ValueError("development menu source ID is outside the owned namespace")
        if not item.name or not item.description or not item.category or not item.ingredients:
            raise ValueError("development menu required text fields must be populated")
        if item.price_minor <= 0 or item.currency != "EUR":
            raise ValueError("development menu prices must be positive EUR minor units")


async def seed_menu(engine: AsyncEngine) -> dict[str, int | str]:
    validate_seed_data()
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    item_ids = tuple(item.id for item in DEV_MENU_ITEMS)
    source_ids = tuple(item.source_row_id for item in DEV_MENU_ITEMS)
    expected_pairs = {(item.id, item.source_row_id) for item in DEV_MENU_ITEMS}

    async with sessions() as session, session.begin():
        await session.execute(
            text("SELECT pg_advisory_xact_lock(:key)"),
            {"key": SEED_ADVISORY_LOCK_KEY},
        )
        existing_result = await session.execute(
            select(MenuItem.id, MenuItem.source_row_id).where(
                or_(MenuItem.id.in_(item_ids), MenuItem.source_row_id.in_(source_ids))
            )
        )
        existing_pairs = set(existing_result.tuples().all())
        if not existing_pairs.issubset(expected_pairs):
            raise SeedConflictError(
                "Development menu seed ownership conflict; no records were changed."
            )

        existing_ids = {item_id for item_id, _source_id in existing_pairs}
        for item in DEV_MENU_ITEMS:
            values = item.values()
            update_values = {key: value for key, value in values.items() if key != "id"}
            statement = insert(MenuItem).values(**values).on_conflict_do_update(
                index_elements=[MenuItem.id],
                set_=update_values,
            )
            await session.execute(statement)

        total = await session.scalar(
            select(func.count()).select_from(MenuItem).where(MenuItem.id.in_(item_ids))
        )
        if total != len(DEV_MENU_ITEMS):
            raise RuntimeError("Development menu seed verification failed.")

    return {
        "status": "ok",
        "dataset": "development_menu",
        "created": len(DEV_MENU_ITEMS) - len(existing_ids),
        "updated": len(existing_ids),
        "total": len(DEV_MENU_ITEMS),
    }


async def _run() -> int:
    settings = get_settings()
    configure_logging(settings.log_level)
    engine = create_engine(settings.database_url)
    try:
        result = await seed_menu(engine)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except SeedConflictError as exc:
        logger.error("Development menu seed ownership conflict")
        print(json.dumps({"status": "error", "message": str(exc)}, ensure_ascii=False))
        return 1
    except Exception:
        logger.error("Development menu seed failed")
        print(
            json.dumps(
                {
                    "status": "error",
                    "message": "Development menu seed failed; check database readiness.",
                },
                ensure_ascii=False,
            )
        )
        return 1
    finally:
        await engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Idempotently seed the configured development database with 15 menu items"
    )
    parser.parse_args()
    return asyncio.run(_run())


if __name__ == "__main__":
    raise SystemExit(main())
