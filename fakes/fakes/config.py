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
    # Atlassian (Jira), GitHub, Google and a customer run MCP server
    atlassian_client_id: str = field(default_factory=lambda: os.environ.get("ATLASSIAN_CLIENT_ID", "local-jira-client"))
    atlassian_client_secret: str = field(
        default_factory=lambda: os.environ.get("ATLASSIAN_CLIENT_SECRET", "local-jira-client-secret")
    )
    atlassian_token_ttl: int = field(default_factory=lambda: int(os.environ.get("ATLASSIAN_TOKEN_TTL", "3600")))
    github_app_id: str = field(default_factory=lambda: os.environ.get("GITHUB_APP_ID", "424242"))
    github_app_slug: str = field(default_factory=lambda: os.environ.get("GITHUB_APP_SLUG", "agent-platform-local"))
    sandbox_api_key: str = field(default_factory=lambda: os.environ.get("SANDBOX_API_KEY", "sbx-local-key"))
    sandbox_root: Path | None = field(
        default_factory=lambda: Path(os.environ["SANDBOX_ROOT"]) if os.environ.get("SANDBOX_ROOT") else None
    )
    gcp_sa_email: str = field(
        default_factory=lambda: os.environ.get("GCP_SA_EMAIL", "agent-reader@acme-prod.iam.gserviceaccount.com")
    )
    gcp_sa_public_key: str = field(default_factory=lambda: os.environ.get("GCP_SA_PUBLIC_KEY", "").replace("\\n", "\n"))
    github_app_public_key: str = field(
        default_factory=lambda: os.environ.get("GITHUB_APP_PUBLIC_KEY", "").replace("\\n", "\n")
    )
    github_client_id: str = field(default_factory=lambda: os.environ.get("GITHUB_CLIENT_ID", "Iv1.localclient"))
    github_client_secret: str = field(
        default_factory=lambda: os.environ.get("GITHUB_CLIENT_SECRET", "local-github-client-secret")
    )
    github_setup_url: str = field(
        default_factory=lambda: os.environ.get("GITHUB_SETUP_URL", "http://localhost:8000/oauth/github/install")
    )
    github_public_url: str = field(
        default_factory=lambda: os.environ.get("GITHUB_PUBLIC_URL", "http://localhost:4100/github")
    )
    github_webhook_url: str | None = field(default_factory=lambda: os.environ.get("GITHUB_WEBHOOK_URL") or None)
    github_webhook_secret: str = field(
        default_factory=lambda: os.environ.get("GITHUB_WEBHOOK_SECRET", "local-github-webhook-secret")
    )
    github_user_token_ttl: int = field(default_factory=lambda: int(os.environ.get("GITHUB_USER_TOKEN_TTL", "28800")))
    google_client_id: str = field(default_factory=lambda: os.environ.get("GOOGLE_CLIENT_ID", "local-google-client"))
    google_client_secret: str = field(
        default_factory=lambda: os.environ.get("GOOGLE_CLIENT_SECRET", "local-google-client-secret")
    )
    google_token_ttl: int = field(default_factory=lambda: int(os.environ.get("GOOGLE_TOKEN_TTL", "3599")))
    custom_mcp_key: str = field(default_factory=lambda: os.environ.get("CUSTOM_MCP_KEY", "mcp-key-local"))
    # Anthropic
    anthropic_api_key: str = field(default_factory=lambda: os.environ.get("ANTHROPIC_API_KEY", "sk-ant-local-key"))
    anthropic_models: list[str] = field(
        default_factory=lambda: _list(
            os.environ.get("ANTHROPIC_MODELS", "claude-haiku-4-5-20251001,claude-sonnet-5,claude-opus-5-5")
        )
    )
