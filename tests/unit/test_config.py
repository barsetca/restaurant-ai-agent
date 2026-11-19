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


def test_thread_hmac_secret_is_required_only_when_telegram_is_enabled() -> None:
    with pytest.raises(ValidationError, match="THREAD_ID_HMAC_SECRET"):
        Settings(
            _env_file=None,
            telegram_enabled=True,
            telegram_bot_token="123456:synthetic-token",
            thread_id_hmac_secret=None,
        )

    settings = Settings(
        _env_file=None,
        telegram_enabled=True,
        telegram_bot_token="123456:synthetic-token",
        thread_id_hmac_secret="synthetic-hmac-secret-at-least-32-characters",
    )
    assert settings.thread_id_hmac_key_id == "v1"


def test_database_url_is_required_without_embedded_default(monkeypatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(ValidationError, match="DATABASE_URL is required"):
        Settings(_env_file=None)


def test_database_url_is_accepted_from_configuration() -> None:
    url = "postgresql+asyncpg://user:synthetic@localhost:5432/restaurant_test"
    settings = Settings(_env_file=None, database_url=url)
    assert settings.database_url == url


def test_faq_credentials_are_optional_when_disabled() -> None:
    settings = Settings(_env_file=None, faq_enabled=False, openai_api_key=None)
    assert settings.faq_enabled is False


def test_faq_configuration_is_validated_when_enabled() -> None:
    with pytest.raises(ValidationError, match="FAQ_ENABLED=true requires"):
        Settings(_env_file=None, faq_enabled=True, openai_api_key=None)


def test_mcp_timeouts_are_positive_and_bounded() -> None:
    settings = Settings(
        _env_file=None,
        mcp_startup_timeout_seconds=1,
        mcp_tool_timeout_seconds=2,
        mcp_shutdown_timeout_seconds=3,
    )
    assert settings.mcp_tool_timeout_seconds == 2
    with pytest.raises(ValidationError):
        Settings(_env_file=None, mcp_tool_timeout_seconds=0)


def test_llm_debug_response_json_is_dev_only_and_disabled_by_default() -> None:
    default = Settings(_env_file=None)
    assert default.llm_debug_response_json is False
    assert default.llm_debug_response_json_enabled is False

    development = Settings(
        _env_file=None,
        app_env="development",
        llm_debug_response_json=True,
    )
    assert development.llm_debug_response_json_enabled is True

    production = Settings(
        _env_file=None,
        app_env="production",
        llm_debug_response_json=True,
    )
    assert production.llm_debug_response_json_enabled is False
