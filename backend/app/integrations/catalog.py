"""The integration catalog: what can be enabled, how it authenticates, whether people link their own
accounts. Per org choices live in ``app.org_integrations``; customer supplied MCP servers are org
rows of kind ``custom_mcp`` (key ``mcp_<name>``) that need admin approval before they are enabled."""

import re
from dataclasses import dataclass
from typing import Literal

Kind = Literal["native", "vendor_mcp", "custom_mcp"]
Auth = Literal["slack", "oauth2", "github_app", "api_key", "none"]


@dataclass(frozen=True)
class Integration:
    key: str
    title: str
    kind: Kind
    auth: Auth
    description: str
    user_links: bool
    provider: str | None = None  # OAuth provider key in app.integrations.providers

    @property
    def connectable(self) -> bool:
        return self.auth in ("oauth2", "github_app")


CATALOG: dict[str, Integration] = {
    "slack": Integration(
        key="slack",
        title="Slack",
        kind="native",
        auth="slack",
        description="Read allowed channels and threads, answer in Slack. Connected from the Slack page.",
        user_links=False,
    ),
    "jira": Integration(
        key="jira",
        title="Jira",
        kind="native",
        auth="oauth2",
        description="Search and read issues. Writes (create, assign, comment) use each person's own account.",
        user_links=True,
        provider="jira",
    ),
    "github": Integration(
        key="github",
        title="GitHub",
        kind="native",
        auth="github_app",
        description="Read repositories, files and CODEOWNERS through the GitHub App. People link their "
        "account so the agent can check what they may read.",
        user_links=True,
        provider="github",
    ),
    "google_workspace": Integration(
        key="google_workspace",
        title="Google Workspace",
        kind="vendor_mcp",
        auth="oauth2",
        description="Sheets and Drive through Google's MCP server, with each person's own Google account.",
        user_links=True,
        provider="google_workspace",
    ),
}

CUSTOM_PREFIX = "mcp_"


def custom_key(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_")[:40] or "server"
    return f"{CUSTOM_PREFIX}{slug}"


def is_custom(key: str) -> bool:
    return key.startswith(CUSTOM_PREFIX)
