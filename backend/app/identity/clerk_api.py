"""Clerk Backend API client (the calls the console makes; Clerk is the source of truth)."""

from typing import Any

import httpx


class ClerkError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(f"Clerk {status} {code}: {message}")
        self.status = status
        self.code = code
        self.message = message


class ClerkClient:
    def __init__(self, base_url: str, secret_key: str, http: httpx.AsyncClient | None = None) -> None:
        self._http = http or httpx.AsyncClient(timeout=10.0)
        self._base = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {secret_key}"}

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            response = await self._http.request(method, self._base + path, headers=self._headers, **kwargs)
        except httpx.HTTPError as exc:
            raise ClerkError(502, "unreachable", type(exc).__name__) from exc
        if response.status_code >= 400:
            code, message = "error", response.reason_phrase
            try:
                first = response.json().get("errors", [{}])[0]
                code, message = (
                    first.get("code", code),
                    first.get("long_message") or first.get("message", message),
                )
            except (ValueError, AttributeError, IndexError):
                pass
            raise ClerkError(response.status_code, code, message)
        return response.json() if response.content else None

    async def create_invitation(
        self, org_id: str, email: str, role: str, inviter_user_id: str | None
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"email_address": email, "role": role}
        if inviter_user_id:
            body["inviter_user_id"] = inviter_user_id
        result: dict[str, Any] = await self._request(
            "POST", f"/organizations/{org_id}/invitations", json=body
        )
        return result

    async def list_invitations(self, org_id: str, status: str = "pending") -> list[dict[str, Any]]:
        result = await self._request(
            "GET", f"/organizations/{org_id}/invitations", params={"status": status, "limit": 500}
        )
        data: list[dict[str, Any]] = result.get("data", []) if isinstance(result, dict) else result
        return data

    async def revoke_invitation(self, org_id: str, invitation_id: str, requesting_user_id: str) -> None:
        await self._request(
            "POST", f"/organizations/{org_id}/invitations/{invitation_id}/revoke",
            json={"requesting_user_id": requesting_user_id},
        )  # fmt: skip

    async def update_membership_role(self, org_id: str, user_id: str, role: str) -> None:
        await self._request("PATCH", f"/organizations/{org_id}/memberships/{user_id}", json={"role": role})

    async def delete_membership(self, org_id: str, user_id: str) -> None:
        await self._request("DELETE", f"/organizations/{org_id}/memberships/{user_id}")

    async def update_organization(self, org_id: str, name: str) -> None:
        await self._request("PATCH", f"/organizations/{org_id}", json={"name": name})

    async def aclose(self) -> None:
        await self._http.aclose()
