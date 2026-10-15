"""Typed settings. Every value comes from the environment (Render rule 2)."""

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore", frozen=True)

    env: Literal["local", "test", "staging", "production"] = "local"
    port: int = Field(default=8000, ge=1, le=65535)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    # Direct (non pooled) connection used only by `migrate` and `seed`. It holds a session
    # advisory lock, so it must never point at PgBouncer.
    migration_database_url: SecretStr | None = None

    temporal_address: str = "localhost:7233"
    temporal_namespace: str = "default"
    temporal_task_queue: str = "agent-tasks"
    # Temporal Cloud: an API key switches the client to TLS. Unset for the local dev server.
    temporal_api_key: SecretStr | None = None
    # How long a stopping worker waits for in-flight activities before cancelling them.
    worker_graceful_shutdown_seconds: int = Field(default=60, ge=0, le=290)

    # Key Value (Redis compatible). Used from phase 4.
    redis_url: SecretStr | None = None

    @field_validator("migration_database_url", "temporal_api_key", "redis_url", mode="before")
    @classmethod
    def _empty_secret_is_unset(cls, value: object) -> object:
        # An empty `sync: false` variable (or `KEY=` in a .env file) means "not configured".
        if isinstance(value, str) and not value.strip():
            return None
        return value

    def require_migration_database_url(self) -> str:
        if self.migration_database_url is None:
            raise RuntimeError("MIGRATION_DATABASE_URL is not set")
        return self.migration_database_url.get_secret_value()


@lru_cache
def get_settings() -> Settings:
    return Settings()
