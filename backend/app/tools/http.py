"""Requests to providers from tool adapters.

A read (GET, HEAD) is retried once after a transport failure: a kept alive connection the server
had just closed, or a brief network blip, should not fail a task. Writes are never retried here;
their idempotency is checked before acting (labels, markers, ``external_actions``), and the
activity's own retry goes through that check.
"""

import asyncio
import logging
from typing import Any

import httpx

log = logging.getLogger(__name__)
READS = ("GET", "HEAD")


async def request(http: httpx.AsyncClient, method: str, url: str, **kw: Any) -> httpx.Response:
    attempts = 2 if method.upper() in READS else 1
    for attempt in range(1, attempts + 1):
        try:
            return await http.request(method, url, **kw)
        except httpx.TransportError as exc:
            # The URL's path can name a repository or issue, never a secret; the query is left out.
            log.warning(
                "provider request failed",
                extra={"method": method, "host": httpx.URL(url).host, "error": type(exc).__name__,
                       "attempt": attempt},
            )  # fmt: skip
            if attempt == attempts:
                raise
            await asyncio.sleep(0.2)
    raise AssertionError("unreachable")  # pragma: no cover
