"""The policy module (spec 9.2): one place that approves or blocks every tool call and every output.

It answers, for a task acting on behalf of its requester:
1. Is the tool enabled for this org (and the operation not turned off by an admin)?
2. For channel reads and posts: is the channel allowlisted and is the requester a member?
3. Does the action tier fit the effective Ask or Auto mode?
4. Which credential applies (org, org with check, user)?
5. Is the output safe for its audience?

Every decision is written to the audit log. The model can be persuaded; this module cannot.
"""

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

from psycopg.types.json import Jsonb

from app.agent import roles
from app.db.tenant import tenant_tx
from app.tools.base import ActionTier, ApproverRule, CredentialMode, Source, Target
from app.tools.registry import Registry

log = logging.getLogger(__name__)

Mode = Literal["ask", "auto"]
Outcome = Literal["allowed", "needs_approval", "denied"]
AUDIT_DECISION = {"allowed": "allowed", "denied": "denied", "needs_approval": "info"}


# Built in tools over our own data and infrastructure: the service catalog, the task's working copies.
ALWAYS_ON = {"catalog", "sandbox"}
IRREVERSIBLE_REASON = "Actions that reach people outside this conversation or cannot be undone need approval."
SHADOW_REASON = (
    "Shadow mode: I read, plan and answer, but make no changes until an admin turns shadow mode off "
    "in the console."
)


@dataclass(frozen=True)
class TaskScope:
    org_id: UUID
    task_id: UUID | None
    membership_id: UUID
    workspace_id: UUID
    slack_user_id: str
    channel_external_id: str  # where the conversation happens (a D... id for DMs)
    channel_id: UUID | None  # the tracked channel; None for DMs
    mode: Mode


@dataclass(frozen=True)
class Decision:
    outcome: Outcome
    code: str
    reason: str
    tier: ActionTier | None = None
    credential_mode: CredentialMode | None = None
    approver_rule: ApproverRule | None = None
    approver_role_id: UUID | None = None  # with approver_rule "role_or_admin": whose holders decide
    approver_role: str | None = None

    @property
    def allowed(self) -> bool:
        return self.outcome == "allowed"


@dataclass(frozen=True)
class Destination:
    kind: Literal["thread", "channel", "dm"]
    channel_external_id: str
    channel_id: UUID | None = None
    name: str | None = None


@dataclass(frozen=True)
class Member:
    """Someone at an output's destination, as the audience check sees them."""

    membership_id: UUID
    email: str
    slack_user_id: str
    links: dict[str, str]  # integration key -> linked external account (a Jira account id, a login)


AudienceCheck = Callable[[Source, list[Member]], Awaitable[bool]]


@dataclass(frozen=True)
class OutputDecision:
    deliver: Literal["as_is", "private"]
    reason: str
    restricted: tuple[str, ...] = ()


class Policy:
    def __init__(self, pool: Any, scope: TaskScope, registry: Registry) -> None:
        self.pool = pool
        self.scope = scope
        self.registry = registry

    # ------------------------------------------------------------------ tool calls

    async def check_tool(self, adapter_key: str, operation: str, target: Target | None) -> Decision:
        decision = await self._decide(adapter_key, operation, target)
        where = f" #{target.name}" if target else ""
        await self.audit(
            "policy.tool",
            f"{adapter_key}.{operation}{where}",
            AUDIT_DECISION[decision.outcome],
            decision.reason,
            {
                "outcome": decision.outcome,
                "code": decision.code,
                "tier": decision.tier,
                "credential_mode": decision.credential_mode,
                "mode": self.scope.mode,
            },
        )
        return decision

    async def _decide(self, adapter_key: str, operation: str, target: Target | None) -> Decision:
        adapter = self.registry.adapters.get(adapter_key)
        op = adapter.operations.get(operation) if adapter else None
        if adapter is None or op is None:
            return Decision("denied", "tool_unknown", f"There is no tool {adapter_key}.{operation}.")
        async with tenant_tx(self.pool, self.scope.org_id) as conn:
            enabled = await (
                await conn.execute(
                    "select enabled from app.org_integrations where integration_key = %s", (adapter_key,)
                )
            ).fetchone()
            if adapter_key in ALWAYS_ON:
                enabled = (True,)
            if not enabled or not enabled[0]:
                return Decision(
                    "denied",
                    "integration_disabled",
                    f"{adapter_key.title()} is not enabled for this organization.",
                )
            override = await (
                await conn.execute(
                    "select credential_mode, enabled from app.operation_policies "
                    "where integration_key = %s and operation = %s",
                    (adapter_key, operation),
                )
            ).fetchone()
            if override and not override[1]:
                return Decision(
                    "denied", "operation_disabled", f"An admin turned off {adapter_key} {operation}."
                )
            mode: CredentialMode = override[0] if override else op.credential_mode
            name = f"{adapter_key}.{operation}"
            limits = await roles.limits_for(conn, self.scope.membership_id)
            if name in limits.blocked:  # roles narrow what the agent does for their holders
                return Decision(
                    "denied", "role_blocked",
                    f"Your role ({limits.blocked[name]}) does not include {adapter_key} {operation}.",
                    op.tier, mode,
                )  # fmt: skip
            approver = await roles.approver_for(conn, name) if op.tier != "read" else None
            shadow = await (await conn.execute("select shadow_mode from app.org_settings")).fetchone()
            needs_channel = "channel" in op.input_schema.get("required", [])
            if needs_channel:
                if target is None:
                    return Decision("denied", "channel_unknown", "That channel is not known.")
                denial = await self._channel_rule(conn, target)
                if denial:
                    return denial
            if mode == "user_required":
                linked = await (
                    await conn.execute(
                        "select 1 from app.connections where integration_key = %s and scope = 'user' "
                        "and membership_id = %s and status = 'active'",
                        (adapter_key, self.scope.membership_id),
                    )
                ).fetchone()
                if linked is None:
                    return Decision(
                        "denied", "link_required", f"Link your {adapter_key.title()} account in the console.",
                        op.tier, mode,
                    )  # fmt: skip
        tier = op.tier
        if tier == "read":
            return Decision("allowed", "ok", "Read access.", tier, mode)
        if shadow is not None and shadow[0]:  # spec 15: plans and answers, never acts, until trusted
            return Decision("denied", "shadow_mode", SHADOW_REASON, tier, mode)
        asks = limits.ask.get(name)
        if tier == "reversible_write" and self.scope.mode == "auto" and not asks:
            return Decision(
                "allowed", "ok", "Auto mode allows reversible writes.", tier, mode, op.approver_rule
            )
        code, reason = "approval_required", IRREVERSIBLE_REASON
        if tier == "reversible_write":
            reason = "Ask mode: reversible writes need approval."
            if asks and self.scope.mode == "auto":
                code, reason = "role_ask", f"Your role ({asks}) asks before {adapter_key} {operation}."
        if approver is not None:  # an admin routed this operation's approvals to a role
            return Decision(
                "needs_approval", code, reason, tier, mode, "role_or_admin", approver[0], approver[1]
            )
        return Decision("needs_approval", code, reason, tier, mode, op.approver_rule)

    async def _channel_rule(self, conn: Any, target: Target) -> Decision | None:
        row = await (
            await conn.execute(
                "select is_allowlisted, archived, app.can_read_channel(%s, id) from app.channels "
                "where id = %s",
                (self.scope.membership_id, target.id),
            )
        ).fetchone()
        if row is None:
            return Decision("denied", "channel_unknown", "That channel is not known.")
        allowlisted, archived, readable = row
        if archived:
            return Decision("denied", "channel_archived", f"#{target.name} is archived.")
        if not allowlisted:
            return Decision(
                "denied", "channel_not_allowlisted", f"#{target.name} is not enabled for the agent."
            )
        if not readable:
            return Decision(
                "denied",
                "not_a_member",
                f"You are not a member of #{target.name}, so I can't use it for you.",
            )
        return None

    # ------------------------------------------------------------------ outputs

    async def check_output(
        self, destination: Destination, sources: list[Source], audience: AudienceCheck | None = None
    ) -> OutputDecision:
        """``audience`` answers, for a restricted source (a Jira project, a private repository), whether
        every given member may see it; without it, restricted sources are delivered privately."""
        decision = await self._audience(destination, sources, audience)
        await self.audit(
            "policy.output",
            f"{destination.kind} {destination.name or destination.channel_external_id}",
            "allowed" if decision.deliver == "as_is" else "info",
            decision.reason,
            {"deliver": decision.deliver, "restricted": list(decision.restricted), "sources": len(sources)},
        )
        return decision

    async def _audience(
        self, destination: Destination, sources: list[Source], audience: AudienceCheck | None
    ) -> OutputDecision:
        """Content goes to a channel only if everyone who can read that channel could read every
        source: the same channel, a public channel, or a private channel whose members include
        every member of a private destination. A public destination never gets private Slack
        content (anyone in the workspace can join it later). Restricted sources from other tools
        need every member to be an identified org member the adapter confirms can see them.
        Otherwise: deliver privately."""
        if destination.kind == "dm":
            return OutputDecision("as_is", "Direct message to the requester.")
        slack = [s for s in sources if s.kind == "slack"]
        tools = [s for s in sources if s.kind != "slack" and s.visibility != "public"]
        others = {
            s.channel_external_id: s
            for s in slack
            if s.channel_external_id != destination.channel_external_id
        }
        risky = {cid: s for cid, s in others.items() if s.visibility != "public"}
        if not risky and not tools:
            return OutputDecision("as_is", "Every source is visible to this audience.")
        restricted: list[str] = []
        async with tenant_tx(self.pool, self.scope.org_id) as conn:
            dest = await (
                await conn.execute(
                    "select c.is_private, array(select cm.external_user_id from app.channel_members cm "
                    "where cm.channel_id = c.id) from app.channels c "
                    "where c.workspace_id = %s and c.external_id = %s",
                    (self.scope.workspace_id, destination.channel_external_id),
                )
            ).fetchone()
            for cid, source in risky.items():
                if source.visibility == "dm" or dest is None or not dest[0] or not dest[1]:
                    restricted.append(f"#{source.channel_name}")
                    continue
                members = await (
                    await conn.execute(
                        "select array(select cm.external_user_id from app.channel_members cm "
                        "join app.channels c on c.id = cm.channel_id "
                        "where c.workspace_id = %s and c.external_id = %s)",
                        (self.scope.workspace_id, cid),
                    )
                ).fetchone()
                if not set(dest[1]) <= set(members[0] if members else []):
                    restricted.append(f"#{source.channel_name}")
            people = await self._members(conn, list(dest[1]) if dest and dest[1] else [])
        for source in tools:
            known = people is not None and audience is not None
            if not known or not await audience(source, people or []):  # type: ignore[misc]
                restricted.append(source.channel_name)
        if not restricted:
            return OutputDecision("as_is", "Everyone here can see the sources.")
        names = ", ".join(sorted(set(restricted)))
        return OutputDecision(
            "private", f"Uses {names}, which not everyone here can see.", tuple(sorted(set(restricted)))
        )

    async def _members(self, conn: Any, slack_ids: list[str]) -> list[Member] | None:
        """The destination's members as org members (email and linked accounts), or None when
        someone there is not an identified org member (a guest, someone not invited)."""
        if not slack_ids:
            return None
        rows = await (
            await conn.execute(
                "select pi.external_user_id, m.id, u.email from app.platform_identities pi "
                "join app.org_memberships m on m.id = pi.membership_id and m.status in ('active', 'invited') "
                "join app.users u on u.id = m.user_id "
                "where pi.workspace_id = %s and pi.external_user_id = any(%s)",
                (self.scope.workspace_id, slack_ids),
            )
        ).fetchall()
        if len(rows) < len(set(slack_ids)):
            return None
        links = await (
            await conn.execute(
                "select membership_id, integration_key, external_account_id from app.connections "
                "where scope = 'user' and status = 'active' and membership_id = any(%s)",
                ([r[1] for r in rows],),
            )
        ).fetchall()
        linked: dict[UUID, dict[str, str]] = {}
        for membership, key, account in links:
            if account:
                linked.setdefault(membership, {})[key] = account
        return [Member(r[1], r[2], r[0], linked.get(r[1], {})) for r in rows]

    # ------------------------------------------------------------------ audit

    async def audit(
        self, action: str, target: str, decision: str, reason: str, metadata: dict[str, Any] | None = None
    ) -> None:
        async with tenant_tx(self.pool, self.scope.org_id) as conn:
            await conn.execute(
                "insert into app.audit_log (org_id, actor_type, actor_id, task_id, action, target, decision, "
                "reason, metadata) values (%s, 'agent', %s, %s, %s, %s, %s, %s, %s)",
                (
                    self.scope.org_id,
                    self.scope.membership_id,
                    self.scope.task_id,
                    action,
                    target[:500],
                    decision,
                    reason[:1000],
                    Jsonb({k: v for k, v in (metadata or {}).items() if v is not None}),
                ),
            )
