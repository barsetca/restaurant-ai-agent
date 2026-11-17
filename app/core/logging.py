import json
import logging
import re
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Any

REDACTED = "[REDACTED]"

_TELEGRAM_URL_RE = re.compile(
    r"(?P<prefix>https?://api\.telegram\.org/bot)"
    r"(?P<token>\d{6,12}:[A-Za-z0-9_-]{20,})",
    re.IGNORECASE,
)
_TELEGRAM_TOKEN_RE = re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{20,}\b")
_OPENAI_KEY_RE = re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{16,}\b", re.IGNORECASE)
_AUTH_HEADER_RE = re.compile(
    r"(?P<prefix>\bauthorization\b\s*[:=]\s*['\"]?)"
    r"(?!%[A-Za-z])(?:(?:bearer|basic)\s+)?[^\s,'\"}\]]+",
    re.IGNORECASE,
)
_BEARER_RE = re.compile(
    r"(?P<prefix>\bbearer\s+)[A-Za-z0-9._~+/=-]{8,}",
    re.IGNORECASE,
)
_API_KEY_RE = re.compile(
    r"(?P<prefix>\b(?:x-api-key|api[_-]?key)\b\s*[:=]\s*['\"]?)"
    r"(?!%[A-Za-z])[^\s,'\"}&}\]]+",
    re.IGNORECASE,
)
_URL_PASSWORD_RE = re.compile(
    r"(?P<prefix>[A-Za-z][A-Za-z0-9+.-]*://[^\s/:@]+:)"
    r"[^\s/@]+(?P<suffix>@)",
)
_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----.*?"
    r"-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
    re.DOTALL,
)


class SecretRedactor:
    def __init__(self, secrets: Iterable[str | None] = ()) -> None:
        # Avoid exact-match redaction of short/common values. Structured patterns
        # below still cover credentials in known header and URL forms.
        self._secrets = tuple(
            sorted({value for value in secrets if value and len(value) >= 8}, key=len, reverse=True)
        )

    def redact_text(self, value: str) -> str:
        redacted = value
        for secret in self._secrets:
            redacted = redacted.replace(secret, REDACTED)
        redacted = _TELEGRAM_URL_RE.sub(r"\g<prefix>[REDACTED]", redacted)
        redacted = _TELEGRAM_TOKEN_RE.sub(REDACTED, redacted)
        redacted = _OPENAI_KEY_RE.sub(REDACTED, redacted)
        redacted = _AUTH_HEADER_RE.sub(r"\g<prefix>[REDACTED]", redacted)
        redacted = _BEARER_RE.sub(r"\g<prefix>[REDACTED]", redacted)
        redacted = _API_KEY_RE.sub(r"\g<prefix>[REDACTED]", redacted)
        redacted = _URL_PASSWORD_RE.sub(r"\g<prefix>[REDACTED]\g<suffix>", redacted)
        return _PRIVATE_KEY_RE.sub(REDACTED, redacted)

    def redact_value(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.redact_text(value)
        if isinstance(value, tuple):
            return tuple(self.redact_value(item) for item in value)
        if isinstance(value, list):
            return [self.redact_value(item) for item in value]
        if isinstance(value, Mapping):
            return {key: self.redact_value(item) for key, item in value.items()}
        if isinstance(value, BaseException):
            return self.redact_text(str(value))
        # Libraries such as httpx pass URL objects through logging args. Keep
        # ordinary objects intact, but replace an object with its safe string
        # representation when redaction actually changed that representation.
        rendered = str(value)
        redacted = self.redact_text(rendered)
        if redacted != rendered:
            return redacted
        return value

    def redact_record(self, record: logging.LogRecord) -> None:
        record.msg = self.redact_value(record.msg)
        record.args = self.redact_value(record.args)
        if record.exc_info:
            traceback_text = logging.Formatter().formatException(record.exc_info)
            record.exc_text = self.redact_text(traceback_text)
            # Every downstream handler now sees only the preformatted safe traceback.
            record.exc_info = None
        elif record.exc_text:
            record.exc_text = self.redact_text(record.exc_text)


_active_redactor = SecretRedactor()
_base_record_factory = logging.getLogRecordFactory()
_factory_installed = False


def _install_redacting_record_factory(redactor: SecretRedactor) -> None:
    global _active_redactor, _factory_installed
    _active_redactor = redactor
    if _factory_installed:
        return

    def redacting_factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
        record = _base_record_factory(*args, **kwargs)
        _active_redactor.redact_record(record)
        return record

    logging.setLogRecordFactory(redacting_factory)
    _factory_installed = True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        # Defense in depth: logging ``extra`` fields are attached after the
        # LogRecord factory returns, and another library may replace the factory.
        _active_redactor.redact_record(record)
        payload: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        elif record.exc_text:
            payload["exception"] = record.exc_text

        for field in ("http_method", "endpoint", "status_code", "duration_ms", "exception_type"):
            value = getattr(record, field, None)
            if value is not None:
                payload[field] = _active_redactor.redact_value(value)

        # Reserved for a future request-scoped correlation_id filter.
        correlation_id = getattr(record, "correlation_id", None)
        if correlation_id:
            payload["correlation_id"] = _active_redactor.redact_value(correlation_id)
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(level: str, secrets: Iterable[str | None] = ()) -> None:
    _install_redacting_record_factory(SecretRedactor(secrets))
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())
