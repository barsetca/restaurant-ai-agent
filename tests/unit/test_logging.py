import json
import logging
import sys

import httpx
import pytest

from app.bot.bootstrap import _telegram_request_started, _telegram_response_received
from app.core.logging import JsonFormatter, SecretRedactor

TELEGRAM_TOKEN = "123456789:synthetic_telegram_token_value_12345"
OPENAI_KEY = "sk-proj-synthetic_openai_key_value_12345"
CUSTOM_SECRET = "synthetic-database-password"


def _format(record: logging.LogRecord, redactor: SecretRedactor | None = None) -> dict[str, object]:
    (redactor or SecretRedactor()).redact_record(record)
    return json.loads(JsonFormatter().format(record))


def test_redacts_plain_message_and_keeps_diagnostics() -> None:
    record = logging.LogRecord(
        "httpx",
        logging.INFO,
        __file__,
        1,
        (
            "POST https://api.telegram.org/bot"
            f"{TELEGRAM_TOKEN}/getUpdates status=200 duration_ms=42 "
            f"Authorization: Bearer {OPENAI_KEY}"
        ),
        (),
        None,
    )
    record.http_method = "POST"
    record.endpoint = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
    record.status_code = 200
    record.duration_ms = 42

    payload = _format(record)
    rendered = json.dumps(payload)

    assert TELEGRAM_TOKEN not in rendered
    assert OPENAI_KEY not in rendered
    assert "https://api.telegram.org/bot[REDACTED]/getUpdates" in payload["message"]
    assert payload["http_method"] == "POST"
    assert payload["endpoint"] == "https://api.telegram.org/bot[REDACTED]/getUpdates"
    assert payload["status_code"] == 200
    assert payload["duration_ms"] == 42


def test_redacts_parameterized_message_args_and_known_secret() -> None:
    record = logging.LogRecord(
        "httpx",
        logging.INFO,
        __file__,
        1,
        'HTTP Request: %s %s "%s %d %s" api_key=%s',
        (
            "POST",
            httpx.URL(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"),
            "HTTP/1.1",
            200,
            "OK",
            CUSTOM_SECRET,
        ),
        None,
    )

    payload = _format(record, SecretRedactor((CUSTOM_SECRET,)))
    message = str(payload["message"])

    assert TELEGRAM_TOKEN not in message
    assert CUSTOM_SECRET not in message
    assert "POST" in message
    assert "/sendMessage" in message
    assert "200 OK" in message


def test_redacts_exception_message_and_traceback() -> None:
    try:
        raise RuntimeError(
            "request failed for "
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getMe "
            f"with Authorization=Bearer {OPENAI_KEY}"
        )
    except RuntimeError:
        record = logging.LogRecord(
            "telegram.ext.Updater",
            logging.ERROR,
            __file__,
            1,
            "Polling request failed",
            (),
            exc_info=sys.exc_info(),
        )

    payload = _format(record)
    rendered = json.dumps(payload)
    exception = str(payload["exception"])

    assert TELEGRAM_TOKEN not in rendered
    assert OPENAI_KEY not in rendered
    assert "RuntimeError" in exception
    assert "/getMe" in exception
    assert "[REDACTED]" in exception
    assert record.exc_info is None


def test_redacts_authorization_mapping_and_url_password() -> None:
    record = logging.LogRecord(
        "client",
        logging.DEBUG,
        __file__,
        1,
        "headers=%r database=%s",
        (
            {"Authorization": f"Bearer {OPENAI_KEY}", "X-Api-Key": OPENAI_KEY},
            f"postgresql://restaurant:{CUSTOM_SECRET}@postgres:5432/restaurant",
        ),
        None,
    )

    payload = _format(record, SecretRedactor((CUSTOM_SECRET,)))
    message = str(payload["message"])

    assert OPENAI_KEY not in message
    assert CUSTOM_SECRET not in message
    assert "postgres:5432/restaurant" in message
    assert message.count("[REDACTED]") >= 3


@pytest.mark.anyio
async def test_telegram_http_timing_log_has_safe_diagnostics(caplog: pytest.LogCaptureFixture) -> None:
    request = httpx.Request(
        "POST",
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",
    )
    await _telegram_request_started(request)
    response = httpx.Response(200, request=request)

    with caplog.at_level(logging.INFO, logger="app.bot.bootstrap"):
        await _telegram_response_received(response)

    record = caplog.records[-1]
    assert record.getMessage() == "Telegram HTTP request completed"
    assert record.http_method == "POST"
    assert record.endpoint == "/getUpdates"
    assert record.status_code == 200
    assert isinstance(record.duration_ms, float)
    assert record.duration_ms >= 0
    assert TELEGRAM_TOKEN not in str(record.__dict__)
