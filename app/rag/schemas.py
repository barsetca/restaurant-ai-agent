import hashlib
import json
import re
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_EMAIL_PATTERN = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
_PHONE_PATTERN = re.compile(r"(?<!\w)(?:\+?\d[\d ()-]{7,}\d)(?!\w)")


class FaqEntry(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    source_id: str = Field(min_length=1, max_length=255)
    category: str = Field(min_length=1, max_length=120)
    question: str = Field(min_length=1, max_length=2000)
    answer: str = Field(min_length=1, max_length=5000)
    updated_at: str | None = Field(default=None, max_length=100)

    @field_validator("source_id", "category", "question", "answer")
    @classmethod
    def reject_control_characters(cls, value: str) -> str:
        if any(ord(character) < 32 and character not in "\n\t" for character in value):
            raise ValueError("control characters are not allowed")
        return value

    @model_validator(mode="after")
    def reject_personal_data(self) -> "FaqEntry":
        combined = f"{self.question}\n{self.answer}"
        if _EMAIL_PATTERN.search(combined) or _PHONE_PATTERN.search(combined):
            raise ValueError("FAQ rows containing email addresses or phone numbers are not indexed")
        return self

    @property
    def content_hash(self) -> str:
        canonical = json.dumps(
            {"category": self.category, "question": self.question, "answer": self.answer},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @property
    def embedding_text(self) -> str:
        return f"Категория: {self.category}\nВопрос: {self.question}\nОтвет: {self.answer}"


@dataclass(frozen=True)
class FaqSourceResult:
    entries: list[FaqEntry]
    inactive: int = 0
    skipped_empty: int = 0


class RetrievedFaq(BaseModel):
    entry: FaqEntry
    score: float


class AnswerDecision(BaseModel):
    supported: bool
    conflicting: bool
    answer: str
