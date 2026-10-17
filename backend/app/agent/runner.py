"""The agent loop as plain async steps: understand, gather, plan, act, verify, report (spec 7.1).

Temporal activities and the eval harness call the same steps. Each step opens its own tenant
transactions, reads Slack and calls the model inside the step, and returns derived content only
(``app.agent.types``): raw messages never leave the step (invariant 7).

The orchestrator stays thin: the brief drives retrieval; the gather step is an isolated subagent
with only the playbook's read tools, whose every call passes the policy module; the answer is
composed from its findings, checked in code (verify) and checked against its audience before it
is delivered.
"""

import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from psycopg.types.json import Jsonb

from app.agent import prompts, tickets
from app.agent.implementation import Implementation
from app.agent.investigation import Investigation
from app.agent.outbox import Sink, SlackSink, post_once
from app.agent.playbooks import BUILTIN, PlaybookSpec, keys, latest
from app.agent.types import (
    ActionOutcome,
    ApprovalState,
    BudgetCheck,
    CaseFile,
    ChangeCase,
    Composed,
    Delivered,
    Exported,
    Findings,
    Implemented,
    PlannedAction,
    ReviewResult,
    StartResult,
    TaskRef,
    Understanding,
    Verification,
)
from app.db.tenant import tenant_tx
from app.integrations.catalog import CATALOG
from app.llm.gateway import BudgetExceededError, CallContext, Lane, LLMGateway, LLMResponseError
from app.llm.models import Tier
from app.policy import Destination, Policy, TaskScope
from app.policy.redaction import redact
from app.sandbox import provider_from_settings
from app.sandbox.workspaces import Workspaces
from app.slack import service
from app.slack.adapter import ApprovalRequest, ChannelAdapter, DirectMessage, Progress, Reply, escape
from app.slack.client import SlackClient, SlackError
from app.tools.base import RefBook, Source, Target, ToolContext, definitions, tool_name
from app.tools.executor import ExecResult, ToolExecutor, ToolUnavailableError, build_registry
from app.tools.mcp import McpAdapter, McpError
from app.tools.registry import Registry
from app.tools.slack import SlackAdapter

log = logging.getLogger(__name__)

STEP_SEQ = {"gather": 1, "plan": 2, "act": 3, "verify": 4, "report": 5}
MAX_GATHER_ROUNDS = 6
STEP_TOKEN_CAP = 150_000
LOW_CONFIDENCE = 0.6
MAX_WINDOW_DAYS = 31
MAX_PERMALINKS = 25
MAX_ACTIONS = 5
MAX_TICKETS = 5  # tickets a change moves along (the plan's issues)
TICKET_ACTIONS = 100  # action numbers of those updates, apart from the plan's and the push
AUTO_PLAN_LIMIT = 3  # Auto drops to Ask above this many changes (spec 4.5.3)
CONVERSATION_LIMIT = 50
TERMINAL = ("completed", "failed", "cancelled")
TIERS: tuple[Tier, ...] = ("fast", "standard", "advanced")


@dataclass
class AgentDeps:
    pool: Any
    vault: Any
    slack: SlackClient
    kv: Any
    gateway: LLMGateway
    console_url: str = "http://localhost:5173"
    approval_ttl_hours: int = 24
    sink_factory: Callable[[str, str], Sink] | None = None  # (team, token) -> Sink
    now: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))
    settings: Any = None  # provider API and MCP base URLs for the integration adapters
    http: Any = None  # httpx.AsyncClient for the integration adapters
    store: Any = None  # ObjectStore for large artifacts (diffs)


@dataclass
class Context:
    org_name: str
    bot_user_id: str | None
    timezone: str
    channel_name: str
    roles: list[prompts.RoleInfo]
    requester_email: str | None = None


def as_tier(value: str) -> Tier:
    return next((t for t in TIERS if t == value), "standard")


class AgentRunner:
    def __init__(self, deps: AgentDeps) -> None:
        self.d = deps

    # ------------------------------------------------------------------ plumbing

    async def _token(self, ref: TaskRef) -> tuple[str, str]:
        return await service.bot_token(self.d.pool, self.d.vault, UUID(ref.org_id))

    async def _sink(self, ref: TaskRef) -> Sink:
        team, token = await self._token(ref)
        if self.d.sink_factory is not None:
            return self.d.sink_factory(team, token)
        return SlackSink(ChannelAdapter(self.d.slack, self.d.kv), team, token)

    async def _context(self, ref: TaskRef) -> Context:
        async with tenant_tx(self.d.pool, ref.org_id) as conn:
            row = await (
                await conn.execute(
                    "select o.name, w.bot_user_id, coalesce(p.timezone, 'UTC'), c.name from app.orgs o "
                    "join app.workspaces w on w.id = %s "
                    "left join app.user_preferences p on p.membership_id = %s "
                    "left join app.channels c on c.id = %s",
                    (ref.workspace_id, ref.membership_id, ref.channel_id),
                )
            ).fetchone()
            email = await (
                await conn.execute(
                    "select u.email from app.org_memberships m join app.users u on u.id = m.user_id "
                    "where m.id = %s",
                    (ref.membership_id,),
                )
            ).fetchone()
            roles = await (
                await conn.execute(
                    "select r.title, r.description, mr.is_primary from app.membership_roles mr "
                    "join app.custom_roles r on r.id = mr.role_id "
                    "where mr.membership_id = %s and r.status = 'active'",
                    (ref.membership_id,),
                )
            ).fetchall()
        if row is None:
            raise RuntimeError("task context not found")
        channel_name = row[3] or ("direct message" if ref.channel_type == "im" else ref.channel)
        role_info = [prompts.RoleInfo(t, d, p) for t, d, p in roles]
        return Context(row[0], row[1], row[2], channel_name, role_info, email[0] if email else None)

    def _call(self, ref: TaskRef, purpose: str) -> CallContext:
        lane: Lane = "batch" if ref.lane == "batch" else "interactive"
        return CallContext(UUID(ref.org_id), UUID(ref.membership_id), UUID(ref.task_id), purpose, lane)

    async def _tools(
        self, ref: TaskRef, ctx: Context, mode: str, refs: RefBook
    ) -> tuple[ToolContext, Registry, Policy, ToolExecutor]:
        """The task's tools: Slack plus the org's enabled integrations, the policy and the executor
        every call goes through."""
        tctx = await self._tool_ctx(ref, refs, ctx)
        registry = await build_registry(tctx, ctx.bot_user_id)
        policy = Policy(self.d.pool, self._scope(ref, mode), registry)
        executor = ToolExecutor(tctx, registry, policy)
        await self._discover(ref, registry, executor)
        return tctx, registry, policy, executor

    async def _discover(self, ref: TaskRef, registry: Registry, executor: ToolExecutor) -> None:
        """MCP servers without cached tools: discover them with the credential their mode names, and
        cache the list on the integration."""
        for adapter in list(registry.adapters.values()):
            if not isinstance(adapter, McpAdapter) or adapter.operations:
                continue
            try:
                credential = await executor.credential_for(adapter.key, adapter.mode)
                if credential is None:
                    continue
                tools = await adapter.discover(executor.ctx, credential)
            except (ToolUnavailableError, McpError, httpx.HTTPError) as exc:
                log.info(
                    "mcp discovery skipped", extra={"integration": adapter.key, "reason": str(exc)[:200]}
                )
                continue
            async with tenant_tx(self.d.pool, ref.org_id) as conn:
                await conn.execute(
                    "update app.org_integrations set config = config || %s where integration_key = %s",
                    (Jsonb({"tools": tools}), adapter.key),
                )

    @staticmethod
    def _definitions(registry: Registry, spec: PlaybookSpec, kind: str) -> list[dict[str, Any]]:
        """Tool definitions for the operations of one kind (read or write) the playbook allows."""
        out: list[dict[str, Any]] = []
        for adapter in registry.adapters.values():
            mcp = isinstance(adapter, McpAdapter)
            names = [
                op.name
                for op in adapter.operations.values()
                if (op.tier == "read") == (kind == "read")
                and spec.allows(adapter.key, op.name, op.tier, mcp, getattr(adapter, "family", None))
            ]
            out += definitions(adapter, names)
        return out

    def _scope(self, ref: TaskRef, mode: str) -> TaskScope:
        return TaskScope(
            UUID(ref.org_id),
            UUID(ref.task_id),
            UUID(ref.membership_id),
            UUID(ref.workspace_id),
            ref.slack_user_id,
            ref.channel,
            UUID(ref.channel_id) if ref.channel_id else None,
            "auto" if mode == "auto" else "ask",
        )

    async def _tool_ctx(self, ref: TaskRef, refs: RefBook, ctx: Context | None = None) -> ToolContext:
        team, token = await self._token(ref)
        return ToolContext(
            UUID(ref.org_id),
            UUID(ref.membership_id),
            UUID(ref.workspace_id),
            team,
            ref.slack_user_id,
            token,
            self.d.pool,
            self.d.slack,
            self.d.kv,
            refs,
            task_id=UUID(ref.task_id),
            requester_email=ctx.requester_email if ctx else None,
            http=self.d.http,
            settings=self.d.settings,
            vault=self.d.vault,
            store=self.d.store,
        )

    def _here(self, ref: TaskRef, ctx: Context) -> Target:
        return Target(
            "slack_channel",
            UUID(ref.channel_id) if ref.channel_id else None,
            ref.channel,
            ctx.channel_name,
            ref.channel_type != "channel",
            ref.channel_type == "im",
        )

    async def step(self, ref: TaskRef, kind: str, status: str, summary: str | None = None) -> None:
        async with tenant_tx(self.d.pool, ref.org_id) as conn:
            await conn.execute(
                """
                insert into app.task_steps
                  (org_id, task_id, seq, kind, status, summary, started_at, finished_at)
                values (%(org)s, %(task)s, %(seq)s, %(kind)s, %(status)s, %(summary)s,
                        case when %(status)s <> 'pending' then now() end,
                        case when %(status)s in ('completed', 'failed', 'skipped') then now() end)
                on conflict (task_id, seq) do update
                   set status = excluded.status,
                       summary = coalesce(excluded.summary, app.task_steps.summary),
                       started_at = coalesce(app.task_steps.started_at, excluded.started_at),
                       finished_at = excluded.finished_at
                """,
                {
                    "org": ref.org_id,
                    "task": ref.task_id,
                    "seq": STEP_SEQ[kind],
                    "kind": kind,
                    "status": status,
                    "summary": summary[:2000] if summary else None,
                },
            )

    # ------------------------------------------------------------------ lifecycle

    async def start(self, ref: TaskRef) -> StartResult:
        """Start the task if its turn has come: FIFO per session (one running task per thread),
        the org's concurrency limit, and budgets checked before starting (spec 4.8)."""
        status = await self.d.gateway.budget_status(self._call(ref, "start"))
        over = status.exceeded()
        if over is not None and over.scope != "task":
            return StartResult("refused", f"I can't start: {over}.")
        async with tenant_tx(self.d.pool, ref.org_id) as conn:
            await conn.execute(
                "select pg_advisory_xact_lock(hashtextextended(%s, 0))", (f"tasks:{ref.org_id}",)
            )
            task = await (
                await conn.execute(
                    "select status, created_at, effective_mode::text, model_tier::text from app.tasks "
                    "where id = %s",
                    (ref.task_id,),
                )
            ).fetchone()
            if task is None or task[0] in TERMINAL:
                return StartResult("gone")
            if task[0] in ("running", "awaiting_approval"):  # a retried start
                return StartResult("started", mode=task[2], model_tier=task[3])
            ahead = (
                ref.session_id
                and await (  # scheduled runs have no session
                    await conn.execute(
                        "select 1 from app.tasks where session_id = %s and id <> %s and (status in "
                        "('running', 'awaiting_approval') or (status = 'queued' and created_at < %s)) "
                        "limit 1",
                        (ref.session_id, ref.task_id, task[1]),
                    )
                ).fetchone()
            )
            if ahead:
                return StartResult("wait_session", "Another request in this thread is being handled first.")
            counts = await (
                await conn.execute(
                    "select (select count(*) from app.tasks where status = 'running'), "
                    "(select max_concurrent_tasks from app.org_settings)"
                )
            ).fetchone()
            if counts and counts[0] >= counts[1]:
                return StartResult(
                    "wait_org", f"Queued: the organization's limit of {counts[1]} tasks at a time is reached."
                )
            await conn.execute(
                "update app.tasks set status = 'running', started_at = coalesce(started_at, now()) "
                "where id = %s",
                (ref.task_id,),
            )
        return StartResult("started", mode=task[2], model_tier=task[3])

    async def status(self, ref: TaskRef, text: str, ts: str | None, running: bool = True) -> str | None:
        """The one status message per task, created once and edited in place, with a Cancel button
        while the task runs."""
        sink = await self._sink(ref)
        actions = [("task_cancel", "Cancel", ref.task_id)] if running else []
        intent = Progress(ref.channel, text, ref.thread_ts, ts, actions)
        if ts is None:
            return await post_once(
                self.d.pool,
                sink,
                ref.org_id,
                ref.task_id,
                f"task:{ref.task_id}:status",
                intent,
                channel=ref.channel,
                thread_ts=ref.thread_ts,
                operation="post_status",
            )
        try:
            await sink.send(intent)
        except SlackError as exc:  # a status update is never worth failing the task
            log.warning("status update failed", extra={"error": exc.error})
        return ts

    async def reply(self, ref: TaskRef, text: str, key: str) -> str | None:
        sink = await self._sink(ref)
        body, _ = redact(text)
        return await post_once(
            self.d.pool,
            sink,
            ref.org_id,
            ref.task_id,
            f"task:{ref.task_id}:{key}",
            Reply(ref.channel, body, ref.thread_ts),
            channel=ref.channel,
            thread_ts=ref.thread_ts,
        )

    async def set_status(self, ref: TaskRef, status: str) -> None:
        async with tenant_tx(self.d.pool, ref.org_id) as conn:
            await conn.execute(
                "update app.tasks set status = %s "
                "where id = %s and status not in ('completed', 'failed', 'cancelled')",
                (status, ref.task_id),
            )

    async def finish(self, ref: TaskRef, status: str, error: str | None = None) -> None:
        async with tenant_tx(self.d.pool, ref.org_id) as conn:
            await conn.execute(
                "update app.tasks set status = %s, finished_at = now(), error = %s "
                "where id = %s and status not in ('completed', 'failed', 'cancelled')",
                (status, error[:2000] if error else None, ref.task_id),
            )
            if status == "cancelled":
                await conn.execute(
                    "update app.approvals set status = 'expired' where task_id = %s and status = 'pending'",
                    (ref.task_id,),
                )
        await self._release_sandboxes(ref)

    async def _prior_plan(self, ref: TaskRef) -> dict[str, Any] | None:
        """The session's current plan: the latest plan another task of this thread made."""
        if not ref.session_id:
            return None
        async with tenant_tx(self.d.pool, ref.org_id) as conn:
            row = await (
                await conn.execute(
                    "select a.content from app.artifacts a join app.tasks t on t.id = a.task_id "
                    "where t.session_id = %s and a.task_id <> %s and a.kind = 'plan' "
                    "and a.content is not null order by a.created_at desc limit 1",
                    (ref.session_id, ref.task_id),
                )
            ).fetchone()
        return dict(row[0]) if row and isinstance(row[0], dict) else None

    async def _session_tickets(self, ref: TaskRef) -> list[str]:
        """The Jira issues earlier tasks of this thread created, oldest first: "these tickets" in a
        follow-up (the agent does not read its own messages back)."""
        if not ref.session_id:
            return []
        async with tenant_tx(self.d.pool, ref.org_id) as conn:
            rows = await (
                await conn.execute(
                    "select a.target_ref from app.external_actions a join app.tasks t on t.id = a.task_id "
                    "where t.session_id = %s and a.task_id <> %s and a.integration_key = 'jira' "
                    "and a.operation = 'create_issue' and a.status = 'succeeded' "
                    "and a.target_ref like 'jira:%%' order by a.performed_at, a.created_at",
                    (ref.session_id, ref.task_id),
                )
            ).fetchall()
        return list(dict.fromkeys(str(r[0]).removeprefix("jira:") for r in rows))

    async def _session_pull(self, ref: TaskRef) -> dict[str, Any] | None:
        """The pull request a task of this thread opened most recently: ``{"repo", "number", "url"}``."""
        if not ref.session_id:
            return None
        async with tenant_tx(self.d.pool, ref.org_id) as conn:
            row = await (
                await conn.execute(
                    "select l.external_id, l.url from app.task_links l join app.tasks t on t.id = l.task_id "
                    "where t.session_id = %s and l.kind = 'pull_request' order by l.created_at desc limit 1",
                    (ref.session_id,),
                )
            ).fetchone()
        if row is None or "#" not in row[0]:
            return None
        repo, _, number = str(row[0]).rpartition("#")
        return {"repo": repo, "number": int(number), "url": row[1]} if number.isdigit() else None

    async def _release_sandboxes(self, ref: TaskRef) -> None:
        """The task's sandboxes go with it (best effort: they expire, and maintenance destroys them)."""
        provider = provider_from_settings(self.d.settings, self.d.http) if self.d.http else None
        if provider is None:
            return
        try:
            released = await Workspaces(self.d.pool, provider).release_task(ref.org_id, ref.task_id)
        except Exception:
            log.warning("could not release the task's sandboxes", exc_info=True)
            return
        if released:
            log.info("sandboxes released", extra={"count": released})

    async def budget(self, ref: TaskRef) -> BudgetCheck:
        status = await self.d.gateway.budget_status(self._call(ref, "budget"))
        over = status.exceeded()
        return BudgetCheck(True) if over is None else BudgetCheck(False, str(over))

    async def usage(self, ref: TaskRef) -> tuple[int, str]:
        async with tenant_tx(self.d.pool, ref.org_id) as conn:
            row = await (
                await conn.execute("select tokens_used, cost from app.tasks where id = %s", (ref.task_id,))
            ).fetchone()
        return (int(row[0]), str(row[1])) if row else (0, "0")

    # ------------------------------------------------------------------ understand

    async def _conversation(
        self, ref: TaskRef, ctx: Context, refs: RefBook
    ) -> tuple[list[str], str, str | None]:
        """The thread (or the message) the request is in, as cited lines: (lines, the request's text,
        the request's ref). Raw text stays in the calling activity."""
        tctx = await self._tool_ctx(ref, refs)
        adapter = SlackAdapter(ctx.bot_user_id)
        messages: list[dict[str, Any]] = []
        async for m in self.d.slack.paginate(
            "conversations.replies",
            "messages",
            tctx.token,
            tctx.team_id,
            channel=ref.channel,
            ts=ref.thread_ts or ref.message_ts,
            limit=CONVERSATION_LIMIT,
        ):
            messages.append(m)
            if len(messages) >= 4 * CONVERSATION_LIMIT:
                break
        if len(messages) > CONVERSATION_LIMIT:  # the parent and the latest replies
            messages = [messages[0], *messages[-(CONVERSATION_LIMIT - 1) :]]
        lines = await adapter.transcript(tctx, self._here(ref, ctx), messages)
        request_ref = refs.by_key.get(f"slack:{ref.channel}:{ref.message_ts}")
        request_line = next(
            (line for line in lines if request_ref and line.startswith(f"[{request_ref}]")), ""
        )
        request_text = request_line.split(": ", 1)[1] if ": " in request_line else request_line
        return lines, request_text, request_ref

    async def understand(self, ref: TaskRef, mode: str) -> Understanding:
        """Classify the request to a playbook, then turn the conversation into a brief."""
        ctx = await self._context(ref)
        refs = RefBook("B")
        await self.step(ref, "gather", "running", "Understanding the request")
        lines, request_text, request_ref = await self._conversation(ref, ctx, refs)
        prior = await self._prior_plan(ref)
        current_plan = (
            "\n<current_plan>"
            + json.dumps({k: prior.get(k) for k in ("playbook", "goal", "summary")})
            + "</current_plan>"
            if prior
            else ""
        )
        pull = await self._session_pull(ref)
        if pull:
            opened = json.dumps({"repo": pull["repo"], "number": pull["number"]})
            current_plan += f"\n<pull_request>{opened}</pull_request>"
        try:
            result = await self.d.gateway.complete(
                self._call(ref, "classify"),
                "fast",
                system=prompts.system(
                    ctx.org_name, None, ctx.roles, prompts.CLASSIFY.format(catalog=prompts.catalog())
                ),
                messages=[
                    {
                        "role": "user",
                        "content": f"<request>{request_text}</request>\n<conversation>\n"
                        + "\n".join(lines[-10:])
                        + "\n</conversation>"
                        + current_plan,
                    }
                ],
                tools=[prompts.submit_tool("submit_classification", "Submit the classification.")],
                tool_choice={"type": "tool", "name": "submit_classification"},
                max_tokens=400,
            )
            raw = result.tool_input("submit_classification")
            key = str(raw.get("playbook") or "none")
            confidence = max(0.0, min(1.0, float(raw.get("confidence") or 0)))
            if key not in keys(BUILTIN):
                question = escape(
                    str(raw.get("clarifying_question") or "Could you say a bit more about what you need?")
                )
                await self.step(ref, "gather", "completed", "Asked a clarifying question")
                return Understanding(None, None, confidence, question[:500], mode)
            spec = latest(key)
            note = None
            if mode == "auto" and confidence < LOW_CONFIDENCE:
                mode, note = "ask", "I'm not fully sure what you meant, so I'll ask before changing anything."
            playbook_id = await self._pin(ref, spec)
            now = self.d.now()
            local_now = now.astimezone(self._zone(ctx.timezone))
            result = await self.d.gateway.complete(
                self._call(ref, "brief"),
                "fast",
                system=prompts.system(ctx.org_name, spec, ctx.roles, prompts.BRIEF),
                messages=[
                    {
                        "role": "user",
                        "content": "<thread>\n" + "\n".join(lines) + "\n</thread>\n"
                        f"<request_ref>{request_ref or ''}</request_ref>\n"
                        f"<now>{local_now.isoformat()}</now>\n<timezone>{ctx.timezone}</timezone>",
                    }
                ],
                tools=[prompts.submit_tool("submit_brief", "Submit the brief.")],
                tool_choice={"type": "tool", "name": "submit_brief"},
                max_tokens=1500,
            )
            brief = clean_brief(result.tool_input("submit_brief"), refs, now)
            await self._artifact(ref, "brief", brief, [s["ref"] for s in brief_sources(brief)])
            log.info(
                "brief ready",
                extra={"playbook": key, "playbook_id": str(playbook_id), "confidence": confidence},
            )
            return Understanding(spec.key, spec.version, confidence, None, mode, note, brief)
        except BudgetExceededError as exc:
            await self.step(ref, "gather", "failed", f"Stopped: {exc}")
            return Understanding(None, None, 0, None, mode, stopped=str(exc))

    @staticmethod
    def _zone(name: str) -> ZoneInfo:
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            return ZoneInfo("UTC")

    async def _pin(self, ref: TaskRef, spec: PlaybookSpec) -> UUID | None:
        """A task pins the playbook version it started with."""
        async with tenant_tx(self.d.pool, ref.org_id) as conn:
            row = await (
                await conn.execute(
                    "select id from app.playbooks "
                    "where org_id is null and key = %s and version = %s and enabled",
                    (spec.key, spec.version),
                )
            ).fetchone()
            if row is not None:
                await conn.execute(
                    "update app.tasks set playbook_id = %s where id = %s", (row[0], ref.task_id)
                )
        return row[0] if row else None

    async def _artifact(
        self, ref: TaskRef, kind: str, content: dict[str, Any], source_refs: list[str]
    ) -> None:
        async with tenant_tx(self.d.pool, ref.org_id) as conn:
            await conn.execute(
                "insert into app.artifacts (org_id, task_id, kind, content, source_refs) "
                "values (%s, %s, %s, %s, %s)",
                (ref.org_id, ref.task_id, kind, Jsonb(content), Jsonb(source_refs[:500])),
            )

    # ------------------------------------------------------------------ gather

    async def gather(self, ref: TaskRef, u: Understanding, tier: str) -> Findings:
        """An isolated subagent: only the playbook's read tools, every call through the policy,
        bounded rounds and tokens. Returns compact findings with checked citations."""
        spec, brief = playbook_of(u)
        ctx = await self._context(ref)
        refs = RefBook("S")
        _, _, _, executor = await self._tools(ref, ctx, u.mode, refs)
        tools = [
            *self._definitions(executor.registry, spec, "read"),
            prompts.submit_tool("submit_findings", "Submit findings."),
        ]
        channels = ", ".join(f"#{c}" for c in (brief.get("entities") or {}).get("channels") or [])
        await self.step(ref, "gather", "running", f"Reading {channels or 'the conversation'}")
        messages: list[dict[str, Any]] = [
            {
                "role": "user",
                "content": f"<brief>{json.dumps(brief_for_model(brief))}</brief>\n"
                f'<conversation channel="{ctx.channel_name}" thread="{ref.thread_ts or ""}"/>\n'
                f"<now>{self.d.now().isoformat()}</now>",
            }
        ]
        system = prompts.system(ctx.org_name, spec, ctx.roles, prompts.GATHER)
        findings = await self._subagent(ref, tier, executor, tools, system, messages, refs)
        await self._unread_channels(executor, brief, findings)
        if findings.stopped:
            await self.step(ref, "gather", "failed", f"Stopped: {findings.stopped}")
        else:
            where = ", ".join(findings.channels_read) or "no source"
            await self.step(ref, "gather", "completed", f"{len(findings.points)} findings from {where}")
        return findings

    @staticmethod
    async def _unread_channels(executor: ToolExecutor, brief: dict[str, Any], findings: Findings) -> None:
        """A channel the brief names that the subagent did not read is reported with the policy's own
        reason (a model that sees it may not read a channel does not even try, so no call was denied)."""
        slack = executor.registry.adapters.get("slack")
        if slack is None:
            return
        for name in (brief.get("entities") or {}).get("channels") or []:
            label = f"#{name}"
            if label in findings.channels_read or any(g.startswith(f"{label}:") for g in findings.gaps):
                continue
            target = await slack.resolve_target(executor.ctx, "read_channel", {"channel": name})
            decision = await executor.policy.check_tool("slack", "read_channel", target)
            reason = decision.reason if decision.outcome == "denied" else "not read"
            findings.gaps = [f"{label}: {reason}"[:300], *(g for g in findings.gaps if label not in g)][:20]

    async def _subagent(
        self,
        ref: TaskRef,
        tier: str,
        executor: ToolExecutor,
        tools: list[dict[str, Any]],
        system: list[dict[str, Any]],
        messages: list[dict[str, Any]],
        refs: RefBook,
        purpose: str = "gather",
        seen: list[tuple[str, ExecResult]] | None = None,
    ) -> Findings:
        """The tool loop of an isolated subagent: only ``tools`` (the last one is ``submit_findings``),
        every call through the policy, bounded rounds and tokens. ``seen`` collects each tool result
        for checks in code."""
        denied: list[str] = []
        read: list[str] = []
        try:
            raw = await self._tool_loop(
                ref, tier, executor, tools, system, messages, "submit_findings", purpose, denied, read, seen
            )
        except BudgetExceededError as exc:
            return Findings([], denied, read, stopped=str(exc))
        if raw is None:  # pragma: no cover - the last round forces the submission
            return Findings([], [*denied, "The search ended before findings were submitted."], read)
        return self._findings(raw, refs, denied, read)

    async def _tool_loop(
        self,
        ref: TaskRef,
        tier: str,
        executor: ToolExecutor,
        tools: list[dict[str, Any]],
        system: list[dict[str, Any]],
        messages: list[dict[str, Any]],
        submit: str,
        purpose: str,
        denied: list[str],
        read: list[str],
        seen: list[tuple[str, ExecResult]] | None = None,
        rounds: int = MAX_GATHER_ROUNDS,
        token_cap: int = STEP_TOKEN_CAP,
        max_tokens: int = 2500,
    ) -> dict[str, Any] | None:
        """Model and tools in turn until the model calls ``submit`` (forced on the last round or past
        the token cap): only ``tools``, every call through the policy. Returns what was submitted.
        ``denied`` and ``read`` collect what could not be read and what was."""
        allowed_names = {t["name"] for t in tools}
        used = 0
        for round_no in range(rounds + 1):
            force = round_no == rounds or used > token_cap
            result = await self.d.gateway.complete(
                self._call(ref, purpose),
                as_tier(tier),
                system=system,
                messages=messages,
                tools=tools,
                tool_choice={"type": "tool", "name": submit} if force else {"type": "auto"},
                max_tokens=max_tokens,
            )
            used += result.usage.counted
            calls = result.tool_uses()
            submitted = next((c for c in calls if c.get("name") == submit), None)
            if submitted is not None and result.stop_reason == "max_tokens":  # cut off: fields may be missing
                raise LLMResponseError(f"the {submit} answer was cut off at the output limit")
            if submitted is not None:
                raw = submitted.get("input")
                return raw if isinstance(raw, dict) else {}
            if not calls:  # the model answered in prose: ask for the structured result
                messages += [
                    {"role": "assistant", "content": result.content or [{"type": "text", "text": "..."}]},
                    {"role": "user", "content": f"Call {submit} now."},
                ]
                continue
            results = []
            for call in calls:
                text, is_error = await self._run_tool(call, allowed_names, executor, denied, read, seen)
                results.append(
                    {"type": "tool_result", "tool_use_id": call["id"], "content": text, "is_error": is_error}
                )
            messages += [
                {"role": "assistant", "content": result.content},
                {"role": "user", "content": results},
            ]
        return None  # pragma: no cover

    async def _run_tool(
        self,
        call: dict[str, Any],
        allowed: set[str],
        executor: ToolExecutor,
        denied: list[str],
        read: list[str],
        seen: list[tuple[str, ExecResult]] | None = None,
    ) -> tuple[str, bool]:
        name = str(call.get("name"))
        raw_args = call.get("input")
        args: dict[str, Any] = raw_args if isinstance(raw_args, dict) else {}
        result = await executor.run(name, args, allowed)
        if seen is not None:
            seen.append((name, result))
        found = executor.registry.lookup(name)
        tool = CATALOG[found[0].key].title if found and found[0].key in CATALOG else name
        label = (
            f"#{result.target.name}"
            if result.target
            else str(args.get("channel") or args.get("repo") or tool)
        )
        if result.denied:
            reason = result.text.removeprefix("Access denied: ").removeprefix("Not available: ")
            denied.append(f"{label}: {reason}")
            return result.text, True
        if result.decision is not None and result.decision.outcome == "needs_approval":
            return "Changes are not made while gathering information.", True
        if result.is_error:
            denied.append(f"{label}: {result.text}")
        elif label not in read:
            read.append(label)
        return result.text, result.is_error

    @staticmethod
    def _findings(raw: dict[str, Any], refs: RefBook, denied: list[str], read: list[str]) -> Findings:
        points = []
        for point in (raw.get("points") or [])[:40]:
            if not isinstance(point, dict) or not str(point.get("text", "")).strip():
                continue
            sources = [s.to_dict() for r in point.get("sources") or [] if (s := refs.get(str(r))) is not None]
            points.append({"text": redact(str(point["text"]).strip())[0][:600], "sources": sources})
        # What could not be read is always reported in the tools' own words, whatever the model wrote;
        # the model's own account of the same source is dropped.
        labels = [note.split(":", 1)[0] for note in denied]
        model_gaps = [redact(str(g))[0][:300] for g in (raw.get("gaps") or []) if str(g).strip()]
        gaps = [
            *(note[:300] for note in denied),
            *(g for g in model_gaps if not any(lb in g for lb in labels)),
        ]
        return Findings(points, gaps[:20], read)

    # ------------------------------------------------------------------ investigate

    async def orient(self, ref: TaskRef, u: Understanding) -> CaseFile:
        return await Investigation(self).orient(ref, u)

    async def investigate(
        self, ref: TaskRef, u: Understanding, case: CaseFile, kind: str, tier: str
    ) -> Findings:
        return await Investigation(self).investigate(ref, u, case, kind, tier)

    async def plan_investigation(
        self, ref: TaskRef, u: Understanding, case: CaseFile, findings: list[Findings], tier: str
    ) -> Composed:
        return await Investigation(self).plan(ref, u, case, findings, tier)

    # ------------------------------------------------------------------ implement

    async def prepare_change(self, ref: TaskRef, u: Understanding) -> ChangeCase:
        return await Implementation(self).prepare(ref, u)

    async def implement(
        self, ref: TaskRef, u: Understanding, case: ChangeCase, tier: str, feedback: dict[str, Any]
    ) -> Implemented:
        return await Implementation(self).implement(ref, u, case, tier, feedback)

    async def verify_change(self, ref: TaskRef, u: Understanding, case: ChangeCase) -> Verification:
        return await Implementation(self).verify(ref, u, case)

    async def review_change(
        self, ref: TaskRef, u: Understanding, case: ChangeCase, tier: str
    ) -> ReviewResult:
        return await Implementation(self).review(ref, u, case, tier)

    async def export_change(
        self,
        ref: TaskRef,
        u: Understanding,
        case: ChangeCase,
        verification: Verification,
        review: ReviewResult | None,
        implemented: Implemented,
    ) -> Exported:
        return await Implementation(self).export(ref, u, case, verification, review, implemented)

    async def report_change(
        self,
        ref: TaskRef,
        u: Understanding,
        case: ChangeCase,
        attempts: list[Implemented],
        verification: Verification | None,
        review: ReviewResult | None,
        exported: Exported | None,
        outcome: ActionOutcome | None,
    ) -> Composed:
        return await Implementation(self).report(
            ref, u, case, attempts, verification, review, exported, outcome
        )

    # ------------------------------------------------------------------ plan

    async def plan(self, ref: TaskRef, u: Understanding, f: Findings, tier: str) -> list[PlannedAction]:
        spec, brief = playbook_of(u)
        ctx = await self._context(ref)
        _, registry, _, _ = await self._tools(ref, ctx, u.mode, RefBook())
        writes = [d["name"] for d in self._definitions(registry, spec, "write")]
        if not writes:
            await self.step(ref, "plan", "skipped", "No changes are possible with this playbook")
            return []
        await self.step(ref, "plan", "running")
        numbered = [{"finding": n, "text": p["text"]} for n, p in enumerate(f.points[:30], 1)]
        # Each action as the model must give it back (tool and operation), with its combined name.
        offered = [
            {"action": name, "tool": found[0].key, "operation": found[1].name}
            for name in writes
            if (found := registry.lookup(name)) is not None
        ]
        by_name = {o["action"]: (o["tool"], o["operation"]) for o in offered}
        result = await self.d.gateway.complete(
            self._call(ref, "plan"),
            as_tier(tier),
            system=prompts.system(ctx.org_name, spec, ctx.roles, prompts.PLAN),
            messages=[
                {
                    "role": "user",
                    "content": f"<brief>{json.dumps(brief_for_model(brief))}</brief>\n"
                    f"<findings>{json.dumps(numbered)}</findings>\n"
                    f"<available_actions>{json.dumps(offered)}</available_actions>",
                }
            ],
            tools=[prompts.submit_tool("submit_plan", "Submit the plan.")],
            tool_choice={"type": "tool", "name": "submit_plan"},
            max_tokens=2500,
        )
        actions = []
        unknown: list[str] = []
        listed = result.tool_input("submit_plan").get("actions") or []
        unreadable = 0 if isinstance(listed, list) else 1  # the gateway decodes what it can
        for raw in (listed if isinstance(listed, list) else [])[:MAX_ACTIONS]:
            if not isinstance(raw, dict):
                unreadable += 1
                continue
            tool, operation = str(raw.get("tool")), str(raw.get("operation"))
            # The combined name in either field is the same action (models write it both ways).
            for candidate in (tool_name(tool, operation), tool, operation):
                if candidate in by_name:
                    tool, operation = by_name[candidate]
                    raw = {**raw, "tool": tool, "operation": operation}
                    break
            else:
                unknown.append(f"{tool} {operation}"[:80])
                continue
            raw_args = raw.get("args")
            args: dict[str, Any] = raw_args if isinstance(raw_args, dict) else {}
            if raw.get("tool") == "slack" and not str(args.get("channel") or "").strip():
                continue
            if raw.get("tool") == "slack" and isinstance(args.get("text"), str):
                args["text"] = escape(args["text"])  # "{answer}" is escaped when it is written
            summary = escape(str(raw.get("summary") or f"{raw['tool']} {raw['operation']}"))[:300]
            actions.append(PlannedAction(str(raw["tool"]), str(raw["operation"]), dict(args), summary))
        if any(a.tool == "jira" for a in actions):  # checked in code before anything reaches the policy
            _, _, _, executor = await self._tools(ref, ctx, u.mode, RefBook())
            repositories = [str(r) for r in (brief.get("entities") or {}).get("repositories") or []]
            actions = await tickets.review(
                executor,
                actions,
                f.points[:30],
                str(brief.get("goal") or ""),
                partial(self._permalinks, ref),
                repositories,
            )
        # Never drop a requested change silently: the answer must not read as if it happened.
        if unknown or unreadable:
            log.warning(
                "plan named actions that are not available",
                extra={"actions": unknown, "unreadable": unreadable},
            )
            named = f" ({escape(', '.join(unknown))})" if unknown else ""
            await self.reply(
                ref,
                f"⚠️ I could not turn part of this request into an action I can take{named}, so nothing was "
                "changed for it.",
                "plan-unknown",
            )
        changes = [a for a in actions if a.skip is None]
        if u.mode == "auto" and len(changes) > AUTO_PLAN_LIMIT:
            actions = [replace(a, ask=True) for a in actions]
            await self.reply(
                ref,
                f"This plan makes {len(changes)} changes, more than {AUTO_PLAN_LIMIT}, so I'll ask before "
                "each one.",
                "plan-size",
            )
        await self.step(
            ref, "plan", "completed", "; ".join(a.summary for a in changes) or "No changes needed"
        )
        return actions

    # ------------------------------------------------------------------ compose and verify

    async def compose(self, ref: TaskRef, u: Understanding, f: Findings, tier: str) -> Composed:
        """Write the answer from the findings, then verify citations in code."""
        spec, brief = playbook_of(u)
        tickets_only = spec.key == "jira_tickets"  # the outcome is the tickets: a short answer
        ctx = await self._context(ref)
        numbered: list[dict[str, Any]] = []
        index: dict[str, int] = {}
        points = []
        for point in f.points:
            numbers = []
            for source in point["sources"]:
                if source["ref"] not in index:
                    numbered.append(source)
                    index[source["ref"]] = len(numbered)
                numbers.append(index[source["ref"]])
            points.append({"text": point["text"], "sources": numbers})
        await self.step(ref, "report", "running", "Writing the answer")
        stopped = None
        try:
            result = await self.d.gateway.complete(
                self._call(ref, "report"),
                as_tier(tier),
                system=prompts.system(
                    ctx.org_name, spec, ctx.roles, prompts.REPORT_TICKETS if tickets_only else prompts.REPORT
                ),
                messages=[
                    {
                        "role": "user",
                        "content": f"<brief>{json.dumps(brief_for_model(brief))}</brief>\n"
                        f"<findings>{json.dumps({'points': points, 'gaps': f.gaps})}</findings>",
                    }
                ],
                tools=[prompts.submit_tool("submit_report", "Submit the answer.")],
                tool_choice={"type": "tool", "name": "submit_report"},
                max_tokens=2500,
            )
            raw = result.tool_input("submit_report")
            answer = str(raw.get("answer") or "").strip()
            confidence = str(raw.get("confidence") or "medium")
            model_unverified = [str(x)[:200] for x in raw.get("not_verified") or []][:10]
        except BudgetExceededError as exc:  # stop cleanly: list the findings without a model call
            stopped = str(exc)
            answer = (
                "\n".join(f"• {p['text']} " + " ".join(f"[{n}]" for n in p["sources"]) for p in points)
                or "I had not found anything yet."
            )
            confidence, model_unverified = "low", []
        answer = expand_citations(escape(redact(answer)[0]))
        # Keep only citations of known findings, renumbered in order of first use.
        order: list[int] = []
        for match in re.finditer(r"\[(\d{1,3})\]", answer):
            number = int(match.group(1))
            if 1 <= number <= len(numbered) and number not in order:
                order.append(number)
        remap = {old: new for new, old in enumerate(order, 1)}
        answer = re.sub(
            r" ?\[(\d{1,3})\]",
            lambda m: f" [{remap[int(m.group(1))]}]" if int(m.group(1)) in remap else "",
            answer,
        )
        verified = sum(1 for p in points if p["sources"])
        # The gaps are listed by code (the model is told not to repeat them); the model's own doubts
        # only when they say something else. A ticket answer keeps neither: its tickets are the point.
        gaps = [] if tickets_only else [f"Not covered: {g}"[:200] for g in f.gaps[:5]]
        doubts = (
            []
            if tickets_only
            else [d for d in model_unverified if not any(tickets.similar(d, g) for g in f.gaps)]
        )
        not_verified = [
            escape(t) for t in [*gaps, *(p["text"][:120] for p in points if not p["sources"]), *doubts]
        ]
        if points and not order:
            not_verified.append("The answer does not cite its sources.")
        sources = await self._permalinks(ref, [numbered[old - 1] for old in order])
        summary = f"{verified} of {len(points)} points cite messages that were read"
        await self.step(ref, "verify", "completed", summary)
        return Composed(answer, sources, confidence, verified, len(points), not_verified, stopped)

    async def _permalinks(self, ref: TaskRef, sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
        team, token = await self._token(ref)
        out = []
        for i, source in enumerate(sources):
            link = source.get("permalink")  # other tools give their own links (a Jira issue, a file)
            if source.get("kind", "slack") == "slack" and i < MAX_PERMALINKS:
                try:
                    body = await self.d.slack.call(
                        "chat.getPermalink",
                        token,
                        team,
                        channel=source["channel_external_id"],
                        message_ts=source["ts"],
                    )
                    link = body.get("permalink")
                except SlackError:
                    link = None
            out.append({**source, "permalink": link})
        return out

    # ------------------------------------------------------------------ act

    async def act(
        self,
        ref: TaskRef,
        u: Understanding,
        action: PlannedAction,
        n: int,
        answer: str,
        sources: list[dict[str, Any]],
    ) -> ActionOutcome:
        """The policy decides: denied, allowed now, or an approval request in the thread. An action
        marked ``ask`` (a large plan, a private source) is decided as in Ask mode."""
        if action.skip:
            return ActionOutcome("skipped", action.skip)
        ctx = await self._context(ref)
        mode = "ask" if action.ask else u.mode
        tctx, registry, policy, executor = await self._tools(ref, ctx, mode, RefBook())
        found = registry.lookup(tool_name(action.tool, action.operation))
        if found is None:
            return ActionOutcome("denied", f"{action.tool} {action.operation} is not available.")
        adapter, op = found
        args = self._args(action, answer)
        target = await adapter.resolve_target(tctx, op.name, args)
        decision = await policy.check_tool(adapter.key, op.name, target)
        await self.step(ref, "act", "running", action.summary)
        if decision.outcome == "denied":
            return ActionOutcome("denied", decision.reason)
        if target is not None:  # what is shared must be fit for the audience it is shared with
            shared = [*sources, *brief_sources(u.brief or {})]
            audience = await policy.check_output(
                Destination("channel", target.external_id, target.id, target.name),
                [Source.from_dict(s) for s in shared],
                audience=executor.audience,
            )
            if audience.deliver != "as_is":
                return ActionOutcome("denied", f"Not shared in #{target.name}: {audience.reason}")
        if decision.outcome == "allowed":
            return await self.execute(ref, u, action, n, answer)
        expires = self.d.now() + timedelta(hours=self.d.approval_ttl_hours)
        preview = self._preview(action, args, target)
        async with tenant_tx(self.d.pool, ref.org_id) as conn:
            row = await (
                await conn.execute(
                    "select id, expires_at from app.approvals where task_id = %s and action_summary = %s "
                    "and status = 'pending' order by created_at desc limit 1",
                    (ref.task_id, preview[:2000]),
                )
            ).fetchone()
            if row is None:
                row = await (
                    await conn.execute(
                        "insert into app.approvals "
                        "(org_id, task_id, action_summary, action_tier, approver_rule, "
                        "expires_at) values (%s, %s, %s, %s, %s, %s) returning id, expires_at",
                        (
                            ref.org_id,
                            ref.task_id,
                            preview[:2000],
                            decision.tier,
                            decision.approver_rule,
                            expires,
                        ),
                    )
                ).fetchone()
        assert row is not None
        approval_id, expires_at = str(row[0]), row[1]
        sink = await self._sink(ref)
        ts = await post_once(
            self.d.pool,
            sink,
            ref.org_id,
            ref.task_id,
            f"task:{ref.task_id}:approval:{n}",
            ApprovalRequest(
                ref.channel,
                ref.thread_ts,
                preview,
                approval_id,
                str(decision.tier),
                f"Expires {expires_at:%Y-%m-%d %H:%M} UTC · {approver_text(decision.approver_rule)}",
            ),
            channel=ref.channel,
            thread_ts=ref.thread_ts,
            operation="post_approval",
        )
        async with tenant_tx(self.d.pool, ref.org_id) as conn:
            await conn.execute(
                "update app.approvals set message_ref = %s where id = %s",
                (f"{ref.channel}:{ts}", approval_id),
            )
            await conn.execute(
                "update app.tasks set status = 'awaiting_approval' where id = %s and status = 'running'",
                (ref.task_id,),
            )
        return ActionOutcome("awaiting_approval", preview, approval_id, expires_at.isoformat())

    @staticmethod
    def _args(action: PlannedAction, answer: str) -> dict[str, Any]:
        args = dict(action.args)
        if isinstance(args.get("text"), str):
            args["text"] = args["text"].replace("{answer}", answer)
        return args

    @staticmethod
    def _preview(action: PlannedAction, args: dict[str, Any], target: Target | None) -> str:
        if action.tool == "jira":
            return tickets.preview(action, args)
        if action.tool == "github":
            return f"{action.summary}:\n" + "\n".join(f"> {escape(e)}" for e in action.evidence[:8])
        where = f"#{target.name}" if target else str(args.get("channel"))
        text = str(args.get("text") or "")
        quoted = "\n".join(f"> {line}" for line in text[:700].splitlines())
        return f"{action.summary} ({where}):\n{quoted}" + ("\n> …" if len(text) > 700 else "")

    async def resolve_approval(self, ref: TaskRef, approval_id: str) -> ApprovalState:
        """The decision as the database has it; a pending approval past its expiry is expired."""
        async with tenant_tx(self.d.pool, ref.org_id) as conn:
            row = await (
                await conn.execute(
                    "select status, decided_by, expires_at <= now(), message_ref, action_summary "
                    "from app.approvals where id = %s",
                    (approval_id,),
                )
            ).fetchone()
            if row is None:
                return ApprovalState("expired")
            status = row[0]
            if status == "pending" and row[2]:
                await conn.execute(
                    "update app.approvals set status = 'expired' where id = %s and status = 'pending'",
                    (approval_id,),
                )
                status = "expired"
            if status != "pending":
                await conn.execute(
                    "update app.tasks set status = 'running' where id = %s and status = 'awaiting_approval'",
                    (ref.task_id,),
                )
        if status == "expired" and row[3]:
            await self._mark_approval(ref, row[3], row[4], "⌛ Expired: nothing was done.")
        return ApprovalState(status, str(row[1]) if row[1] else None)

    async def _mark_approval(self, ref: TaskRef, message_ref: str, summary: str, outcome: str) -> None:
        channel, _, ts = message_ref.rpartition(":")
        team, token = await self._token(ref)
        try:
            await self.d.slack.call(
                "chat.update",
                token,
                team,
                channel=channel,
                ts=ts,
                text=outcome,
                blocks=[
                    {"type": "section", "text": {"type": "mrkdwn", "text": f"{summary}"[:2900]}},
                    {"type": "context", "elements": [{"type": "mrkdwn", "text": outcome}]},
                ],
            )
        except SlackError:
            log.warning("could not update the approval message", exc_info=True)

    async def execute(
        self, ref: TaskRef, u: Understanding, action: PlannedAction, n: int, answer: str
    ) -> ActionOutcome:
        """Perform an allowed or approved write once (idempotency key in ``external_actions``),
        re-checking the requester's access at execution time."""
        ctx = await self._context(ref)
        tctx, registry, _, executor = await self._tools(ref, ctx, u.mode, RefBook())
        name = tool_name(action.tool, action.operation)
        found = registry.lookup(name)
        if found is None:
            return ActionOutcome("denied", f"{action.tool} {action.operation} is not available.")
        args = self._args(action, answer)
        target = await found[0].resolve_target(tctx, found[1].name, args)
        key = f"task:{ref.task_id}:action:{n}"
        async with tenant_tx(self.d.pool, ref.org_id) as conn:
            row = await (
                await conn.execute(
                    "select status, target_ref from app.external_actions where idempotency_key = %s", (key,)
                )
            ).fetchone()
        if row is not None and row[0] == "succeeded":
            return ActionOutcome("done", f"Done: {action.summary}", result={"target": row[1]})
        if row is None and name == tool_name("jira", "create_issue"):
            # Someone may have filed it while the approval waited. A retry (row present) skips this:
            # the issue found by its label is our own.
            same = await tickets.duplicate_of(executor, str(args.get("project")), str(args.get("summary")))
            if same is not None:
                return ActionOutcome(
                    "skipped",
                    f'Not created: "{escape(str(args.get("summary")))}" looks like '
                    f"{tickets.issue_link(same)}, opened meanwhile.",
                )
        async with tenant_tx(self.d.pool, ref.org_id) as conn:
            if row is None:
                await conn.execute(
                    "insert into app.external_actions "
                    "(org_id, task_id, integration_key, operation, target_ref, "
                    "idempotency_key) values (%s, %s, %s, %s, %s, %s)",
                    (
                        ref.org_id,
                        ref.task_id,
                        action.tool,
                        action.operation,
                        target.external_id if target else None,
                        key,
                    ),
                )
        # The executor re-checks the policy and the requester's access at execution time.
        result = await executor.run(name, {**args, "_idempotency_key": key}, approved=True)
        if result.denied:
            async with tenant_tx(self.d.pool, ref.org_id) as conn:
                await conn.execute(
                    "update app.external_actions set status = 'failed', error = %s "
                    "where idempotency_key = %s",
                    (result.text[:500], key),
                )
            return ActionOutcome("denied", result.text.removeprefix("Access denied: "))
        async with tenant_tx(self.d.pool, ref.org_id) as conn:
            await conn.execute(
                "update app.external_actions "
                "set status = %s, error = %s, performed_at = now(), target_ref = %s "
                "where idempotency_key = %s",
                (
                    "failed" if result.is_error else "succeeded",
                    result.text[:500] if result.is_error else None,
                    _target_ref(result.data),
                    key,
                ),
            )
        if result.is_error:
            return ActionOutcome("failed", f"{action.summary}: {result.text}")
        if name == tool_name("github", "push_changes") and result.data.get("number"):
            # The pull request belongs to this task: GitHub's webhooks find the thread through it.
            async with tenant_tx(self.d.pool, ref.org_id) as conn:
                await conn.execute(
                    "insert into app.task_links (org_id, task_id, kind, external_id, url) "
                    "values (%s, %s, 'pull_request', %s, %s) on conflict do nothing",
                    (
                        ref.org_id,
                        ref.task_id,
                        f"{result.data['repo']}#{result.data['number']}",
                        result.data["url"],
                    ),
                )
            await self.step(ref, "act", "completed", action.summary)
            return ActionOutcome("done", f"Opened {result.data['url']}", result=result.data)
        await self.step(ref, "act", "completed", action.summary)
        if result.data.get("url") and result.data.get("key"):
            issue = tickets.issue_link(result.data)
            done = (
                f"Created {issue}: {escape(str(args.get('summary')))}"
                if action.operation == "create_issue"
                else (f"{action.summary} ({issue})")
            )
            return ActionOutcome("done", done, result=result.data)
        return ActionOutcome("done", f"Done: {action.summary}", result=result.data)

    async def move_tickets(
        self,
        ref: TaskRef,
        u: Understanding,
        keys: list[str],
        status: str,
        link: str | None = None,
        approved: bool = False,
    ) -> list[str]:
        """A change's tickets follow it: each moves to ``status`` and, with ``link`` (its pull
        request), gets a comment linking it; each update is an external write with its own key.
        ``approved``: a person approved the work these updates are part of (the push, whose request
        listed them). Otherwise they are made only where the policy allows them without asking
        (Auto) and skipped in Ask. Returns the notes for the thread."""
        keys = [k for k in dict.fromkeys(keys) if k][:MAX_TICKETS]
        if not keys:
            return []
        ctx = await self._context(ref)
        _, registry, policy, _ = await self._tools(ref, ctx, u.mode, RefBook())
        if registry.lookup(tool_name("jira", "transition_issue")) is None:
            return []
        if not approved and (await policy.check_tool("jira", "transition_issue", None)).outcome != "allowed":
            return []
        base = TICKET_ACTIONS + (TICKET_ACTIONS // 2 if link else 0)  # the push is action 1
        moved: list[str] = []
        notes: list[str] = []
        for i, key in enumerate(keys):
            move = PlannedAction(
                "jira", "transition_issue", {"key": key, "status": status}, f"Move {key} to {status}"
            )
            outcome = await self.execute(ref, u, move, base + i, "")
            if outcome.status != "done":
                notes.append(f"⚠️ {key} stayed where it was: {outcome.message[:200]}")
                continue
            moved.append(key)
            if link:
                text = f"The agent opened a pull request for this issue: {link}"
                comment = PlannedAction(
                    "jira", "add_comment", {"key": key, "text": text}, f"Link {link} on {key}"
                )
                await self.execute(ref, u, comment, base + MAX_TICKETS + i, "")
        if moved:
            linked = " with a link to the pull request" if link else ""
            notes.insert(0, f"🎫 {', '.join(moved)} moved to {status}{linked}.")
        return notes

    # ------------------------------------------------------------------ deliver

    async def deliver(self, ref: TaskRef, u: Understanding, c: Composed, notes: list[str]) -> Delivered:
        """The output policy check, then the answer in the thread or, when it uses sources not
        everyone here can read, privately to the requester with a note in the thread."""
        ctx = await self._context(ref)
        _, _, policy, executor = await self._tools(ref, ctx, u.mode, RefBook())
        body = render_answer(c, notes)
        sink = await self._sink(ref)
        if ref.channel_type == "im":
            ts = await self.reply(ref, body, "answer")
            where = "dm"
        else:
            sources = [Source.from_dict(s) for s in c.sources]
            destination = Destination("thread", ref.channel, None, ctx.channel_name)
            decision = await policy.check_output(destination, sources, audience=executor.audience)
            if decision.deliver == "as_is":
                ts = await self.reply(ref, body, "answer")
                where = "thread"
            else:
                ts = await post_once(
                    self.d.pool,
                    sink,
                    ref.org_id,
                    ref.task_id,
                    f"task:{ref.task_id}:answer-dm",
                    DirectMessage(ref.slack_user_id, f"Your answer from #{ctx.channel_name}:\n\n{body}", []),
                    channel=ref.slack_user_id,
                    thread_ts=None,
                )
                await self.reply(ref, f"I sent you the answer privately: {decision.reason}", "answer-note")
                where = "private"
        await self._artifact(
            ref,
            "summary",
            {
                "answer": c.answer,
                "sources": c.sources,
                "delivered": where,
                "verified": c.verified,
                "total": c.total,
            },
            [s.get("permalink") or s["ref"] for s in c.sources],
        )
        await self.step(ref, "report", "completed", f"Answer delivered ({where})")
        return Delivered(where, ts)


# ------------------------------------------------------------------ helpers


CITATION_RANGE = re.compile(
    r"\[(\d{1,3})\]\s*[-\u2013\u2014]\s*\[(\d{1,3})\]|\[(\d{1,3})\s*[-\u2013\u2014]\s*(\d{1,3})\]"
)
CITATION_LIST = re.compile(r"\[(\d{1,3}(?:\s*,\s*\d{1,3})+)\]")
MAX_RANGE = 40


def expand_citations(text: str) -> str:
    """Ranges (``[4]-[9]``, ``[4-9]``) and lists (``[1, 2]``) as one citation each (``[4] [5] ... [9]``),
    so every source a sentence draws on is kept, not only the two ends of a range."""

    def each(numbers: list[int]) -> str:
        return " ".join(f"[{n}]" for n in numbers)

    def expand_range(m: re.Match[str]) -> str:
        first, last = (int(m.group(1)), int(m.group(2))) if m.group(1) else (int(m.group(3)), int(m.group(4)))
        if not first <= last <= first + MAX_RANGE:
            return m.group(0)
        return each(list(range(first, last + 1)))

    text = CITATION_RANGE.sub(expand_range, text)
    return CITATION_LIST.sub(lambda m: each([int(n) for n in m.group(1).split(",")]), text)


def _target_ref(data: dict[str, Any]) -> str | None:
    """What a write produced: a tool's own reference (``jira:PAY-7``), or a Slack channel and ts."""
    if not data:
        return None
    if data.get("target_ref"):
        return str(data["target_ref"])
    return f"{data.get('channel')}:{data.get('ts')}"


def playbook_of(u: Understanding) -> tuple[PlaybookSpec, dict[str, Any]]:
    if not u.playbook_key or not u.playbook_version or u.brief is None:
        raise ValueError("the task has no playbook or brief")
    return BUILTIN[(u.playbook_key, u.playbook_version)], u.brief


def approver_text(rule: str | None) -> str:
    return {
        "requester": "the requester decides",
        "admin": "an admin decides",
        "requester_or_admin": "the requester or an admin decides",
    }.get(rule or "", "")


def render_answer(c: Composed, notes: list[str]) -> str:
    parts = [c.answer]
    if notes:
        parts.append("\n".join(notes))
    if c.not_verified:
        parts.append("_Not verified: " + "; ".join(c.not_verified[:5]) + "_")
    if c.sources:
        refs = []
        for i, s in enumerate(c.sources, 1):
            label = s.get("label") or f"#{s.get('channel_name')}"
            refs.append(f"[{i}] <{s['permalink']}|{label}>" if s.get("permalink") else f"[{i}] {label}")
        parts.append("*Sources* " + " · ".join(refs))
    if c.stopped:
        parts.append(f"_I stopped early: {c.stopped}._")
    return "\n\n".join(p for p in parts if p)


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        when = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=UTC)


def clean_brief(raw: dict[str, Any], refs: RefBook, now: datetime) -> dict[str, Any]:
    """Validate the model's brief: known refs only (turned into sources), sane time window,
    bounded sizes. ``_sources`` keeps every source the brief cites."""
    cited: dict[str, dict[str, Any]] = {}

    def items(name: str) -> list[dict[str, Any]]:
        out = []
        for item in (raw.get(name) or [])[:20]:
            if not isinstance(item, dict) or not str(item.get("text", "")).strip():
                continue
            sources = []
            for r in item.get("sources") or []:
                source = refs.get(str(r))
                if source is not None:
                    cited[source.ref] = source.to_dict()
                    sources.append(source.ref)
            entry = {"text": redact(str(item["text"]))[0][:500], "sources": sources}
            if item.get("by"):
                entry["by"] = str(item["by"])[:100]
            out.append(entry)
        return out

    raw_entities = raw.get("entities")
    entities: dict[str, Any] = raw_entities if isinstance(raw_entities, dict) else {}
    raw_window = entities.get("time_window")
    window: dict[str, Any] = raw_window if isinstance(raw_window, dict) else {}
    start, end = _parse_time(window.get("start")), _parse_time(window.get("end"))
    if end is not None and end > now:
        end = now
    if start is not None and start > (end or now):
        start, end = None, None
    if start is not None and start < now - timedelta(days=MAX_WINDOW_DAYS):
        start = now - timedelta(days=MAX_WINDOW_DAYS)
    channels = []
    for c in entities.get("channels") or []:
        name = re.sub(r"[^a-z0-9_-]", "", str(c).lower().lstrip("#"))[:80]
        if name and name not in channels:
            channels.append(name)
    brief = {
        "goal": redact(str(raw.get("goal") or ""))[0][:500],
        "facts": items("facts"),
        "constraints": items("constraints"),
        "decisions": items("decisions"),
        "open_questions": items("open_questions"),
        "entities": {
            "channels": channels[:10],
            "people": [str(p)[:100] for p in entities.get("people") or []][:20],
            "ids": [str(i)[:60] for i in entities.get("ids") or []][:20],
            "repositories": [str(r)[:200] for r in entities.get("repositories") or []][:10],
            "files": [str(f)[:300] for f in entities.get("files") or []][:20],
            "time_window": {
                "start": start.isoformat() if start else None,
                "end": end.isoformat() if end else None,
                "label": str(window.get("label") or ("any time" if start is None else ""))[:60],
            },
        },
    }
    brief["_sources"] = list(cited.values())
    return brief


def brief_sources(brief: dict[str, Any]) -> list[dict[str, Any]]:
    return list(brief.get("_sources") or [])


def brief_for_model(brief: dict[str, Any]) -> dict[str, Any]:
    """The brief without internal fields; time window as start/end for the tools."""
    out = {k: v for k, v in brief.items() if not k.startswith("_")}
    window = (out.get("entities") or {}).get("time_window") or {}
    if window.get("start") or window.get("end"):
        out.setdefault("entities", {})["time_window"] = window
    return out
