"""Channel adapter (spec T1): the agent emits platform neutral intents; this renders them as Slack
Block Kit and sends them through the rate limited client.

Slack limits respected here: section text up to 3000 characters, 50 blocks per message, message
text up to 40000 characters. Long output is split into several sections; reports longer than one
message are split across messages in the thread.
"""

from dataclasses import dataclass, field
from typing import Any

from app.slack.client import SlackClient

SECTION_LIMIT = 3000
BLOCK_LIMIT = 50
FALLBACK_TEXT_LIMIT = 3000  # notification text; the blocks carry the full content


def escape(text: str) -> str:
    """Text written by the model or a person, for mrkdwn: ``&`` and ``<`` escaped, so it cannot form
    mentions (``<!channel>``, ``<@U...>``), links or channel references. ``>`` (quotes) is kept."""
    return text.replace("&", "&amp;").replace("<", "&lt;")


@dataclass(frozen=True)
class Reply:
    channel: str
    text: str
    thread_ts: str | None = None


@dataclass(frozen=True)
class Ephemeral:
    channel: str
    user: str
    text: str
    thread_ts: str | None = None


@dataclass(frozen=True)
class DirectMessage:
    user: str
    text: str
    actions: list[tuple[str, str, str]] = field(default_factory=list)  # (action_id, label, value)
    style_first: str | None = "primary"


@dataclass(frozen=True)
class Progress:
    """One status message per task, created once and edited in place (spec 10)."""

    channel: str
    text: str
    thread_ts: str | None = None
    ts: str | None = None  # set to update an existing status message
    actions: list[tuple[str, str, str]] = field(default_factory=list)  # (action_id, label, value)


@dataclass(frozen=True)
class ApprovalRequest:
    channel: str
    thread_ts: str | None
    summary: str
    approval_id: str
    tier: str
    expires_text: str


@dataclass(frozen=True)
class Report:
    channel: str
    title: str
    sections: list[tuple[str, str]]
    thread_ts: str | None = None
    footer: str | None = None


@dataclass(frozen=True)
class Prompt:
    """A message asking the person something, with buttons (a schedule to confirm)."""

    channel: str
    text: str
    thread_ts: str | None = None
    actions: list[tuple[str, str, str]] = field(default_factory=list)  # (action_id, label, value)


Intent = Reply | Ephemeral | DirectMessage | Progress | ApprovalRequest | Report | Prompt


def split_text(text: str, limit: int = SECTION_LIMIT) -> list[str]:
    """Split on paragraph, then line, then hard boundaries so each chunk fits a section."""
    chunks: list[str] = []
    rest = text.strip()
    while len(rest) > limit:
        cut = rest.rfind("\n\n", 0, limit)
        if cut < limit // 2:
            cut = rest.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = rest.rfind(" ", 0, limit)
        if cut <= 0:
            cut = limit
        chunks.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip()
    if rest:
        chunks.append(rest)
    return chunks or [""]


def section(text: str) -> dict[str, Any]:
    return {"type": "section", "text": {"type": "mrkdwn", "text": text}}


def text_blocks(text: str) -> list[dict[str, Any]]:
    return [section(chunk) for chunk in split_text(text)]


def fallback(text: str) -> str:
    return text if len(text) <= FALLBACK_TEXT_LIMIT else text[: FALLBACK_TEXT_LIMIT - 1] + "…"


def render(intent: Intent) -> list[tuple[str, dict[str, Any]]]:
    """Slack API calls (method, params) for an intent. Usually one; long reports use several."""
    if isinstance(intent, Reply):
        blocks = text_blocks(intent.text)
        return [
            (
                "chat.postMessage",
                {
                    "channel": intent.channel,
                    "thread_ts": intent.thread_ts,
                    "text": fallback(intent.text),
                    "blocks": part,
                },
            )
            for part in _batches(blocks)
        ]
    if isinstance(intent, Ephemeral):
        return [
            (
                "chat.postEphemeral",
                {
                    "channel": intent.channel,
                    "user": intent.user,
                    "thread_ts": intent.thread_ts,
                    "text": fallback(intent.text),
                    "blocks": text_blocks(intent.text)[:BLOCK_LIMIT],
                },
            )
        ]
    if isinstance(intent, DirectMessage):
        blocks = text_blocks(intent.text)
        if intent.actions:
            elements = []
            for index, (action_id, label, value) in enumerate(intent.actions):
                button: dict[str, Any] = {
                    "type": "button",
                    "action_id": action_id,
                    "text": {"type": "plain_text", "text": label},
                    "value": value,
                }
                if index == 0 and intent.style_first:
                    button["style"] = intent.style_first
                elements.append(button)
            blocks.append({"type": "actions", "elements": elements})
        return [
            (
                "chat.postMessage",
                {"channel": intent.user, "text": fallback(intent.text), "blocks": blocks[:BLOCK_LIMIT]},
            )
        ]
    if isinstance(intent, Progress):
        progress_blocks: list[dict[str, Any]] = [
            {"type": "context", "elements": [{"type": "mrkdwn", "text": intent.text[:SECTION_LIMIT]}]}
        ]
        if intent.actions:
            progress_blocks.append(
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "action_id": a,
                            "text": {"type": "plain_text", "text": label},
                            "value": v,
                        }
                        for a, label, v in intent.actions
                    ],
                }
            )
        params = {"channel": intent.channel, "text": fallback(intent.text), "blocks": progress_blocks}
        if intent.ts:
            return [("chat.update", {**params, "ts": intent.ts})]
        return [("chat.postMessage", {**params, "thread_ts": intent.thread_ts})]
    if isinstance(intent, Prompt):
        prompt_blocks: list[dict[str, Any]] = [*text_blocks(intent.text)]
        if intent.actions:
            prompt_blocks.append(
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "action_id": a,
                            "text": {"type": "plain_text", "text": label},
                            "value": v,
                            **({"style": "primary"} if i == 0 else {}),
                        }
                        for i, (a, label, v) in enumerate(intent.actions)
                    ],
                }
            )
        return [
            (
                "chat.postMessage",
                {
                    "channel": intent.channel,
                    "thread_ts": intent.thread_ts,
                    "text": fallback(intent.text),
                    "blocks": prompt_blocks[:BLOCK_LIMIT],
                },
            )
        ]
    if isinstance(intent, ApprovalRequest):
        blocks = [
            section(f"*Approval needed* ({intent.tier.replace('_', ' ')})"),
            *text_blocks(intent.summary),
            {"type": "context", "elements": [{"type": "mrkdwn", "text": intent.expires_text}]},
            {
                "type": "actions",
                "block_id": f"approval:{intent.approval_id}",
                "elements": [
                    {
                        "type": "button",
                        "action_id": "approval_approve",
                        "style": "primary",
                        "text": {"type": "plain_text", "text": "Approve"},
                        "value": intent.approval_id,
                    },
                    {
                        "type": "button",
                        "action_id": "approval_reject",
                        "style": "danger",
                        "text": {"type": "plain_text", "text": "Reject"},
                        "value": intent.approval_id,
                    },
                ],
            },
        ]
        return [
            (
                "chat.postMessage",
                {
                    "channel": intent.channel,
                    "thread_ts": intent.thread_ts,
                    "text": fallback(f"Approval needed: {intent.summary}"),
                    "blocks": blocks[:BLOCK_LIMIT],
                },
            )
        ]
    # Report
    blocks = [{"type": "header", "text": {"type": "plain_text", "text": intent.title[:150]}}]
    for heading, body in intent.sections:
        blocks.extend(text_blocks(f"*{heading}*\n{body}" if heading else body))
    if intent.footer:
        blocks.append(
            {"type": "context", "elements": [{"type": "mrkdwn", "text": intent.footer[:SECTION_LIMIT]}]}
        )
    return [
        (
            "chat.postMessage",
            {
                "channel": intent.channel,
                "thread_ts": intent.thread_ts,
                "text": fallback(intent.title),
                "blocks": part,
            },
        )
        for part in _batches(blocks)
    ]


def _batches(blocks: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    return [blocks[i : i + BLOCK_LIMIT] for i in range(0, len(blocks), BLOCK_LIMIT)] or [[]]


class ChannelAdapter:
    def __init__(self, client: SlackClient, kv: Any = None) -> None:
        self.client = client
        self.kv = kv

    async def send(
        self, team_id: str, token: str, intent: Intent, metadata: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        """Send an intent; returns Slack's responses (``ts`` of each message posted). ``metadata``
        (Slack message metadata) goes on the first message posted: we put idempotency keys there."""
        if isinstance(intent, Progress) and intent.ts and self.kv is not None:
            return await self._coalesced_update(team_id, token, intent)
        responses = []
        for index, (method, params) in enumerate(render(intent)):
            if index == 0 and metadata and method == "chat.postMessage":
                params = {**params, "metadata": metadata}
            responses.append(await self.client.call(method, token, team_id, **params))
        return responses

    async def _coalesced_update(self, team_id: str, token: str, intent: Progress) -> list[dict[str, Any]]:
        """Updates to the same status message are coalesced: if a newer update was queued while
        this one waited for a rate limit slot, this one is skipped."""
        key = f"ap:g:slack-progress:{team_id}:{intent.channel}:{intent.ts}"
        version = await self.kv.incr(key)
        await self.kv.expire(key, 300)
        ((method, params),) = render(intent)
        if self.client.limiter is not None:
            await self.client.limiter.acquire(team_id, method)
        latest = int(await self.kv.get(key) or 0)
        if latest != version:
            return []
        return [await self.client.call(method, token, None, **params)]  # slot already taken
