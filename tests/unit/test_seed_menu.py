from collections import Counter
from uuid import uuid5

from scripts.seed_menu import DEV_MENU_ITEMS, DEV_MENU_NAMESPACE, validate_seed_data


def test_development_menu_seed_is_valid_and_has_stable_unique_ids() -> None:
    validate_seed_data()

    assert len(DEV_MENU_ITEMS) == 15
    assert len({item.id for item in DEV_MENU_ITEMS}) == 15
    assert len({item.source_row_id for item in DEV_MENU_ITEMS}) == 15
    assert all(
        item.id == uuid5(DEV_MENU_NAMESPACE, item.source_row_id)
        for item in DEV_MENU_ITEMS
    )
    assert all(item.price_minor > 0 and item.currency == "EUR" for item in DEV_MENU_ITEMS)


def test_development_menu_seed_distribution_and_m4_test_coverage() -> None:
    assert Counter(item.category for item in DEV_MENU_ITEMS) == {
        "Закуски": 3,
        "Салаты": 3,
        "Основные блюда": 5,
        "Десерты": 2,
        "Напитки": 2,
    }
    assert sum(not item.available for item in DEV_MENU_ITEMS) == 1
    assert sum(bool(item.dietary_tags) for item in DEV_MENU_ITEMS) >= 5
    assert sum(bool(item.allergens) for item in DEV_MENU_ITEMS) >= 5
    assert len({item.price_minor for item in DEV_MENU_ITEMS}) >= 10


def test_development_menu_seed_has_explicit_complete_content() -> None:
    for item in DEV_MENU_ITEMS:
        assert item.name.strip()
        assert item.description.strip()
        assert item.ingredients
        assert all(value.strip() for value in item.ingredients)
        assert all(value == value.casefold() for value in item.dietary_tags)
        assert "gluten_free" not in item.dietary_tags
