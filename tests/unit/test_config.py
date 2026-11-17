import pytest
from pydantic import ValidationError

from app.core.config import Settings


def test_external_credentials_are_optional_when_integrations_are_disabled() -> None:
    settings = Settings(
        _env_file=None,
        telegram_enabled=False,
        telegram_bot_token=None,
        openai_api_key=None,
        google_credentials_path=None,
    )
    assert settings.telegram_enabled is False
    assert settings.openai_api_key is None


def test_telegram_token_is_required_only_when_enabled() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, telegram_enabled=True, telegram_bot_token=None)
