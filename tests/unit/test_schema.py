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
