"""Svix style webhook signing and delivery (what Clerk uses).

Deliveries run one at a time in order, with retries on failure, like a well behaved Svix queue.
Every attempt is logged so tests can inspect what was sent and what the receiver answered.
"""

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx

log = logging.getLogger("fakes.svix")
RETRY_DELAYS = (0.2, 1.0, 3.0, 8.0)


def sign(secret: str, msg_id: str, timestamp: int, body: bytes) -> str:
    key = base64.b64decode(secret.removeprefix("whsec_"))
    digest = hmac.new(key, f"{msg_id}.{timestamp}.".encode() + body, hashlib.sha256).digest()
    return "v1," + base64.b64encode(digest).decode()


@dataclass
class Delivery:
    msg_id: str
    event_type: str
    payload: dict[str, Any]
    attempts: list[dict[str, Any]] = field(default_factory=list)
    delivered: bool = False


class Deliverer:
    def __init__(self, url: str | None, secret: str, retry_delays: tuple[float, ...] = RETRY_DELAYS) -> None:
        self.url = url
        self.secret = secret
        self.retry_delays = retry_delays
        self.queue: asyncio.Queue[Delivery] = asyncio.Queue()
        self.log: list[Delivery] = []
        self._task: asyncio.Task[None] | None = None
        self._http = httpx.AsyncClient(timeout=10.0)

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
        await self._http.aclose()

    def enqueue(self, event_type: str, data: dict[str, Any]) -> Delivery:
        payload = {
            "type": event_type,
            "object": "event",
            "data": data,
            "timestamp": int(time.time() * 1000),
            "instance_id": "ins_local",
        }
        delivery = Delivery(f"msg_{uuid.uuid4().hex}", event_type, payload)
        self.log.append(delivery)
        if self.url:
            self.queue.put_nowait(delivery)
        return delivery

    async def drain(self, limit_seconds: float = 30.0) -> None:
        """Wait until every queued delivery finished (successfully or out of retries)."""
        async with asyncio.timeout(limit_seconds):
            await self.queue.join()

    async def _send(self, delivery: Delivery) -> bool:
        body = json.dumps(delivery.payload).encode()
        ts = int(time.time())
        headers = {
            "content-type": "application/json",
            "svix-id": delivery.msg_id,
            "svix-timestamp": str(ts),
            "svix-signature": sign(self.secret, delivery.msg_id, ts, body),
        }
        try:
            assert self.url is not None
            response = await self._http.post(self.url, content=body, headers=headers)
            delivery.attempts.append({"status": response.status_code, "body": response.text[:300]})
            return 200 <= response.status_code < 300
        except httpx.HTTPError as exc:
            delivery.attempts.append({"error": type(exc).__name__})
            return False

    async def _run(self) -> None:
        while True:
            delivery = await self.queue.get()
            try:
                for delay in (0.0, *self.retry_delays):
                    if delay:
                        await asyncio.sleep(delay)
                    if await self._send(delivery):
                        delivery.delivered = True
                        break
                if not delivery.delivered:
                    log.warning("webhook %s %s not delivered after retries", delivery.event_type, delivery.msg_id)
            finally:
                self.queue.task_done()
