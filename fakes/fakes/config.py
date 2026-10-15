"""Configuration from the environment."""

import os
from dataclasses import dataclass, field
from pathlib import Path


def _list(value: str) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()]


@dataclass(frozen=True)
class Config:
    state_dir: Path = field(default_factory=lambda: Path(os.environ.get("FAKES_STATE_DIR", "/data")))
    seed_fixture: Path = field(default_factory=lambda: Path(os.environ.get("FAKES_SEED_FIXTURE", "/seed/fixture.json")))
    console_origins: list[str] = field(
        default_factory=lambda: _list(os.environ.get("CONSOLE_ORIGINS", "http://localhost:5173"))
    )
    dev_password: str = field(default_factory=lambda: os.environ.get("FAKES_DEV_PASSWORD", "password123"))
    # Clerk
    clerk_issuer: str = field(default_factory=lambda: os.environ.get("CLERK_ISSUER", "http://localhost:4100/clerk"))
    clerk_secret_key: str = field(default_factory=lambda: os.environ.get("CLERK_SECRET_KEY", "sk_test_local"))
    clerk_webhook_url: str | None = field(default_factory=lambda: os.environ.get("CLERK_WEBHOOK_URL") or None)
    clerk_webhook_secret: str = field(
        default_factory=lambda: os.environ.get(
            "CLERK_WEBHOOK_SECRET", "whsec_bG9jYWwtY2xlcmstd2ViaG9vay1zZWNyZXQtMDAwMQ=="
        )
    )
    clerk_token_ttl: int = field(default_factory=lambda: int(os.environ.get("CLERK_TOKEN_TTL", "60")))
    # Slack
    slack_public_url: str = field(
        default_factory=lambda: os.environ.get("SLACK_PUBLIC_URL", "http://localhost:4100/slack")
    )
    slack_client_id: str = field(default_factory=lambda: os.environ.get("SLACK_CLIENT_ID", "local.client"))
    slack_client_secret: str = field(
        default_factory=lambda: os.environ.get("SLACK_CLIENT_SECRET", "local-slack-client-secret")
    )
    slack_signing_secret: str = field(
        default_factory=lambda: os.environ.get("SLACK_SIGNING_SECRET", "local-slack-signing-secret")
    )
    slack_events_url: str | None = field(default_factory=lambda: os.environ.get("SLACK_EVENTS_URL") or None)
    slack_interactions_url: str | None = field(default_factory=lambda: os.environ.get("SLACK_INTERACTIONS_URL") or None)
    # Anthropic
    anthropic_api_key: str = field(default_factory=lambda: os.environ.get("ANTHROPIC_API_KEY", "sk-ant-local-key"))
    anthropic_models: list[str] = field(
        default_factory=lambda: _list(
            os.environ.get("ANTHROPIC_MODELS", "claude-haiku-4-5-20251001,claude-sonnet-5,claude-opus-5-5")
        )
    )
