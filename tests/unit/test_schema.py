from app.db.base import Base
from app.db import models  # noqa: F401


def test_expected_m1_tables_are_registered() -> None:
    assert set(Base.metadata.tables) == {
        "menu_items",
        "restaurant_settings",
        "orders",
        "order_items",
        "conversation_state",
        "faq_sync",
        "unanswered_questions",
        "calendar_jobs",
    }


def test_menu_items_include_explicit_dietary_tags() -> None:
    column = Base.metadata.tables["menu_items"].c.dietary_tags
    assert column.nullable is False
    assert column.server_default is not None
