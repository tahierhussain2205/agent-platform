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
from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

from psycopg.types.json import Jsonb

from app.db.tenant import tenant_tx
from app.tools.base import ActionTier, ApproverRule, CredentialMode, Source, Target
from app.tools.registry import Registry

log = logging.getLogger(__name__)

Mode = Literal["ask", "auto"]
Outcome = Literal["allowed", "needs_approval", "denied"]
AUDIT_DECISION = {"allowed": "allowed", "denied": "denied", "needs_approval": "info"}


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
        if tier == "reversible_write" and self.scope.mode == "auto":
            return Decision(
                "allowed", "ok", "Auto mode allows reversible writes.", tier, mode, op.approver_rule
            )
        reason = (
            "Ask mode: reversible writes need approval."
            if tier == "reversible_write"
            else "Actions that reach people outside this conversation or cannot be undone need approval."
        )
        return Decision("needs_approval", "approval_required", reason, tier, mode, op.approver_rule)

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

    async def check_output(self, destination: Destination, sources: list[Source]) -> OutputDecision:
        decision = await self._audience(destination, sources)
        await self.audit(
            "policy.output",
            f"{destination.kind} {destination.name or destination.channel_external_id}",
            "allowed" if decision.deliver == "as_is" else "info",
            decision.reason,
            {"deliver": decision.deliver, "restricted": list(decision.restricted), "sources": len(sources)},
        )
        return decision

    async def _audience(self, destination: Destination, sources: list[Source]) -> OutputDecision:
        """Content goes to a channel only if everyone who can read that channel could read every
        source: the same channel, a public channel, or a private channel whose members include
        every member of a private destination. A public destination never gets private content
        (anyone in the workspace can join it later). Otherwise: deliver privately."""
        if destination.kind == "dm":
            return OutputDecision("as_is", "Direct message to the requester.")
        others = {
            s.channel_external_id: s
            for s in sources
            if s.channel_external_id != destination.channel_external_id
        }
        risky = {cid: s for cid, s in others.items() if s.visibility != "public"}
        if not risky:
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
                    restricted.append(source.channel_name)
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
                    restricted.append(source.channel_name)
        if not restricted:
            return OutputDecision("as_is", "Every member here can read the private sources.")
        names = ", ".join(f"#{n}" for n in sorted(set(restricted)))
        return OutputDecision(
            "private", f"Uses {names}, which not everyone here can read.", tuple(sorted(set(restricted)))
        )

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
