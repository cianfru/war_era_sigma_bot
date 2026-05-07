from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = Field(..., alias="DATABASE_URL")
    warera_api_key: str | None = Field(default=None, alias="WARERA_API_KEY")
    warera_base_url: str = Field(
        default="https://api2.warera.io/trpc", alias="WARERA_BASE_URL"
    )

    telegram_bot_token: str | None = Field(default=None, alias="TELEGRAM_BOT_TOKEN")
    telegram_chat_id: str | None = Field(default=None, alias="TELEGRAM_CHAT_ID")
    enable_bot_commands: bool = Field(default=True, alias="ENABLE_BOT_COMMANDS")

    poll_interval_minutes: int = Field(default=15, alias="POLL_INTERVAL_MINUTES")
    snapshot_retention_days: int = Field(default=90, alias="SNAPSHOT_RETENTION_DAYS")

    log_level: str = Field(default="INFO", alias="LOG_LEVEL")


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
