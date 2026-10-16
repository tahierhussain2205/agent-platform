"""Provider requests from adapters: a read is retried once after a transport failure; a write
never is (its idempotency is checked before acting, not by repeating the request)."""

import httpx
import pytest

from app.tools.http import request


def flaky(failures: int) -> tuple[httpx.AsyncClient, list[str]]:
    seen: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req.method)
        if len(seen) <= failures:
            raise httpx.RemoteProtocolError("Server disconnected without sending a response", request=req)
        return httpx.Response(200, json={"ok": True})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler)), seen


async def test_a_read_is_retried_once() -> None:
    client, seen = flaky(1)
    async with client:
        response = await request(client, "GET", "https://api.example/repos")
    assert response.json() == {"ok": True}
    assert seen == ["GET", "GET"]


async def test_a_read_that_keeps_failing_raises() -> None:
    client, seen = flaky(5)
    async with client:
        with pytest.raises(httpx.RemoteProtocolError):
            await request(client, "GET", "https://api.example/repos")
    assert seen == ["GET", "GET"]


async def test_a_write_is_never_repeated() -> None:
    client, seen = flaky(1)
    async with client:
        with pytest.raises(httpx.RemoteProtocolError):
            await request(client, "POST", "https://api.example/issue", json={"summary": "x"})
    assert seen == ["POST"]
