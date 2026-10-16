"""``RemoteVault``: the API's and worker's client of the vault service."""

from typing import Any
from uuid import UUID

import httpx

from app.vault_service import Credential, VaultError


class RemoteVault:
    def __init__(self, base_url: str, token: str, http: httpx.AsyncClient | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.http = http or httpx.AsyncClient(timeout=30.0)

    async def _post(self, path: str, **body: Any) -> Any:
        payload = {k: (str(v) if isinstance(v, UUID) else v) for k, v in body.items()}
        try:
            response = await self.http.post(
                f"{self.base_url}{path}", json=payload, headers={"authorization": f"Bearer {self.token}"}
            )
        except httpx.HTTPError as exc:
            raise VaultError("provider", f"vault unreachable: {type(exc).__name__}") from exc
        if response.status_code == 204:
            return None
        try:
            data = response.json()
        except ValueError as exc:
            raise VaultError("provider", f"vault answered {response.status_code}") from exc
        if response.status_code >= 400:
            raise VaultError(str(data.get("error", "provider")), str(data.get("message", data.get("error"))))
        return data

    async def ready(self) -> str:
        return str((await self._post("/v1/ready"))["status"])

    async def store(self, org_id: UUID | str, connection_id: UUID | str, secret: str) -> None:
        await self._post("/v1/secrets", org_id=org_id, connection_id=connection_id, secret=secret)

    async def read(self, org_id: UUID | str, connection_id: UUID | str) -> str:
        data = await self._post("/v1/secrets/read", org_id=org_id, connection_id=connection_id)
        return str(data["secret"])

    async def delete(self, org_id: UUID | str, connection_id: UUID | str) -> None:
        await self._post("/v1/secrets/delete", org_id=org_id, connection_id=connection_id)

    async def exchange(
        self,
        org_id: UUID | str,
        connection_id: UUID | str,
        provider: str,
        code: str,
        redirect_uri: str,
        verifier: str | None = None,
    ) -> dict[str, Any]:
        data: dict[str, Any] = await self._post(
            "/v1/exchange",
            org_id=org_id,
            connection_id=connection_id,
            provider=provider,
            code=code,
            redirect_uri=redirect_uri,
            verifier=verifier,
        )
        return data

    async def register_installation(
        self,
        org_id: UUID | str,
        connection_id: UUID | str,
        installation_id: str,
        code: str,
        redirect_uri: str,
    ) -> dict[str, Any]:
        data: dict[str, Any] = await self._post(
            "/v1/github/installations",
            org_id=org_id,
            connection_id=connection_id,
            installation_id=installation_id,
            code=code,
            redirect_uri=redirect_uri,
        )
        return data

    async def credential(
        self,
        org_id: UUID | str,
        connection_id: UUID | str,
        task_id: UUID | str | None = None,
        membership_id: UUID | str | None = None,
    ) -> Credential:
        data = await self._post(
            "/v1/credentials",
            org_id=org_id,
            connection_id=connection_id,
            task_id=str(task_id) if task_id else None,
            membership_id=str(membership_id) if membership_id else None,
        )
        return Credential.from_dict(data)

    async def revoke(self, org_id: UUID | str, connection_id: UUID | str) -> None:
        await self._post("/v1/revoke", org_id=org_id, connection_id=connection_id)

    async def aclose(self) -> None:
        await self.http.aclose()
