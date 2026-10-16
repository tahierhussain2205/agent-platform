"""Proposing the service catalog (plan phase 10): an admin asks from the console; one activity reads
GitHub and Jira within that admin's access (through the ``ToolExecutor``) and saves proposals for
admins to confirm."""

import uuid
from collections.abc import Callable
from datetime import timedelta
from typing import Any
from uuid import UUID

from temporalio import activity, workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from psycopg.types.json import Jsonb

    from app.agent.runner import AgentDeps
    from app.catalog import build, save
    from app.db.tenant import tenant_tx
    from app.policy import Policy, TaskScope
    from app.tools.base import RefBook, ToolContext
    from app.tools.executor import ToolExecutor, build_registry


class CatalogActivities:
    def __init__(self, deps: AgentDeps) -> None:
        self.d = deps

    async def _executor(self, org_id: str, membership_id: str) -> ToolExecutor:
        """Tools for the proposing admin: reads only, no task (nothing is posted anywhere)."""
        async with tenant_tx(self.d.pool, org_id) as conn:
            row = await (
                await conn.execute(
                    "select u.email, w.id, w.external_id, w.bot_user_id, pi.external_user_id "
                    "from app.org_memberships m join app.users u on u.id = m.user_id "
                    "left join app.workspaces w on w.org_id = m.org_id and w.status = 'active' "
                    "left join app.platform_identities pi on pi.membership_id = m.id and pi.workspace_id "
                    "= w.id "
                    "where m.id = %s and m.status = 'active' and m.privilege = 'admin'",
                    (membership_id,),
                )
            ).fetchone()
        if row is None:
            raise PermissionError("only an active admin proposes the catalog")
        email, workspace, team, bot, slack_user = row
        ctx = ToolContext(
            UUID(org_id),
            UUID(membership_id),
            workspace or uuid.UUID(int=0),
            team or "",
            slack_user or "",
            "",
            self.d.pool,
            self.d.slack,
            self.d.kv,
            RefBook(),
            task_id=None,
            requester_email=email,
            http=self.d.http,
            settings=self.d.settings,
            vault=self.d.vault,
        )
        registry = await build_registry(ctx, bot)
        scope = TaskScope(
            UUID(org_id),
            None,
            UUID(membership_id),
            workspace or uuid.UUID(int=0),
            slack_user or "",
            "",
            None,
            "ask",
        )
        return ToolExecutor(ctx, registry, Policy(self.d.pool, scope, registry))

    @activity.defn(name="catalog_propose")
    async def propose(self, org_id: str, membership_id: str) -> dict[str, Any]:
        executor = await self._executor(org_id, membership_id)
        proposals, notes = await build(executor, self.d.pool, org_id)
        counts = await save(self.d.pool, org_id, proposals)
        async with tenant_tx(self.d.pool, org_id) as conn:
            await conn.execute(
                "insert into app.audit_log (org_id, actor_type, actor_id, action, target, metadata) "
                "values (%s, 'agent', %s, 'catalog.proposed', 'service catalog', %s)",
                (org_id, membership_id, Jsonb({**counts, "notes": notes[:10]})),
            )
        return {**counts, "notes": notes}

    def all(self) -> list[Callable[..., Any]]:
        return [self.propose]


@workflow.defn(name="CatalogProposalWorkflow")
class CatalogProposalWorkflow:
    @workflow.run
    async def run(self, org_id: str, membership_id: str) -> dict[str, Any]:
        result: dict[str, Any] = await workflow.execute_activity_method(
            CatalogActivities.propose,
            args=[org_id, membership_id],
            start_to_close_timeout=timedelta(minutes=10),
            retry_policy=RetryPolicy(maximum_attempts=3, non_retryable_error_types=["PermissionError"]),
        )
        return result
