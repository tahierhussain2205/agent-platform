"""Outbound rate limiting per Slack workspace, shared by every worker through Key Value.

Slack limits are per workspace and per method tier; a fixed one second window per (workspace,
bucket) is simple and close enough, and Slack's own 429 + Retry-After is honored on top.
"""

import asyncio
import time
from typing import Any

# Requests per second per workspace. chat.* is "special" (about 1/s per channel, bursts allowed);
# Web API tier 3 (conversations.*) is about 50/min; tier 4 (users.*) about 100/min.
BUCKETS: dict[str, tuple[str, int]] = {
    "chat.postMessage": ("chat", 5),
    "chat.update": ("chat", 5),
    "chat.postEphemeral": ("chat", 5),
    "views.publish": ("views", 3),
    "conversations.list": ("tier3", 1),
    "conversations.members": ("tier3", 1),
    "conversations.history": ("tier3", 1),
    "conversations.replies": ("tier3", 1),
    "conversations.info": ("tier3", 1),
    "users.info": ("tier4", 2),
    "users.lookupByEmail": ("tier4", 2),
}
DEFAULT_BUCKET = ("default", 2)


class SlackRateLimiter:
    def __init__(self, kv: Any, max_wait_seconds: float = 30.0) -> None:
        self.kv = kv
        self.max_wait_seconds = max_wait_seconds

    async def acquire(self, team_id: str, method: str) -> float:
        """Wait for a slot. Returns the seconds waited."""
        bucket, limit = BUCKETS.get(method, DEFAULT_BUCKET)
        waited = 0.0
        while True:
            now = time.time()
            window = int(now)
            key = f"ap:g:slack-rl:{team_id}:{bucket}:{window}"
            count = await self.kv.incr(key)
            if count == 1:
                await self.kv.expire(key, 3)
            if count <= limit:
                return waited
            pause = (window + 1) - now + 0.01
            if waited + pause > self.max_wait_seconds:
                raise TimeoutError(f"rate limit wait for {method} exceeded {self.max_wait_seconds}s")
            await asyncio.sleep(pause)
            waited += pause
