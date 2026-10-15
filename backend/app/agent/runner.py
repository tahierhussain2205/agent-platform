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
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.agent import prompts
from app.agent.outbox import Sink, SlackSink, post_once
from app.agent.playbooks import BUILTIN, PlaybookSpec, keys, latest
from app.agent.types import (
    ActionOutcome,
    ApprovalState,
    BudgetCheck,
    Composed,
    Delivered,
    Findings,
    PlannedAction,
    StartResult,
    TaskRef,
    Understanding,
)
from app.db.tenant import tenant_tx
from app.llm.gateway import BudgetExceededError, CallContext, Lane, LLMGateway
from app.llm.models import Tier
from app.policy import Destination, Policy, TaskScope
from app.policy.redaction import redact
from app.slack import service
from app.slack.adapter import ApprovalRequest, ChannelAdapter, DirectMessage, Progress, Reply
from app.slack.client import SlackClient, SlackError
from app.tools.base import RefBook, Source, Target, ToolContext, definitions, tool_name
from app.tools.registry import Registry
from app.tools.slack import SlackAdapter

log = logging.getLogger(__name__)

STEP_SEQ = {"gather": 1, "plan": 2, "act": 3, "verify": 4, "report": 5}
MAX_GATHER_ROUNDS = 6
STEP_TOKEN_CAP = 150_000
LOW_CONFIDENCE = 0.6
MAX_WINDOW_DAYS = 31
MAX_PERMALINKS = 25
MAX_ACTIONS = 3
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


@dataclass
class Context:
    org_name: str
    bot_user_id: str | None
    timezone: str
    channel_name: str
    roles: list[prompts.RoleInfo]


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
        return Context(row[0], row[1], row[2], channel_name, [prompts.RoleInfo(t, d, p) for t, d, p in roles])

    def _call(self, ref: TaskRef, purpose: str) -> CallContext:
        lane: Lane = "batch" if ref.lane == "batch" else "interactive"
        return CallContext(UUID(ref.org_id), UUID(ref.membership_id), UUID(ref.task_id), purpose, lane)

    def _registry(self, ctx: Context) -> Registry:
        registry = Registry()
        registry.register(SlackAdapter(ctx.bot_user_id))
        return registry

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

    async def _tool_ctx(self, ref: TaskRef, refs: RefBook) -> ToolContext:
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
            ahead = await (
                await conn.execute(
                    "select 1 from app.tasks where session_id = %s and id <> %s and (status in "
                    "('running', 'awaiting_approval') or (status = 'queued' and created_at < %s)) limit 1",
                    (ref.session_id, ref.task_id, task[1]),
                )
            ).fetchone()
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

    async def understand(self, ref: TaskRef, mode: str) -> Understanding:
        """Classify the request to a playbook, then turn the conversation into a brief."""
        ctx = await self._context(ref)
        refs = RefBook("B")
        tctx = await self._tool_ctx(ref, refs)
        adapter = SlackAdapter(ctx.bot_user_id)
        await self.step(ref, "gather", "running", "Understanding the request")
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
                        + "\n</conversation>",
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
                question = str(
                    raw.get("clarifying_question") or "Could you say a bit more about what you need?"
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
        from psycopg.types.json import Jsonb

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
        tctx = await self._tool_ctx(ref, refs)
        registry = self._registry(ctx)
        policy = Policy(self.d.pool, self._scope(ref, u.mode), registry)
        adapter = registry.adapters["slack"]
        tools = [
            *definitions(adapter, spec.operations("slack")),
            prompts.submit_tool("submit_findings", "Submit findings."),
        ]
        allowed_names = {t["name"] for t in tools}
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
        denied: list[str] = []
        read: list[str] = []
        used = 0
        system = prompts.system(ctx.org_name, spec, ctx.roles, prompts.GATHER)
        try:
            for round_no in range(MAX_GATHER_ROUNDS + 1):
                force = round_no == MAX_GATHER_ROUNDS or used > STEP_TOKEN_CAP
                result = await self.d.gateway.complete(
                    self._call(ref, "gather"),
                    as_tier(tier),
                    system=system,
                    messages=messages,
                    tools=tools,
                    tool_choice={"type": "tool", "name": "submit_findings"} if force else {"type": "auto"},
                    max_tokens=2500,
                )
                used += result.usage.counted
                calls = result.tool_uses()
                submitted = next((c for c in calls if c.get("name") == "submit_findings"), None)
                if submitted is not None:
                    findings = self._findings(submitted.get("input") or {}, refs, denied, read)
                    where = ", ".join("#" + c for c in read) or "no channel"
                    summary = f"{len(findings.points)} findings from {where}"
                    await self.step(ref, "gather", "completed", summary)
                    return findings
                if not calls:  # the model answered in prose: ask for the structured findings
                    messages += [
                        {"role": "assistant", "content": result.content or [{"type": "text", "text": "..."}]},
                        {"role": "user", "content": "Call submit_findings now."},
                    ]
                    continue
                results = []
                for call in calls:
                    text, is_error = await self._run_tool(
                        call, allowed_names, registry, policy, tctx, denied, read
                    )
                    results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": call["id"],
                            "content": text,
                            "is_error": is_error,
                        }
                    )
                messages += [
                    {"role": "assistant", "content": result.content},
                    {"role": "user", "content": results},
                ]
        except BudgetExceededError as exc:
            await self.step(ref, "gather", "failed", f"Stopped: {exc}")
            return Findings([], denied, read, stopped=str(exc))
        return Findings(
            [], [*denied, "The search ended before findings were submitted."], read
        )  # pragma: no cover

    async def _run_tool(
        self,
        call: dict[str, Any],
        allowed: set[str],
        registry: Registry,
        policy: Policy,
        tctx: ToolContext,
        denied: list[str],
        read: list[str],
    ) -> tuple[str, bool]:
        name = str(call.get("name"))
        raw_args = call.get("input")
        args: dict[str, Any] = raw_args if isinstance(raw_args, dict) else {}
        found = registry.lookup(name) if name in allowed else None
        if found is None:
            return f"The tool {name} is not available in this step.", True
        adapter, op = found
        target = await adapter.resolve_target(tctx, op.name, args)
        decision = await policy.check_tool(adapter.key, op.name, target)
        if not decision.allowed:
            label = f"#{target.name}" if target else str(args.get("channel") or name)
            denied.append(f"{label}: {decision.reason}")
            return f"Access denied: {decision.reason}", True
        result = await adapter.execute(tctx, op.name, args)
        if not result.is_error and target is not None and target.name not in read:
            read.append(target.name)
        if result.is_error:
            denied.append(result.text)
        return result.text, result.is_error

    @staticmethod
    def _findings(raw: dict[str, Any], refs: RefBook, denied: list[str], read: list[str]) -> Findings:
        points = []
        for point in (raw.get("points") or [])[:40]:
            if not isinstance(point, dict) or not str(point.get("text", "")).strip():
                continue
            sources = [s.to_dict() for r in point.get("sources") or [] if (s := refs.get(str(r))) is not None]
            points.append({"text": redact(str(point["text"]).strip())[0][:600], "sources": sources})
        gaps = [redact(str(g))[0][:300] for g in (raw.get("gaps") or []) if str(g).strip()][:20]
        for note in denied:  # what could not be read is always reported, whatever the model wrote
            if not any(note.split(":", 1)[0] in g for g in gaps):
                gaps.append(note[:300])
        return Findings(points, gaps, read)

    # ------------------------------------------------------------------ plan

    async def plan(self, ref: TaskRef, u: Understanding, f: Findings, tier: str) -> list[PlannedAction]:
        spec, brief = playbook_of(u)
        writes = spec.operations("slack", "write")
        if not writes:
            await self.step(ref, "plan", "skipped", "Read only playbook")
            return []
        ctx = await self._context(ref)
        await self.step(ref, "plan", "running")
        result = await self.d.gateway.complete(
            self._call(ref, "plan"),
            as_tier(tier),
            system=prompts.system(ctx.org_name, spec, ctx.roles, prompts.PLAN),
            messages=[
                {
                    "role": "user",
                    "content": f"<brief>{json.dumps(brief_for_model(brief))}</brief>\n"
                    f"<findings>{json.dumps([p['text'] for p in f.points][:20])}</findings>\n"
                    "<available_actions>"
                    + ", ".join(tool_name("slack", w) for w in writes)
                    + "</available_actions>",
                }
            ],
            tools=[prompts.submit_tool("submit_plan", "Submit the plan.")],
            tool_choice={"type": "tool", "name": "submit_plan"},
            max_tokens=800,
        )
        actions = []
        for raw in (result.tool_input("submit_plan").get("actions") or [])[:MAX_ACTIONS]:
            if not isinstance(raw, dict) or raw.get("tool") != "slack" or raw.get("operation") not in writes:
                continue
            raw_args = raw.get("args")
            args: dict[str, Any] = raw_args if isinstance(raw_args, dict) else {}
            if not str(args.get("channel") or "").strip():
                continue
            summary = str(raw.get("summary") or f"{raw['operation']} in #{args['channel']}")[:300]
            actions.append(PlannedAction("slack", str(raw["operation"]), dict(args), summary))
        await self.step(
            ref, "plan", "completed", "; ".join(a.summary for a in actions) or "No changes needed"
        )
        return actions

    # ------------------------------------------------------------------ compose and verify

    async def compose(self, ref: TaskRef, u: Understanding, f: Findings, tier: str) -> Composed:
        """Write the answer from the findings, then verify citations in code."""
        spec, brief = playbook_of(u)
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
                system=prompts.system(ctx.org_name, spec, ctx.roles, prompts.REPORT),
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
        answer, _ = redact(answer)
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
        not_verified = [p["text"][:120] for p in points if not p["sources"]] + model_unverified
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
            link = None
            if i < MAX_PERMALINKS:
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
        """The policy decides: denied, allowed now, or an approval request in the thread."""
        ctx = await self._context(ref)
        registry = self._registry(ctx)
        tctx = await self._tool_ctx(ref, RefBook())
        policy = Policy(self.d.pool, self._scope(ref, u.mode), registry)
        adapter = registry.adapters[action.tool]
        args = self._args(action, answer)
        target = await adapter.resolve_target(tctx, action.operation, args)
        decision = await policy.check_tool(action.tool, action.operation, target)
        await self.step(ref, "act", "running", action.summary)
        if decision.outcome == "denied":
            return ActionOutcome("denied", decision.reason)
        if target is not None:  # what is shared must be fit for the audience it is shared with
            shared = [*sources, *brief_sources(u.brief or {})]
            audience = await policy.check_output(
                Destination("channel", target.external_id, target.id, target.name),
                [Source.from_dict(s) for s in shared],
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
        registry = self._registry(ctx)
        tctx = await self._tool_ctx(ref, RefBook())
        policy = Policy(self.d.pool, self._scope(ref, u.mode), registry)
        adapter = registry.adapters[action.tool]
        args = self._args(action, answer)
        target = await adapter.resolve_target(tctx, action.operation, args)
        decision = await policy.check_tool(action.tool, action.operation, target)
        if decision.outcome == "denied":
            return ActionOutcome("denied", decision.reason)
        key = f"task:{ref.task_id}:action:{n}"
        async with tenant_tx(self.d.pool, ref.org_id) as conn:
            row = await (
                await conn.execute(
                    "select status, target_ref from app.external_actions where idempotency_key = %s", (key,)
                )
            ).fetchone()
            if row is not None and row[0] == "succeeded":
                return ActionOutcome("done", f"Done: {action.summary}", result={"target": row[1]})
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
        result = await adapter.execute(tctx, action.operation, {**args, "_idempotency_key": key})
        async with tenant_tx(self.d.pool, ref.org_id) as conn:
            await conn.execute(
                "update app.external_actions "
                "set status = %s, error = %s, performed_at = now(), target_ref = %s "
                "where idempotency_key = %s",
                (
                    "failed" if result.is_error else "succeeded",
                    result.text[:500] if result.is_error else None,
                    f"{result.data.get('channel')}:{result.data.get('ts')}" if result.data else None,
                    key,
                ),
            )
        if result.is_error:
            return ActionOutcome("failed", result.text)
        await self.step(ref, "act", "completed", action.summary)
        return ActionOutcome("done", f"Done: {action.summary}", result=result.data)

    # ------------------------------------------------------------------ deliver

    async def deliver(self, ref: TaskRef, u: Understanding, c: Composed, notes: list[str]) -> Delivered:
        """The output policy check, then the answer in the thread or, when it uses sources not
        everyone here can read, privately to the requester with a note in the thread."""
        ctx = await self._context(ref)
        policy = Policy(self.d.pool, self._scope(ref, u.mode), self._registry(ctx))
        body = render_answer(c, notes)
        sink = await self._sink(ref)
        if ref.channel_type == "im":
            ts = await self.reply(ref, body, "answer")
            where = "dm"
        else:
            sources = [Source.from_dict(s) for s in c.sources]
            decision = await policy.check_output(
                Destination("thread", ref.channel, None, ctx.channel_name), sources
            )
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
