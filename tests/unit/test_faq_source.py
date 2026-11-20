import json

import pytest

from app.rag.sheets_source import (
    FaqSourceValidationError,
    GoogleSheetsFaqSource,
    JsonFaqSource,
    parse_faq_rows,
)


def _row(**overrides):
    row = {
        "id": "hours",
        "category": "hours",
        "question": "Когда вы закрываетесь?",
        "answer": "Мы закрываемся в 22:00.",
        "updated_at": "2026-11-17",
        "active": True,
    }
    row.update(overrides)
    return row


def test_parse_filters_inactive_and_empty_rows() -> None:
    result = parse_faq_rows([_row(), _row(id="old", active=False), {}])
    assert [entry.source_id for entry in result.entries] == ["hours"]
    assert result.inactive == 1
    assert result.skipped_empty == 1


@pytest.mark.parametrize(
    "row",
    [
        _row(question=""),
        _row(answer="Напишите guest@example.com"),
        _row(answer="Позвоните +34 600 123 456"),
    ],
)
def test_invalid_or_personal_rows_abort_sync(row) -> None:
    with pytest.raises(FaqSourceValidationError):
        parse_faq_rows([row])


def test_content_hash_tracks_content_not_updated_at() -> None:
    first = parse_faq_rows([_row(updated_at="one")]).entries[0]
    same = parse_faq_rows([_row(updated_at="two")]).entries[0]
    changed = parse_faq_rows([_row(answer="До 23:00")]).entries[0]
    assert first.content_hash == same.content_hash
    assert first.content_hash != changed.content_hash


async def test_json_fixture_source(tmp_path) -> None:
    fixture = tmp_path / "faq.json"
    fixture.write_text(json.dumps([_row()]), encoding="utf-8")
    result = await JsonFaqSource(fixture).load()
    assert result.entries[0].source_id == "hours"


def test_google_source_uses_read_only_scope_and_expected_headers(monkeypatch) -> None:
    calls = {}

    class Worksheet:
        def get_all_records(self, **kwargs):
            calls["records"] = kwargs
            return [_row()]

    class Spreadsheet:
        def worksheet(self, name):
            calls["worksheet"] = name
            return Worksheet()

    class Client:
        def open_by_key(self, spreadsheet_id):
            calls["spreadsheet_id"] = spreadsheet_id
            return Spreadsheet()

    def service_account(**kwargs):
        calls["auth"] = kwargs
        return Client()

    monkeypatch.setattr(
        "app.rag.sheets_source.gspread.service_account", service_account
    )
    source = GoogleSheetsFaqSource("sheet-id", "/run/secrets/google.json")
    result = source._load_sync()

    assert result.entries[0].source_id == "hours"
    assert calls["auth"]["scopes"] == [
        "https://www.googleapis.com/auth/spreadsheets.readonly"
    ]
    assert calls["spreadsheet_id"] == "sheet-id"
    assert calls["worksheet"] == "FAQ"
    assert calls["records"]["expected_headers"] == [
        "id",
        "category",
        "question",
        "answer",
        "updated_at",
        "active",
    ]
