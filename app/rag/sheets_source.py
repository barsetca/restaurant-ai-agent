import asyncio
import json
from pathlib import Path
from typing import Any, Protocol

import gspread
from pydantic import ValidationError

from app.rag.schemas import FaqEntry, FaqSourceResult

EXPECTED_HEADERS = ("id", "category", "question", "answer", "updated_at", "active")


class FaqSourceError(RuntimeError):
    pass


class FaqSourceValidationError(FaqSourceError):
    pass


class FaqSource(Protocol):
    async def load(self) -> FaqSourceResult: ...


def _is_active(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y", "да"}


def parse_faq_rows(rows: list[dict[str, Any]]) -> FaqSourceResult:
    entries: list[FaqEntry] = []
    seen_ids: set[str] = set()
    inactive = 0
    skipped_empty = 0
    errors: list[str] = []

    for row_number, row in enumerate(rows, start=2):
        if not any(str(row.get(header, "")).strip() for header in EXPECTED_HEADERS):
            skipped_empty += 1
            continue
        if not _is_active(row.get("active", True)):
            inactive += 1
            continue
        try:
            entry = FaqEntry(
                source_id=str(row.get("id", "")),
                category=str(row.get("category", "")),
                question=str(row.get("question", "")),
                answer=str(row.get("answer", "")),
                updated_at=str(row.get("updated_at", "")).strip() or None,
            )
        except ValidationError as exc:
            errors.append(f"row {row_number}: {exc.errors()[0]['msg']}")
            continue
        if entry.source_id in seen_ids:
            errors.append(f"row {row_number}: duplicate id")
            continue
        seen_ids.add(entry.source_id)
        entries.append(entry)

    if errors:
        detail = "; ".join(errors[:10])
        raise FaqSourceValidationError(f"FAQ validation failed: {detail}")
    return FaqSourceResult(entries=entries, inactive=inactive, skipped_empty=skipped_empty)


class GoogleSheetsFaqSource:
    def __init__(self, spreadsheet_id: str, credentials_path: str, worksheet_name: str = "FAQ") -> None:
        self._spreadsheet_id = spreadsheet_id
        self._credentials_path = credentials_path
        self._worksheet_name = worksheet_name

    def _load_sync(self) -> FaqSourceResult:
        try:
            client = gspread.service_account(
                filename=self._credentials_path,
                scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"],
            )
            worksheet = client.open_by_key(self._spreadsheet_id).worksheet(self._worksheet_name)
            rows = worksheet.get_all_records(expected_headers=list(EXPECTED_HEADERS), numericise_ignore=["all"])
            return parse_faq_rows(rows)
        except FaqSourceValidationError:
            raise
        except Exception as exc:
            raise FaqSourceError("Unable to read the FAQ worksheet") from exc

    async def load(self) -> FaqSourceResult:
        return await asyncio.to_thread(self._load_sync)


class JsonFaqSource:
    def __init__(self, path: Path) -> None:
        self._path = path

    async def load(self) -> FaqSourceResult:
        try:
            # Local fixtures are small test/development inputs; Google I/O remains offloaded above.
            content = self._path.read_text(encoding="utf-8")
            rows = json.loads(content)
        except Exception as exc:
            raise FaqSourceError("Unable to read the local FAQ fixture") from exc
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            raise FaqSourceValidationError("FAQ fixture must be a JSON array of objects")
        return parse_faq_rows(rows)
