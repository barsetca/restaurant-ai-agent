from functools import lru_cache

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_env: str = "development"
    app_host: str = "0.0.0.0"
    app_port: int = 8000
    log_level: str = "INFO"
    database_url: str = "postgresql+asyncpg://restaurant:restaurant_demo_only@localhost:5432/restaurant"
    telegram_bot_token: str | None = None
    telegram_enabled: bool = False
    openai_api_key: str | None = None
    google_sheets_spreadsheet_id: str | None = None
    google_credentials_path: str | None = None
    google_calendar_id: str | None = None
    qdrant_url: str = "http://localhost:6333"

    @model_validator(mode="after")
    def validate_enabled_integrations(self) -> "Settings":
        if self.telegram_enabled and not self.telegram_bot_token:
            raise ValueError("TELEGRAM_BOT_TOKEN is required when TELEGRAM_ENABLED=true")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
