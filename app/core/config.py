from functools import lru_cache
import re

from pydantic import Field, model_validator
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
    database_url: str | None = None
    telegram_bot_token: str | None = None
    telegram_enabled: bool = False
    thread_id_hmac_secret: str | None = None
    thread_id_hmac_key_id: str = "v1"
    faq_enabled: bool = False
    openai_api_key: str | None = None
    openai_chat_model: str = "gpt-4.1-mini"
    llm_debug_response_json: bool = False
    openai_embedding_model: str = "text-embedding-3-small"
    openai_embedding_dimensions: int = Field(default=1536, gt=0)
    google_sheets_spreadsheet_id: str | None = None
    google_credentials_path: str | None = None
    google_calendar_id: str | None = None
    qdrant_url: str = "http://localhost:6333"
    qdrant_collection: str = "faq"
    faq_retrieval_top_k: int = Field(default=4, ge=1, le=10)
    faq_retrieval_score_threshold: float = Field(default=0.30, ge=0.0, le=1.0)
    faq_request_timeout_seconds: float = Field(default=15.0, gt=0.0, le=120.0)
    faq_max_context_characters: int = Field(default=6000, ge=500, le=20000)
    mcp_startup_timeout_seconds: float = Field(default=10.0, gt=0.0, le=60.0)
    mcp_tool_timeout_seconds: float = Field(default=15.0, gt=0.0, le=120.0)
    mcp_shutdown_timeout_seconds: float = Field(default=5.0, gt=0.0, le=30.0)

    @property
    def llm_debug_response_json_enabled(self) -> bool:
        return self.llm_debug_response_json and self.app_env.casefold() in {
            "development",
            "dev",
            "local",
        }

    @model_validator(mode="after")
    def validate_enabled_integrations(self) -> "Settings":
        if not self.database_url:
            raise ValueError("DATABASE_URL is required")
        if self.telegram_enabled and not self.telegram_bot_token:
            raise ValueError("TELEGRAM_BOT_TOKEN is required when TELEGRAM_ENABLED=true")
        if self.telegram_enabled and (
            not self.thread_id_hmac_secret or len(self.thread_id_hmac_secret) < 32
        ):
            raise ValueError(
                "THREAD_ID_HMAC_SECRET with at least 32 characters is required "
                "when TELEGRAM_ENABLED=true"
            )
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", self.thread_id_hmac_key_id):
            raise ValueError("THREAD_ID_HMAC_KEY_ID must be a safe 1-32 character identifier")
        if self.faq_enabled:
            missing = [
                name
                for name, value in (
                    ("OPENAI_API_KEY", self.openai_api_key),
                    ("GOOGLE_SHEETS_SPREADSHEET_ID", self.google_sheets_spreadsheet_id),
                    ("GOOGLE_CREDENTIALS_PATH", self.google_credentials_path),
                    ("QDRANT_URL", self.qdrant_url),
                )
                if not value
            ]
            if missing:
                raise ValueError(f"FAQ_ENABLED=true requires: {', '.join(missing)}")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
