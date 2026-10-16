"""Scheduled report runs (spec 4.6, 6.4): every run is checked against the owner's current access;
figures are computed by code from tool data; the model writes only the narrative, which is
verified in code; the report goes to its channel (or to the owner when that channel could not see
its sources) or by DM, once.

Raw Slack text stays inside ``compose_report`` (the activity that reads it): what it returns holds
figures, model written highlights and source references only (invariant 7).
"""

import logging
import re
from datetime import UTC, datetime
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from psycopg.types.json import Jsonb

from app import kv
from app.agent import prompts
from app.agent.outbox import SlackSink, post_once
from app.agent.playbooks import BUILTIN, latest
from app.agent.runner import AgentRunner, as_tier
from app.agent.types import Delivered, ReportComposed, ReportStart, TaskRef
from app.db.tenant import tenant_tx
from app.llm.gateway import BudgetExceededError, CallContext
from app.policy import Destination
from app.schedules.cron import Cadence
from app.schedules.figures import channel_figures, figure_lines, jira_figures, verify_narrative
from app.slack.adapter import ChannelAdapter, DirectMessage, Report, escape
from app.tools.base import RefBook, Source, tool_name

log = logging.getLogger(__name__)
REPORT_KINDS = {key for (key, _), spec in BUILTIN.items() if spec.kind == "report"}
LINE_REF = re.compile(r"^\s*(?:↳\s*)?\[([A-Z]\d+)\]")
MAX_SOURCE_LINES = 400
PAUSE_REASONS = ("owner_removed", "access_lost", "budget", "admin")
PAUSE_TEXT = {
    "owner_removed": "its owner was removed from the organization",
    "access_lost": "its owner lost access to a source or to where it is delivered",
    "budget": "a token budget is used up",
}


def window_label(start: datetime, end: datetime, timezone: str) -> str:
    zone = ZoneInfo(timezone)
    a, b = start.astimezone(zone), end.astimezone(zone)
    fmt = "%b %d %H:%M"
    return f"{a.strftime(fmt)} to {b.strftime(fmt)} ({timezone})"


class ReportRunner(AgentRunner):
    """The agent runner's context, tools, policy and outbox, for scheduled runs."""

    # ------------------------------------------------------------------ begin

    async def pause(self, org_id: str, schedule_id: str, reason: str, detail: str) -> bool:
        """Pause an active schedule with a reason (audited). False if it was not active."""
        assert reason in PAUSE_REASONS
        async with tenant_tx(self.d.pool, org_id) as conn:
            row = await (
                await conn.execute(
                    "update app.schedules set status = 'paused', pause_reason = %s, pause_detail = %s "
                    "where id = %s and status = 'active' returning name",
                    (reason, detail[:500], schedule_id),
                )
            ).fetchone()
            if row is not None:
                await conn.execute(
                    "insert into app.audit_log (org_id, actor_type, action, target, decision, reason) "
                    "values (%s, 'system', 'schedule.paused', %s, 'info', %s)",
                    (org_id, schedule_id, f"{reason}: {detail}"[:1000]),
                )
        log.info("schedule paused", extra={"schedule_id": schedule_id, "reason": reason})
        return row is not None

    async def begin(self, org_id: str, schedule_id: str, scheduled_at: str, workflow_id: str) -> ReportStart:
        """Is the schedule still active, and may its owner still run it? Then the run's task (one per
        run: keyed by the workflow id, so a retried activity finds it)."""
        async with tenant_tx(self.d.pool, org_id) as conn:
            row = await (
                await conn.execute(
                    """
                    select s.status, s.name, s.playbook_key, s.params, s.cron, s.timezone, s.delivery_type,
                           s.owner_membership_id, m.status, pi.external_user_id, w.id, w.external_id,
                           c.id, c.external_id, c.name, c.is_private, c.is_allowlisted, c.archived,
                           c.bot_is_member, coalesce(app.can_read_channel(m.id, c.id), false),
                           o.default_model_tier::text
                      from app.schedules s
                      join app.org_memberships m on m.id = s.owner_membership_id
                      join app.org_settings o on o.org_id = s.org_id
                      left join app.workspaces w on w.org_id = s.org_id and w.status = 'active'
                      left join app.platform_identities pi
                             on pi.membership_id = m.id and pi.workspace_id = w.id
                      left join app.channels c on c.id = s.delivery_channel_id
                     where s.id = %s
                    """,
                    (schedule_id,),
                )
            ).fetchone()
        if row is None or row[0] == "deleted":
            return ReportStart("skip", "The schedule was deleted.")
        if row[0] == "paused":
            return ReportStart("skip", "The schedule is paused.")
        (_, name, playbook_key, params, cron, timezone, delivery, owner, owner_status, slack_user, workspace,
         team, channel_id, channel_ext, channel_name, private, allowed, archived, bot_in, owner_in,
         tier) = row  # fmt: skip
        problem: tuple[str, str] | None = None
        if owner_status != "active":
            problem = ("owner_removed", "the owner is no longer an active member")
        elif workspace is None or slack_user is None:
            problem = ("access_lost", "the owner has no Slack account in the connected workspace")
        elif delivery == "channel" and (not allowed or archived):
            problem = ("access_lost", f"#{channel_name} is no longer allowed for the agent")
        elif delivery == "channel" and not bot_in:
            problem = ("access_lost", f"the agent is not in #{channel_name}")
        elif delivery == "channel" and not owner_in:
            problem = ("access_lost", f"the owner is not a member of #{channel_name}")
        elif playbook_key not in REPORT_KINDS:
            problem = ("access_lost", f"unknown report kind {playbook_key}")
        if problem is None:
            status = await self.d.gateway.budget_status(
                CallContext(UUID(org_id), owner, None, "schedule", "batch")
            )
            over = status.exceeded()
            if over is not None and over.scope != "task":
                problem = ("budget", str(over))
        if problem is not None:
            paused = await self.pause(org_id, schedule_id, *problem)
            return ReportStart("skip", f"Paused: {problem[1]}.", paused=paused)

        spec = latest(playbook_key)
        when = datetime.fromisoformat(scheduled_at).astimezone(UTC)
        start, end = Cadence(cron, timezone).window(when)
        async with tenant_tx(self.d.pool, org_id) as conn:
            playbook = await (
                await conn.execute(
                    "select id from app.playbooks where org_id is null and key = %s and version = %s",
                    (spec.key, spec.version),
                )
            ).fetchone()
            await conn.execute(
                "insert into app.tasks (org_id, schedule_id, requested_by, trigger, playbook_id, model_tier, "
                "workflow_id) values (%s, %s, %s, 'schedule', %s, %s, %s) "
                "on conflict (workflow_id) do nothing",
                (org_id, schedule_id, owner, playbook[0] if playbook else None, tier, workflow_id),
            )
            task = await (
                await conn.execute("select id from app.tasks where workflow_id = %s", (workflow_id,))
            ).fetchone()
        assert task is not None
        if delivery == "dm":
            team_id, token = await self._token_for(org_id)
            opened = await self.d.slack.call("conversations.open", token, team_id, users=slack_user)
            channel, channel_type, row_id = str(opened["channel"]["id"]), "im", None
        else:
            channel, channel_type, row_id = channel_ext, "group" if private else "channel", str(channel_id)
        ref = TaskRef(
            org_id=org_id,
            task_id=str(task[0]),
            session_id="",
            workspace_id=str(workspace),
            team_id=str(team),
            channel=channel,
            channel_type=channel_type,
            channel_id=row_id,
            thread_ts=None,
            message_ts="",
            membership_id=str(owner),
            slack_user_id=str(slack_user),
            trigger="schedule",
            lane="batch",
        )
        return ReportStart(
            "run",
            ref=ref,
            playbook_key=playbook_key,
            title=name,
            window_start=start.isoformat(),
            window_end=end.isoformat(),
            scheduled_at=when.isoformat(),
            params=dict(params or {}),
            delivery=delivery,
            delivery_name=channel_name or "",
            model_tier=tier,
        )

    async def _token_for(self, org_id: str) -> tuple[str, str]:
        from app.slack import service

        return await service.bot_token(self.d.pool, self.d.vault, UUID(org_id))

    # ------------------------------------------------------------------ compose

    async def compose_report(self, ref: TaskRef, start: ReportStart) -> ReportComposed:
        ctx = await self._context(ref)
        refs = RefBook("S")
        _, _, _, executor = await self._tools(ref, ctx, "ask", refs)
        spec = latest(start.playbook_key)
        window = datetime.fromisoformat(start.window_start), datetime.fromisoformat(start.window_end)
        await self.step(ref, "gather", "running", "Computing the report figures")
        lines: list[str] = []
        if start.playbook_key == "report_jira_progress":
            project = str(start.params.get("project") or "")
            result = await executor.run(
                tool_name("jira", "report_issues"), {"project": project, "since": window[0].isoformat()}
            )
            if result.denied or result.is_error:
                if result.denied or result.data.get("denied"):
                    return ReportComposed({}, [], "", [], [], denied=result.text)
                raise RuntimeError(f"Jira: {result.text}"[:300])  # retried, then the run fails
            figures = jira_figures(project, list(result.data.get("issues") or []), *window)
            lines = result.text.splitlines()[1:]
        else:
            stats = []
            for channel in await self._channels(ref, list(start.params.get("channels") or [])):
                if channel is None:
                    return ReportComposed(
                        {}, [], "", [], [], denied="a channel of this report no longer exists"
                    )
                args = {"channel": channel, "start": window[0].isoformat(), "end": window[1].isoformat()}
                result = await executor.run(tool_name("slack", "channel_stats"), args)
                if result.denied or result.is_error:  # Slack's own refusals do not fix themselves either
                    return ReportComposed({}, [], "", [], [], denied=result.text)
                stats.append(result.data)
                read = await executor.run(tool_name("slack", "read_channel"), {**args, "limit": 200})
                if not read.is_error:
                    lines += read.text.splitlines()[1:]
            figures = channel_figures(stats)
        kind = "jira_progress" if start.playbook_key == "report_jira_progress" else "channel_summary"
        rendered = figure_lines(kind, figures)
        lines = lines[:MAX_SOURCE_LINES]
        by_ref = {m.group(1): line for line in lines if (m := LINE_REF.match(line))}
        summary, dropped = "", 0
        highlights: list[dict[str, Any]] = []
        if by_ref:
            try:
                answer = await self.d.gateway.complete(
                    self._call(ref, "report"),
                    as_tier(start.model_tier),
                    system=prompts.system(ctx.org_name, spec, ctx.roles, prompts.NARRATIVE),
                    messages=[
                        {
                            "role": "user",
                            "content": f"<report>{start.title}</report>\n"
                            f"<window>{window_label(*window, ctx.timezone)}</window>\n"
                            "<figures>\n" + "\n".join(rendered) + "\n</figures>\n"
                            "<sources>\n" + "\n".join(lines) + "\n</sources>",
                        }
                    ],
                    tools=[prompts.submit_tool("submit_report_narrative", "Submit the narrative.")],
                    tool_choice={"type": "tool", "name": "submit_report_narrative"},
                    max_tokens=1500,
                )
            except BudgetExceededError as exc:
                return ReportComposed(figures, rendered, "", [], [], stopped=str(exc))
            raw = answer.tool_input("submit_report_narrative")
            summary, highlights, dropped = verify_narrative(
                str(raw.get("summary") or ""), list(raw.get("highlights") or []), figures, by_ref
            )
        else:
            summary = "No activity in this period."
        cited: list[str] = []
        for h in highlights:
            cited += [r for r in h["refs"] if r not in cited]
        found = [(r, refs.get(r)) for r in cited]
        sources = await self._permalinks(ref, [s.to_dict() for _, s in found if s is not None])
        number = {r: i + 1 for i, r in enumerate(r for r, s in found if s is not None)}
        numbered = [
            {"text": h["text"], "refs": [number[r] for r in h["refs"] if r in number]} for h in highlights
        ]
        await self._artifact(
            ref,
            "report",
            {
                "figures": figures,
                "summary": summary,
                "highlights": numbered,
                "dropped": dropped,
                "window": [start.window_start, start.window_end],
            },
            [s.get("permalink") or s["ref"] for s in sources],
        )
        await self.step(ref, "gather", "completed", f"Figures computed; {len(numbered)} highlights")
        if dropped:
            log.info("report narrative trimmed", extra={"dropped": dropped, "task_id": ref.task_id})
        return ReportComposed(figures, rendered, summary, numbered, sources, dropped)

    async def _channels(self, ref: TaskRef, ids: list[str]) -> list[str | None]:
        """External ids of the report's channels (None for one that no longer exists)."""
        async with tenant_tx(self.d.pool, ref.org_id) as conn:
            rows = dict(
                await (
                    await conn.execute(
                        "select id::text, external_id from app.channels where id = any(%s::uuid[])", (ids,)
                    )
                ).fetchall()
            )
        return [rows.get(i) for i in ids]

    # ------------------------------------------------------------------ deliver

    async def deliver_report(self, ref: TaskRef, start: ReportStart, c: ReportComposed) -> Delivered:
        """The output check against the delivery channel; when it could not see every source, the
        report goes to the owner instead, with the reason."""
        ctx = await self._context(ref)
        _, _, policy, executor = await self._tools(ref, ctx, "ask", RefBook())
        window = datetime.fromisoformat(start.window_start), datetime.fromisoformat(start.window_end)
        sections: list[tuple[str, str]] = [("", "\n".join(c.figure_lines))]
        if c.summary:
            sections.append(("", escape(c.summary)))
        if c.highlights:
            sections.append(
                (
                    "",
                    "\n".join(
                        f"• {escape(h['text'])} " + "".join(f"[{n}]" for n in h["refs"]) for h in c.highlights
                    ),
                )
            )
        footer = (
            f"Scheduled report · {window_label(*window, ctx.timezone)} · manage it in App Home or the console"
        )
        if c.sources:
            cites = " · ".join(
                f"[{i}] <{s['permalink']}|{s.get('label') or s.get('channel_name')}>"
                if s.get("permalink")
                else f"[{i}] {s.get('label') or s.get('channel_name')}"
                for i, s in enumerate(c.sources, 1)
            )
            sections.append(("Sources", cites))
        key = f"report:{ref.task_id}"  # one task per run
        sink = await self._sink(ref)
        where, channel = ("dm", ref.channel) if start.delivery == "dm" else ("channel", ref.channel)
        if start.delivery == "channel":
            row_id = UUID(ref.channel_id) if ref.channel_id else None
            destination = Destination("channel", ref.channel, row_id, start.delivery_name)
            sources = [Source.from_dict(s) for s in c.sources]
            decision = await policy.check_output(destination, sources, audience=executor.audience)
            if decision.deliver != "as_is":
                team_id, token = await self._token_for(ref.org_id)
                opened = await self.d.slack.call(
                    "conversations.open", token, team_id, users=ref.slack_user_id
                )
                where, channel, key = "private", str(opened["channel"]["id"]), f"{key}:private"
                sections.insert(
                    0, ("", f"_I couldn't post this report in #{start.delivery_name}: {decision.reason}_")
                )
        intent = Report(channel, start.title, sections, None, footer)
        ts = await post_once(
            self.d.pool, sink, ref.org_id, ref.task_id, key, intent, channel=channel, thread_ts=None
        )
        async with tenant_tx(self.d.pool, ref.org_id) as conn:
            await conn.execute(
                "update app.schedules set last_run_at = %s where id = (select schedule_id from app.tasks "
                "where id = %s)",
                (start.scheduled_at, ref.task_id),
            )
            await conn.execute(
                "update app.artifacts set content = content || %s where task_id = %s and kind = 'report'",
                (Jsonb({"delivered": where}), ref.task_id),
            )
        await self.step(ref, "report", "completed", f"Report delivered ({where})")
        return Delivered(where, ts)

    # ------------------------------------------------------------------ pause notifications

    async def notify_pauses(self, org_id: str) -> int:
        """Tell every active admin (and the owner, if they opted in) about each pause nobody was told
        about yet; admin pauses need no notice. Each DM claims a Key Value marker before it is sent
        (invariant 8; concurrent syncs send it once), then the pause is marked notified. Returns the
        pauses notified."""
        async with tenant_tx(self.d.pool, org_id) as conn:
            rows = await (
                await conn.execute(
                    """
                    select s.id, s.name, s.pause_reason, s.pause_detail, s.paused_at, u.email, m.status,
                           pi.external_user_id, coalesce('schedule_paused' = any(p.notify_on), false)
                      from app.schedules s
                      join app.org_memberships m on m.id = s.owner_membership_id
                      join app.users u on u.id = m.user_id
                      left join app.workspaces w on w.org_id = s.org_id and w.status = 'active'
                      left join app.platform_identities pi
                             on pi.membership_id = m.id and pi.workspace_id = w.id
                      left join app.user_preferences p on p.membership_id = m.id
                     where s.status = 'paused' and s.pause_notified_at is null and s.pause_reason <> 'admin'
                     order by s.paused_at
                    """
                )
            ).fetchall()
            admins = [
                r[0]
                for r in await (
                    await conn.execute(
                        "select pi.external_user_id from app.org_memberships m "
                        "join app.platform_identities pi on pi.membership_id = m.id "
                        "join app.workspaces w on w.id = pi.workspace_id and w.status = 'active' "
                        "where m.privilege = 'admin' and m.status = 'active'"
                    )
                ).fetchall()
            ]
        if not rows:
            return 0
        try:
            team, token = await self._token_for(org_id)
        except Exception:  # no Slack connection: the console still shows the pause
            log.info("schedule pauses not sent: Slack is not connected", extra={"org_id": org_id})
            team = token = ""
        sink = (
            self.d.sink_factory(team, token)
            if self.d.sink_factory is not None
            else SlackSink(ChannelAdapter(self.d.slack, self.d.kv), team, token)
        )
        link = f"<{self.d.console_url}/schedules|Schedules in the console>"
        for sid, name, reason, detail, paused_at, email, owner_status, owner_slack, opted in rows:
            why = detail or PAUSE_TEXT.get(reason, reason)
            fix = (
                "Reassign it or delete it"
                if reason == "owner_removed"
                else "Resume it once fixed, or delete it"
            )
            messages = [
                (
                    a,
                    f"⏸ The scheduled report *{escape(name)}* owned by {email} was paused: {why}. "
                    f"{fix}: {link}.",
                )
                for a in admins
            ]
            if owner_status == "active" and owner_slack and opted and owner_slack not in admins:
                messages.append(
                    (
                        owner_slack,
                        f"⏸ Your scheduled report *{escape(name)}* was paused: {why}. "
                        f"Resume it once fixed: {link}.",
                    )
                )
            for user, text in messages if token else []:
                marker = kv.tenant_key(
                    UUID(org_id), "schedule-paused", str(sid), str(paused_at.timestamp()), user
                )
                # Claimed before sending, so two syncs at once send one notice; a claim whose send
                # never happened expires soon, and the next sync sends it (at least once).
                if self.d.kv is not None and not await kv.claim(self.d.kv, marker, 120, "sending"):
                    continue
                try:
                    await sink.send(DirectMessage(user, text))
                except Exception:
                    if self.d.kv is not None:
                        await kv.release(self.d.kv, marker)
                    raise
                if self.d.kv is not None:
                    await self.d.kv.set(marker, "sent", ex=30 * 24 * 3600)
            async with tenant_tx(self.d.pool, org_id) as conn:
                await conn.execute(
                    "update app.schedules set pause_notified_at = now() where id = %s and paused_at = %s",
                    (sid, paused_at),
                )
        log.info("schedule pauses notified", extra={"org_id": org_id, "count": len(rows)})
        return len(rows)
