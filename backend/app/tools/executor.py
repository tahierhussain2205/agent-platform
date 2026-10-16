"""Every tool call runs the same way (spec 9.2, phase 8 direction): resolve the target, ask the policy,
pick the credential its decision names, run the adapter. Adding a tool is writing an adapter and its
metadata; this, the policy module and the audit log apply to it unchanged.

Credential modes (spec 4.4): ``org`` and ``org_with_check`` use the org connection (the adapter
verifies the requester's access for the latter), ``user_required`` uses the requester's own linked
account. Credentials come from the vault, scoped to the running task.
"""

import logging
from dataclasses import dataclass, field
from typing import Any

from app.db.tenant import tenant_tx
from app.integrations.catalog import CATALOG
from app.policy import Decision, Member, Policy
from app.tools.base import Source, Target, ToolContext
from app.tools.registry import Registry
from app.vault_service import Credential, VaultError

log = logging.getLogger(__name__)


class ToolUnavailableError(Exception):
    """The credential the decision needs is missing or not usable; the message says what to do."""


@dataclass
class ExecResult:
    text: str
    is_error: bool = False
    decision: Decision | None = None
    target: Target | None = None
    data: dict[str, Any] = field(default_factory=dict)
    denied: bool = False


def _title(key: str) -> str:
    return CATALOG[key].title if key in CATALOG else key


class ToolExecutor:
    def __init__(self, ctx: ToolContext, registry: Registry, policy: Policy) -> None:
        self.ctx = ctx
        self.registry = registry
        self.policy = policy

    async def credential_for(self, integration: str, mode: str) -> Credential | None:
        """The credential a mode names. Slack uses the bot token, the catalog none (None here)."""
        if integration in ("slack", "catalog", "sandbox"):
            return None
        key = f"credential:{integration}:{'user' if mode == 'user_required' else 'org'}"
        cached: Credential | None = self.ctx.cache.get(key)
        if cached is not None:
            return cached
        async with tenant_tx(self.ctx.pool, self.ctx.org_id) as conn:
            if mode == "user_required":
                row = await (
                    await conn.execute(
                        "select id from app.connections where integration_key = %s and scope = 'user' "
                        "and membership_id = %s and status = 'active'",
                        (integration, self.ctx.membership_id),
                    )
                ).fetchone()
            else:
                row = await (
                    await conn.execute(
                        "select id from app.connections where integration_key = %s and scope = 'org' "
                        "and status = 'active'",
                        (integration,),
                    )
                ).fetchone()
        if row is None:
            if mode == "user_required":
                raise ToolUnavailableError(f"Link your {_title(integration)} account in the console first.")
            raise ToolUnavailableError(f"{_title(integration)} is not connected for this organization.")
        try:
            credential: Credential = await self.ctx.vault.credential(
                self.ctx.org_id, row[0], task_id=self.ctx.task_id, membership_id=self.ctx.membership_id
            )
        except VaultError as exc:
            raise ToolUnavailableError(f"{_title(integration)}: {exc.message}") from exc
        self.ctx.cache[key] = credential
        return credential

    async def run(
        self, name: str, args: dict[str, Any], allowed: set[str] | None = None, approved: bool = False
    ) -> ExecResult:
        """One tool call by its model facing name. A ``needs_approval`` decision is returned to the
        caller unless the action was ``approved`` already."""
        found = self.registry.lookup(name) if allowed is None or name in allowed else None
        if found is None:
            return ExecResult(f"The tool {name} is not available in this step.", is_error=True)
        adapter, op = found
        target = await adapter.resolve_target(self.ctx, op.name, args)
        decision = await self.policy.check_tool(adapter.key, op.name, target)
        if decision.outcome == "denied":
            return ExecResult(f"Access denied: {decision.reason}", True, decision, target, denied=True)
        if decision.outcome == "needs_approval" and not approved:
            return ExecResult(decision.reason, False, decision, target)
        try:
            credential = await self.credential_for(
                adapter.key, decision.credential_mode or op.credential_mode
            )
        except ToolUnavailableError as exc:
            return ExecResult(f"Not available: {exc}", True, decision, target, denied=True)
        result = await adapter.execute(self.ctx, op.name, args, credential)
        return ExecResult(result.text, result.is_error, decision, target, result.data)

    async def audience(self, source: Source, members: list[Member]) -> bool:
        """Whether every member may see a restricted source, asked of the adapter that produced it
        (with the org connection, which can look up other people's permissions)."""
        adapter_key = source.audience.split(":", 1)[0] if source.audience else source.kind
        adapter = self.registry.adapters.get(adapter_key)
        checker = getattr(adapter, "audience_ok", None)
        if adapter is None or checker is None:
            return False
        try:
            credential = await self.credential_for(adapter_key, "org")
        except ToolUnavailableError:
            return False
        try:
            return bool(await checker(self.ctx, credential, source, members))
        except Exception:  # an audience we cannot confirm is not confirmed
            log.warning("audience check failed", extra={"source": source.kind}, exc_info=True)
            return False


async def build_registry(ctx: ToolContext, bot_user_id: str | None, executor_factory: Any = None) -> Registry:
    """Slack, the service catalog, and the integrations this org enabled. MCP servers bring their tools
    from discovery (cached in the integration's config)."""
    from app.tools.catalog import CatalogAdapter
    from app.tools.github import GitHubAdapter
    from app.tools.jira import JiraAdapter
    from app.tools.logs import LogsAdapter
    from app.tools.mcp import McpAdapter
    from app.tools.sandbox import SandboxAdapter
    from app.tools.slack import SlackAdapter

    registry = Registry()
    registry.register(SlackAdapter(bot_user_id))
    registry.register(CatalogAdapter())  # our own catalog: always there
    async with tenant_tx(ctx.pool, ctx.org_id) as conn:
        rows = await (
            await conn.execute(
                "select integration_key, kind, config, approved_at is not null from app.org_integrations "
                "where enabled and integration_key <> 'slack' order by integration_key"
            )
        ).fetchall()
    settings = ctx.settings
    for key, kind, config, approved in rows:
        if key == "jira" and settings is not None:
            registry.register(JiraAdapter(settings.atlassian_api_url))
        elif key == "github" and settings is not None:
            registry.register(GitHubAdapter(settings.github_api_url))
        elif key == "google_cloud_logging" and settings is not None:
            registry.register(LogsAdapter(settings))
        elif key == "google_workspace" and settings is not None and settings.google_mcp_url:
            registry.register(
                McpAdapter(key, settings.google_mcp_url, config.get("tools") or [], "user_required")
            )
        elif kind == "custom_mcp" and approved and config.get("url"):
            registry.register(McpAdapter(key, str(config["url"]), config.get("tools") or [], "org"))
    if "github" in registry.adapters and settings is not None and settings.sandbox_url:
        registry.register(SandboxAdapter())  # working copies of the org's repositories
    return registry
