"""Slack Web API client: form encoded POSTs with a bearer token, Slack's ``{"ok": ...}``
envelope, cursor pagination, the shared per workspace limiter and 429 retries."""

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx

from app.slack.limiter import SlackRateLimiter

log = logging.getLogger(__name__)


class SlackError(Exception):
    def __init__(self, method: str, error: str, status: int = 200) -> None:
        super().__init__(f"{method}: {error}")
        self.method = method
        self.error = error
        self.status = status


def _form(params: dict[str, Any]) -> dict[str, str]:
    form: dict[str, str] = {}
    for key, value in params.items():
        if value is None:
            continue
        if isinstance(value, dict | list):
            form[key] = json.dumps(value)
        elif isinstance(value, bool):
            form[key] = "true" if value else "false"
        else:
            form[key] = str(value)
    return form


class SlackClient:
    def __init__(
        self,
        base_url: str,
        http: httpx.AsyncClient | None = None,
        limiter: SlackRateLimiter | None = None,
        max_retries: int = 3,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._http = http or httpx.AsyncClient(timeout=15.0)
        self.limiter = limiter
        self.max_retries = max_retries

    async def call(
        self,
        method: str,
        token: str | None = None,
        team_id: str | None = None,
        *,
        auth: tuple[str, str] | None = None,
        **params: Any,
    ) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        for attempt in range(self.max_retries + 1):
            if self.limiter is not None and team_id:
                await self.limiter.acquire(team_id, method)
            try:
                response = await self._http.post(
                    f"{self.base_url}/{method}",
                    data=_form(params),
                    headers=headers,
                    auth=httpx.BasicAuth(*auth) if auth else httpx.USE_CLIENT_DEFAULT,
                )
            except httpx.HTTPError as exc:
                if attempt == self.max_retries:
                    raise SlackError(method, f"unreachable: {type(exc).__name__}", 502) from exc
                await asyncio.sleep(0.5 * (attempt + 1))
                continue
            if response.status_code == 429:
                retry_after = float(response.headers.get("Retry-After", "1"))
                log.info("slack rate limited", extra={"method": method, "retry_after": retry_after})
                if attempt == self.max_retries:
                    raise SlackError(method, "ratelimited", 429)
                await asyncio.sleep(min(retry_after, 30.0))
                continue
            if response.status_code >= 500 and attempt < self.max_retries:
                await asyncio.sleep(0.5 * (attempt + 1))
                continue
            try:
                body: dict[str, Any] = response.json()
            except ValueError as exc:
                raise SlackError(
                    method, f"bad response ({response.status_code})", response.status_code
                ) from exc
            if not body.get("ok"):
                raise SlackError(method, str(body.get("error", "unknown_error")), response.status_code)
            return body
        raise SlackError(method, "retries exhausted")  # pragma: no cover

    async def paginate(
        self, method: str, key: str, token: str, team_id: str, **params: Any
    ) -> AsyncIterator[Any]:
        """Items of ``key`` across pages (objects for channels, plain ids for members)."""
        cursor: str | None = None
        limit = params.pop("limit", 200)
        while True:
            body = await self.call(method, token, team_id, cursor=cursor, limit=limit, **params)
            for item in body.get(key, []):
                yield item
            cursor = (body.get("response_metadata") or {}).get("next_cursor") or None
            if not cursor:
                return

    async def aclose(self) -> None:
        await self._http.aclose()
