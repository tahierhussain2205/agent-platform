"""Where the agent's messages go, sent once (invariant 8).

Every post made for a task is an external write with an idempotency key in ``external_actions``,
checked before acting. The key also travels in Slack message metadata, so an attempt that
crashed after Slack accepted the post (the row still ``pending``) finds that message instead of
posting again.

``SlackSink`` sends through the channel adapter; ``CaptureSink`` records intents instead (the
eval harness and tests use it, so nothing reaches Slack).
"""

import itertools
import logging
from dataclasses import dataclass, field
from typing import Any, Protocol
from uuid import UUID

from app.db.tenant import tenant_tx
from app.slack.adapter import ChannelAdapter, Intent, Progress

log = logging.getLogger(__name__)


class Sink(Protocol):
    async def send(self, intent: Intent, metadata: dict[str, Any] | None = None) -> list[dict[str, Any]]: ...

    async def find(self, channel: str, thread_ts: str | None, key: str) -> str | None: ...


@dataclass
class SlackSink:
    adapter: ChannelAdapter
    team_id: str
    token: str

    async def send(self, intent: Intent, metadata: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        return await self.adapter.send(self.team_id, self.token, intent, metadata)

    async def find(self, channel: str, thread_ts: str | None, key: str) -> str | None:
        """A message this app posted with ``key`` in its metadata, in the thread or channel."""
        client = self.adapter.client
        method, params = (
            ("conversations.replies", {"ts": thread_ts})
            if thread_ts
            else ("conversations.history", {"limit": 30})
        )
        try:
            body = await client.call(
                method, self.token, self.team_id, channel=channel, include_all_metadata=True, **params
            )
        except Exception:  # best effort; posting again is the fallback
            log.warning("could not look for an earlier post", exc_info=True)
            return None
        for message in body.get("messages", []):
            if ((message.get("metadata") or {}).get("event_payload") or {}).get("idempotency_key") == key:
                return str(message["ts"])
        return None


@dataclass
class CaptureSink:
    sent: list[tuple[Intent, dict[str, Any] | None]] = field(default_factory=list)
    _ts: Any = field(default_factory=lambda: itertools.count(1))

    async def send(self, intent: Intent, metadata: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        self.sent.append((intent, metadata))
        ts = getattr(intent, "ts", None) if isinstance(intent, Progress) else None
        return [
            {
                "ok": True,
                "ts": ts or f"9{next(self._ts):09d}.000100",
                "channel": getattr(intent, "channel", ""),
            }
        ]

    async def find(self, channel: str, thread_ts: str | None, key: str) -> str | None:
        for _, metadata in self.sent:
            if ((metadata or {}).get("event_payload") or {}).get("idempotency_key") == key:
                return "captured"
        return None


async def post_once(
    pool: Any,
    sink: Sink,
    org_id: UUID | str,
    task_id: UUID | str,
    key: str,
    intent: Intent,
    *,
    channel: str,
    thread_ts: str | None,
    operation: str = "post_message",
) -> str | None:
    """Post ``intent`` once for ``key``; returns the message ts (the earlier one on a retry)."""
    async with tenant_tx(pool, org_id) as conn:
        row = await (
            await conn.execute(
                "select status, target_ref from app.external_actions where idempotency_key = %s", (key,)
            )
        ).fetchone()
        if row is not None and row[0] == "succeeded":
            return str(row[1]).rsplit(":", 1)[-1] if row[1] else None
        if row is None:
            await conn.execute(
                "insert into app.external_actions (org_id, task_id, integration_key, operation, "
                "idempotency_key) values (%s, %s, 'slack', %s, %s)",
                (org_id, task_id, operation, key),
            )
    metadata = {"event_type": "agent_post", "event_payload": {"idempotency_key": key}}
    ts = await sink.find(channel, thread_ts, key) if row is not None else None
    if ts is None:
        responses = await sink.send(intent, metadata)
        ts = str(responses[0].get("ts")) if responses and responses[0].get("ts") else None
        channel = str(responses[0].get("channel") or channel) if responses else channel
    async with tenant_tx(pool, org_id) as conn:
        await conn.execute(
            "update app.external_actions set status = 'succeeded', target_ref = %s, performed_at = now() "
            "where idempotency_key = %s",
            (f"{channel}:{ts}", key),
        )
    return ts
