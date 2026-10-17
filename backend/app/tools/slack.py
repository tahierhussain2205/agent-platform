"""Slack tool adapter: read channels and threads the requester can read, and post to a channel.

Reads use the bot token; the channel rule (allowlist plus the requester's membership) is the
"check" of credential mode ``org_with_check`` and is enforced by the policy module before the
call. Message text is normalised (mentions and links made readable), capped and redacted, and
every message is cited with a short ref the model must use. Raw messages are never stored:
they live only in this call and in the model request.
"""

import html
import logging
import re
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from app import kv as kvlib
from app.db.tenant import tenant_tx
from app.policy.redaction import redact
from app.slack.client import SlackError
from app.tools.base import Operation, Source, Target, ToolContext, ToolResult, cap

log = logging.getLogger(__name__)

CHANNEL_ARG = {
    "type": "string",
    "description": "Channel name without # (for example bugs), or its id.",
}
TIME_ARG = "ISO 8601 date-time (UTC unless an offset is given)."
SKIP_SUBTYPES = {
    "channel_join",
    "channel_leave",
    "channel_topic",
    "channel_purpose",
    "bot_add",
    "pinned_item",
}
MAX_THREADS = 10
NAME_TTL = 3600
MAX_STATS_MESSAGES = 10_000
MAX_STATS_THREADS = 300

OPERATIONS = {
    "list_channels": Operation(
        "list_channels",
        "List the Slack channels you may read for the requester (allowed by the admins and the "
        "requester is a member).",
        {"type": "object", "properties": {}},
        credential_mode="org_with_check",
    ),
    "read_channel": Operation(
        "read_channel",
        "Read messages of a Slack channel in a time window, oldest first, with thread replies "
        "folded in. Each message starts with a ref like [S3]; cite refs exactly.",
        {
            "type": "object",
            "properties": {
                "channel": CHANNEL_ARG,
                "start": {"type": "string", "description": f"Window start, {TIME_ARG}"},
                "end": {"type": "string", "description": f"Window end, {TIME_ARG}"},
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 200,
                    "description": "Top level messages.",
                },
            },
            "required": ["channel"],
        },
        credential_mode="org_with_check",
    ),
    "read_thread": Operation(
        "read_thread",
        "Read one Slack thread (the parent message and its replies). Cite refs exactly.",
        {
            "type": "object",
            "properties": {
                "channel": CHANNEL_ARG,
                "ts": {"type": "string", "description": "Thread timestamp."},
            },
            "required": ["channel", "ts"],
        },
        credential_mode="org_with_check",
    ),
    "channel_stats": Operation(
        "channel_stats",
        "Counts for a report: top level messages, people, threads started and their replies in a window "
        "(the agent's own messages excluded).",
        {
            "type": "object",
            "properties": {
                "channel": CHANNEL_ARG,
                "start": {"type": "string", "description": f"Window start, {TIME_ARG}"},
                "end": {"type": "string", "description": f"Window end, {TIME_ARG}"},
            },
            "required": ["channel", "start", "end"],
        },
        credential_mode="org_with_check",
        internal=True,
    ),
    "post_message": Operation(
        "post_message",
        "Post a message in another Slack channel. Reaches people outside this conversation, so it "
        "always needs approval.",
        {
            "type": "object",
            "properties": {"channel": CHANNEL_ARG, "text": {"type": "string", "maxLength": 3000}},
            "required": ["channel", "text"],
        },
        kind="write",
        outward_facing=True,
        credential_mode="org",
    ),
}


def parse_time(value: Any) -> float | None:
    if value in (None, ""):
        return None
    when = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return when.timestamp()


def clock(ts: str) -> str:
    return datetime.fromtimestamp(float(ts), UTC).strftime("%Y-%m-%d %H:%M UTC")


def channel_key(value: Any) -> str:
    raw = str(value or "").strip()
    if m := re.fullmatch(r"<#([A-Z0-9]+)(?:\|[^>]*)?>", raw):
        return m.group(1)
    return raw.lstrip("#").strip()


class SlackAdapter:
    key = "slack"
    operations = OPERATIONS

    def __init__(self, bot_user_id: str | None = None) -> None:
        self.bot_user_id = bot_user_id

    # ------------------------------------------------------------------ targets

    async def resolve_target(self, ctx: ToolContext, operation: str, args: dict[str, Any]) -> Target | None:
        if operation == "list_channels":
            return None
        wanted = channel_key(args.get("channel"))
        if not wanted:
            return None
        async with tenant_tx(ctx.pool, ctx.org_id) as conn:
            row = await (
                await conn.execute(
                    "select id, external_id, name, is_private from app.channels where workspace_id = %s "
                    "and (upper(external_id) = upper(%s) or lower(name) = lower(%s)) "
                    "order by archived, created_at desc limit 1",
                    (ctx.workspace_id, wanted, wanted),
                )
            ).fetchone()
        if row is None:
            return None
        return Target("slack_channel", row[0], row[1], row[2] or row[1], bool(row[3]))

    # ------------------------------------------------------------------ execution

    async def execute(
        self, ctx: ToolContext, operation: str, args: dict[str, Any], credential: Any = None
    ) -> ToolResult:
        try:
            if operation == "list_channels":
                return await self._list_channels(ctx)
            target = await self.resolve_target(ctx, operation, args)
            if target is None:
                return ToolResult(
                    f"No channel named #{channel_key(args.get('channel'))} is known.", is_error=True
                )
            if operation == "read_channel":
                return await self._read_channel(ctx, target, args)
            if operation == "read_thread":
                return await self._read_thread(ctx, target, str(args.get("ts", "")))
            if operation == "channel_stats":
                return await self._channel_stats(ctx, target, args)
            if operation == "post_message":
                return await self._post(ctx, target, args)
        except SlackError as exc:
            return ToolResult(self._explain(exc, args), is_error=True)
        except ValueError as exc:
            return ToolResult(f"Invalid arguments: {exc}", is_error=True)
        return ToolResult(f"Unknown operation {operation}.", is_error=True)

    @staticmethod
    def _explain(exc: SlackError, args: dict[str, Any]) -> str:
        name = channel_key(args.get("channel"))
        return {
            "not_in_channel": f"I'm not in #{name}, so I can't read it. Add me with /invite @agent.",
            "channel_not_found": f"Slack says #{name} does not exist or I can't see it.",
            "thread_not_found": "That thread does not exist.",
            "is_archived": f"#{name} is archived.",
        }.get(exc.error, f"Slack error: {exc.error}")

    async def _list_channels(self, ctx: ToolContext) -> ToolResult:
        async with tenant_tx(ctx.pool, ctx.org_id) as conn:
            rows = await (
                await conn.execute(
                    "select name, is_private from app.channels where workspace_id = %s "
                    "and app.can_read_channel(%s, id) order by name",
                    (ctx.workspace_id, ctx.membership_id),
                )
            ).fetchall()
        if not rows:
            return ToolResult("You can't read any channel through me yet.")
        lines = [f"#{name} ({'private' if private else 'public'})" for name, private in rows]
        return ToolResult("Channels you may read:\n" + "\n".join(lines))

    async def _read_channel(self, ctx: ToolContext, target: Target, args: dict[str, Any]) -> ToolResult:
        oldest, latest = parse_time(args.get("start")), parse_time(args.get("end"))
        if oldest is not None and latest is not None and oldest > latest:
            raise ValueError("start is after end")
        limit = max(1, min(int(args.get("limit") or 100), 200))
        params: dict[str, Any] = {"channel": target.external_id}
        if oldest is not None:
            params["oldest"] = f"{oldest:.6f}"
        if latest is not None:
            params["latest"] = f"{latest:.6f}"
        top: list[dict[str, Any]] = []
        async for message in ctx.slack.paginate(
            "conversations.history", "messages", ctx.token, ctx.team_id, limit=min(limit, 100), **params
        ):
            top.append(message)
            if len(top) >= limit:
                break
        top = [m for m in top if self._keep(m)]
        top.sort(key=lambda m: float(m["ts"]))
        threads: dict[str, list[dict[str, Any]]] = {}
        for message in [m for m in top if m.get("reply_count")][-MAX_THREADS:]:
            replies = [
                r
                async for r in ctx.slack.paginate(
                    "conversations.replies",
                    "messages",
                    ctx.token,
                    ctx.team_id,
                    channel=target.external_id,
                    ts=message["ts"],
                )
            ]
            threads[message["ts"]] = [r for r in replies if r["ts"] != message["ts"] and self._keep(r)]
        window = self._window(oldest, latest)
        if not top:
            return ToolResult(f"No messages in #{target.name} {window}.")
        names = await self._names(ctx, [*top, *(r for rs in threads.values() for r in rs)])
        lines = [f"Messages in #{target.name} {window} ({len(top)} top level):"]
        for message in top:
            lines.append(self._line(ctx, target, message, names))
            lines += ["  ↳ " + self._line(ctx, target, r, names) for r in threads.get(message["ts"], [])]
        return self._result(lines)

    async def _channel_stats(self, ctx: ToolContext, target: Target, args: dict[str, Any]) -> ToolResult:
        """Exact counts over the whole window (not the capped read the model gets). Only numbers and
        user ids leave this function, never message text."""
        oldest, latest = parse_time(args.get("start")), parse_time(args.get("end"))
        if oldest is None or latest is None or oldest > latest:
            raise ValueError("a window with a start before its end is required")
        top: list[dict[str, Any]] = []
        truncated = False
        async for message in ctx.slack.paginate(
            "conversations.history",
            "messages",
            ctx.token,
            ctx.team_id,
            limit=200,
            channel=target.external_id,
            oldest=f"{oldest:.6f}",
            latest=f"{latest:.6f}",
        ):
            if self._keep(message):
                top.append(message)
            if len(top) >= MAX_STATS_MESSAGES:
                truncated = True
                break
        people = {str(m["user"]) for m in top if m.get("user")}
        threads = [m for m in top if m.get("reply_count")]
        if len(threads) > MAX_STATS_THREADS:
            threads, truncated = threads[:MAX_STATS_THREADS], True
        replies = 0
        for parent in threads:
            async for reply in ctx.slack.paginate(
                "conversations.replies", "messages", ctx.token, ctx.team_id,
                channel=target.external_id, ts=parent["ts"],
            ):  # fmt: skip
                if reply["ts"] != parent["ts"] and self._keep(reply):
                    replies += 1
                    if reply.get("user"):
                        people.add(str(reply["user"]))
        data = {
            "channel": target.name,
            "channel_id": target.external_id,
            "messages": len(top),
            "threads": len(threads),
            "replies": replies,
            "people": sorted(people),
            "truncated": truncated,
        }
        text = (
            f"#{target.name}: {len(top)} messages, {len(threads)} threads with {replies} replies, "
            f"{len(people)} people"
        )
        return ToolResult(text, data=data)

    async def _read_thread(self, ctx: ToolContext, target: Target, ts: str) -> ToolResult:
        if not re.fullmatch(r"\d{6,12}\.\d{1,6}", ts):
            raise ValueError("ts must be a Slack timestamp like 1700000000.000100")
        messages = [
            m
            async for m in ctx.slack.paginate(
                "conversations.replies", "messages", ctx.token, ctx.team_id, channel=target.external_id, ts=ts
            )
        ]
        messages = [m for m in messages if self._keep(m)]
        if not messages:
            return ToolResult(f"The thread in #{target.name} has no readable messages.")
        names = await self._names(ctx, messages)
        lines = [f"Thread in #{target.name} ({len(messages)} messages):"]
        lines += [("  ↳ " if i else "") + self._line(ctx, target, m, names) for i, m in enumerate(messages)]
        return self._result(lines)

    async def _post(self, ctx: ToolContext, target: Target, args: dict[str, Any]) -> ToolResult:
        text, _ = redact(str(args.get("text", "")).strip())
        if not text:
            raise ValueError("text is empty")
        key = str(args.get("_idempotency_key") or "")
        if key:  # a retried attempt finds the post that succeeded before a crash
            recent = await ctx.slack.call(
                "conversations.history", ctx.token, ctx.team_id, channel=target.external_id,
                include_all_metadata=True, limit=50,
            )  # fmt: skip
            for message in recent.get("messages", []):
                if ((message.get("metadata") or {}).get("event_payload") or {}).get("idempotency_key") == key:
                    data = {"channel": target.external_id, "ts": message["ts"], "existing": True}
                    return ToolResult(f"Posted in #{target.name}.", data=data)
        metadata = {"event_type": "agent_action", "event_payload": {"idempotency_key": key}} if key else None
        body = await ctx.slack.call(
            "chat.postMessage", ctx.token, ctx.team_id, channel=target.external_id, text=text[:3000],
            unfurl_links=False, metadata=metadata,
        )  # fmt: skip
        return ToolResult(
            f"Posted in #{target.name}.", data={"channel": target.external_id, "ts": body.get("ts")}
        )

    # ------------------------------------------------------------------ helpers

    def _keep(self, message: dict[str, Any]) -> bool:
        if message.get("subtype") in SKIP_SUBTYPES:
            return False
        return not (self.bot_user_id and message.get("user") == self.bot_user_id)

    @staticmethod
    def _window(oldest: float | None, latest: float | None) -> str:
        if oldest is None and latest is None:
            return "(most recent)"
        start = clock(f"{oldest}") if oldest is not None else "the beginning"
        end = clock(f"{latest}") if latest is not None else "now"
        return f"from {start} to {end}"

    def _line(self, ctx: ToolContext, target: Target, message: dict[str, Any], names: dict[str, str]) -> str:
        source = Source(
            ref=f"slack:{target.external_id}:{message['ts']}",
            channel_external_id=target.external_id,
            channel_name=target.name,
            visibility=target.visibility,
            ts=message["ts"],
            label=f"#{target.name}, {clock(message['ts'])}",
        )
        short = ctx.refs.add(source)
        who = names.get(message.get("user", ""), message.get("username") or message.get("user") or "someone")
        return f"[{short}] ({clock(message['ts'])}) {who}: {plain(message.get('text', ''), names)}"

    async def transcript(self, ctx: ToolContext, target: Target, messages: list[dict[str, Any]]) -> list[str]:
        """Lines with refs for a conversation (oldest first), as the model sees tool output."""
        kept = sorted((m for m in messages if self._keep(m)), key=lambda m: float(m["ts"]))
        names = await self._names(ctx, kept)
        return [redact(self._line(ctx, target, m, names))[0] for m in kept]

    @staticmethod
    def _result(lines: list[str]) -> ToolResult:
        text, _ = redact("\n".join(lines))
        return ToolResult(cap(text))

    async def _names(self, ctx: ToolContext, messages: list[dict[str, Any]]) -> dict[str, str]:
        ids: set[str] = set()
        for m in messages:
            if m.get("user"):
                ids.add(m["user"])
            ids.update(re.findall(r"<@([A-Z0-9]+)(?:\|[^>]*)?>", m.get("text", "")))
        names: dict[str, str] = {}
        for uid in sorted(ids):
            names[uid] = await self._name(ctx, uid)
        # Channel links can come without a name (<#C0C9S2Y6Z1B> or <#C0C9S2Y6Z1B|>): ours has it.
        channels = {c for m in messages for c in re.findall(r"<#([A-Z0-9]+)(?:\|[^>]*)?>", m.get("text", ""))}
        if channels:
            async with tenant_tx(ctx.pool, ctx.org_id) as conn:
                rows = await (
                    await conn.execute(
                        "select external_id, name from app.channels where workspace_id = %s "
                        "and external_id = any(%s) and name is not null",
                        (ctx.workspace_id, sorted(channels)),
                    )
                ).fetchall()
            names.update({f"#{row[0]}": str(row[1]) for row in rows})
        return names

    async def _name(self, ctx: ToolContext, uid: str) -> str:
        key = kvlib.tenant_key(UUID(str(ctx.org_id)), "slack-user", uid) if ctx.kv is not None else None
        if key:
            cached = await ctx.kv.get(key)
            if cached:
                return cached.decode() if isinstance(cached, bytes) else str(cached)
        try:
            user = (await ctx.slack.call("users.info", ctx.token, ctx.team_id, user=uid))["user"]
        except SlackError:
            return uid
        profile = user.get("profile") or {}
        name = (
            profile.get("real_name")
            or profile.get("display_name")
            or user.get("real_name")
            or user.get("name")
            or uid
        )
        if key:
            await ctx.kv.set(key, name, ex=NAME_TTL)
        return str(name)


def plain(text: str, names: dict[str, str]) -> str:
    """Slack markup to readable text: mentions, channel links, special mentions and URLs."""
    text = re.sub(
        r"<@([A-Z0-9]+)(?:\|([^>]*))?>", lambda m: "@" + names.get(m.group(1), m.group(2) or m.group(1)), text
    )
    text = re.sub(
        r"<#([A-Z0-9]+)(?:\|([^>]*))?>",
        lambda m: "#" + (m.group(2) or names.get(f"#{m.group(1)}") or m.group(1)),
        text,
    )
    text = re.sub(r"<!(here|channel|everyone)[^>]*>", r"@\1", text)
    text = re.sub(r"<(https?://[^|>]+)\|([^>]+)>", r"\2 (\1)", text)
    text = re.sub(r"<(https?://[^>]+)>", r"\1", text)
    return html.unescape(text).replace("\n", " ⏎ ")
