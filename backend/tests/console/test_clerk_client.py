"""Clerk Backend API client: what invitations ask Clerk for."""

import json
from typing import Any

import httpx

from app.api.main import Resources, build_resources
from app.identity.clerk_api import ClerkClient
from app.settings import Settings


def recording() -> tuple[httpx.AsyncClient, list[dict[str, Any]]]:
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"id": "orginv_1"})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler)), bodies


async def test_invitations_land_on_the_consoles_sign_up_page() -> None:
    http, bodies = recording()
    client = ClerkClient("https://api.clerk.test/v1", "sk_test", http=http,
                         invitation_redirect_url="https://console.example/sign-up")  # fmt: skip
    await client.create_invitation("org_1", "new@example.com", "org:member", "user_admin")
    assert bodies == [{"email_address": "new@example.com", "role": "org:member", "inviter_user_id": "user_admin",
                       "redirect_url": "https://console.example/sign-up"}]  # fmt: skip


async def test_without_a_redirect_clerk_decides() -> None:
    http, bodies = recording()
    await ClerkClient("https://api.clerk.test/v1", "sk_test", http=http).create_invitation(
        "org_1", "new@example.com", "org:admin", None
    )
    assert bodies == [{"email_address": "new@example.com", "role": "org:admin"}]


async def test_the_api_sends_invitations_to_its_console() -> None:
    res = Resources()
    await build_resources(
        Settings(clerk_secret_key="sk_test", console_url="https://web-staging.example/"), res
    )
    try:
        assert res.clerk_client is not None
        assert res.clerk_client._invitation_redirect_url == "https://web-staging.example/sign-up"
    finally:
        for close in reversed(res.owned):
            await close()
