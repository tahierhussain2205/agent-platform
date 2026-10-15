"""Typed settings. Every value comes from the environment (Render rule 2)."""

from functools import lru_cache
from typing import Literal
from urllib.parse import quote, urlsplit, urlunsplit

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore", frozen=True)

    env: Literal["local", "test", "staging", "production"] = "local"
    port: int = Field(default=8000, ge=1, le=65535)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    # JSON lines (production); set false for human readable local output.
    log_json: bool = True
    # OTLP/HTTP collector base URL (Jaeger locally). Unset: no traces exported.
    otel_exporter_otlp_endpoint: str | None = None

    # Direct (non pooled) connection used only by `migrate` and `seed`. It holds a session
    # advisory lock, so it must never point at PgBouncer.
    migration_database_url: SecretStr | None = None
    # Application traffic (Render: the PgBouncer connectionPoolString). Its user and password are
    # replaced by each component's own login user; see component_dsn().
    database_url: SecretStr | None = None
    db_api_password: SecretStr | None = None
    db_worker_password: SecretStr | None = None
    db_vault_password: SecretStr | None = None
    db_identity_password: SecretStr | None = None
    db_maintenance_password: SecretStr | None = None
    db_pool_min_size: int = Field(default=1, ge=0, le=50)
    db_pool_max_size: int = Field(default=10, ge=1, le=200)

    temporal_address: str = "localhost:7233"
    temporal_namespace: str = "default"
    temporal_task_queue: str = "agent-tasks"
    # Temporal Cloud: an API key switches the client to TLS. Unset for the local dev server.
    temporal_api_key: SecretStr | None = None
    # How long a stopping worker waits for in-flight activities before cancelling them.
    worker_graceful_shutdown_seconds: int = Field(default=60, ge=0, le=290)

    # Console: browser origins allowed to call the API and to obtain session tokens (Clerk `azp`).
    console_origins: str = "http://localhost:5173"

    # Clerk (or the local stand-in). Issuer = the instance's Frontend API URL.
    clerk_issuer: str | None = None
    clerk_jwks_url: str | None = None
    clerk_api_url: str = "https://api.clerk.com/v1"
    clerk_secret_key: SecretStr | None = None
    # Svix endpoint secret(s), "whsec_...", comma separated during rotation.
    clerk_webhook_secret: SecretStr | None = None

    # Public URLs (OAuth redirects and links back to the console).
    api_public_url: str = "http://localhost:8000"
    console_url: str = "http://localhost:5173"

    # Slack app (or the local stand-in).
    slack_api_url: str = "https://slack.com/api"
    slack_authorize_url: str = "https://slack.com/oauth/v2/authorize"
    slack_client_id: str | None = None
    slack_client_secret: SecretStr | None = None
    slack_signing_secret: SecretStr | None = None
    # Signs the OAuth `state` parameter (install flow).
    slack_state_secret: SecretStr | None = None

    # Key Value (Redis compatible).
    redis_url: SecretStr | None = None

    # Envelope encryption: master keys "v1:<base64 32 bytes>,v2:..." and the version used for new
    # secrets. Old versions stay listed until every ciphertext has been re-wrapped.
    encryption_keys: SecretStr | None = None
    encryption_active_key: str | None = None

    # Object storage (S3 compatible). Endpoint unset means AWS S3.
    s3_endpoint_url: str | None = None
    s3_region: str = "us-east-1"
    s3_bucket: str | None = None
    s3_access_key_id: SecretStr | None = None
    s3_secret_access_key: SecretStr | None = None

    @field_validator(
        "migration_database_url",
        "database_url",
        "db_api_password",
        "db_worker_password",
        "db_vault_password",
        "db_identity_password",
        "db_maintenance_password",
        "temporal_api_key",
        "redis_url",
        "encryption_keys",
        "s3_access_key_id",
        "s3_secret_access_key",
        "otel_exporter_otlp_endpoint",
        "s3_endpoint_url",
        "clerk_issuer",
        "clerk_jwks_url",
        "clerk_secret_key",
        "clerk_webhook_secret",
        "slack_client_secret",
        "slack_signing_secret",
        "slack_state_secret",
        mode="before",
    )
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

    @property
    def console_origin_list(self) -> list[str]:
        return [o.strip().rstrip("/") for o in self.console_origins.split(",") if o.strip()]

    @property
    def clerk_webhook_secrets(self) -> list[str]:
        if self.clerk_webhook_secret is None:
            return []
        return [s.strip() for s in self.clerk_webhook_secret.get_secret_value().split(",") if s.strip()]

    def db_password(self, component: str) -> str | None:
        secret: SecretStr | None = getattr(self, f"db_{component}_password")
        return secret.get_secret_value() if secret else None

    def component_dsn(self, component: str) -> str:
        """DATABASE_URL with the component's own login user and password."""
        from app.db.roles import LOGIN_USERS

        if self.database_url is None:
            raise RuntimeError("DATABASE_URL is not set")
        user = LOGIN_USERS[component]  # type: ignore[index]
        password = self.db_password(component)
        if password is None:
            raise RuntimeError(f"DB_{component.upper()}_PASSWORD is not set")
        parts = urlsplit(self.database_url.get_secret_value())
        host = parts.hostname or ""
        if ":" in host:  # IPv6 literal
            host = f"[{host}]"
        netloc = f"{quote(user.username, safe='')}:{quote(password, safe='')}@{host}"
        if parts.port:
            netloc += f":{parts.port}"
        return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


@lru_cache
def get_settings() -> Settings:
    return Settings()
