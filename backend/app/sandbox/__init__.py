"""Sandboxes for repository work (spec 8): an interface to a managed provider, and its HTTP client.

A sandbox is untrusted: the platform puts code in (an archive at a pinned commit, fetched outside
with a read only token that never enters the sandbox) and reads results out through the provider's
API. This phase reads files only (list, read ranges, search); phase 12 adds commands.

``HttpSandboxProvider`` speaks the provider API the local stand-in implements (B-008): replacing it
with a managed provider is a client for that provider's API, nothing else.
"""

from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from app.tools.http import request


class SandboxError(Exception):
    """The provider refused or failed; the message is safe to show (no tokens, no paths outside)."""


@dataclass(frozen=True)
class Limits:
    ttl_seconds: int = 2 * 3600
    disk_mb: int = 256


class SandboxProvider(Protocol):
    async def create(self, limits: Limits, metadata: dict[str, str]) -> str: ...

    async def upload(self, sandbox_id: str, archive: bytes, path: str, strip: int = 1) -> int: ...

    async def list_files(
        self, sandbox_id: str, path: str, recursive: bool = False
    ) -> list[dict[str, Any]]: ...

    async def read(
        self, sandbox_id: str, path: str, start: int = 1, end: int | None = None
    ) -> dict[str, Any]: ...

    async def search(
        self,
        sandbox_id: str,
        pattern: str,
        path: str = "",
        glob: str = "*",
        regex: bool = False,
        limit: int = 50,
    ) -> dict[str, Any]: ...

    async def destroy(self, sandbox_id: str) -> None: ...


class HttpSandboxProvider:
    def __init__(self, base_url: str, api_key: str, http: httpx.AsyncClient) -> None:
        self.base = base_url.rstrip("/")
        self.headers = {"authorization": f"Bearer {api_key}"}
        self.http = http

    async def _call(self, method: str, path: str, **kw: Any) -> Any:
        try:
            response = await request(self.http, method, f"{self.base}{path}", headers=self.headers, **kw)
        except httpx.HTTPError as exc:
            raise SandboxError(f"the sandbox provider is unreachable ({type(exc).__name__})") from exc
        if response.status_code == 204:
            return None
        if response.status_code >= 400:
            try:
                message = str(response.json().get("error", {}).get("message") or response.status_code)
            except ValueError:
                message = f"HTTP {response.status_code}"
            raise SandboxError(f"the sandbox provider refused: {message}"[:300])
        return response.json()

    async def create(self, limits: Limits, metadata: dict[str, str]) -> str:
        spec = {
            "ttl_seconds": limits.ttl_seconds,
            "limits": {"disk_mb": limits.disk_mb},
            "metadata": metadata,
        }
        body = await self._call("POST", "/sandboxes", json=spec)
        return str(body["id"])

    async def upload(self, sandbox_id: str, archive: bytes, path: str, strip: int = 1) -> int:
        body = await self._upload(sandbox_id, archive, path, strip)
        return int(body.get("files", 0))

    async def _upload(self, sandbox_id: str, archive: bytes, path: str, strip: int) -> Any:
        try:
            response = await self.http.put(
                f"{self.base}/sandboxes/{sandbox_id}/archive",
                params={"path": path, "strip": strip},
                content=archive,
                headers={**self.headers, "content-type": "application/gzip"},
            )
        except httpx.HTTPError as exc:
            raise SandboxError(f"the sandbox provider is unreachable ({type(exc).__name__})") from exc
        if response.status_code >= 400:
            raise SandboxError(f"the sandbox provider refused the archive ({response.status_code})")
        return response.json()

    async def list_files(self, sandbox_id: str, path: str, recursive: bool = False) -> list[dict[str, Any]]:
        params = {"path": path, "recursive": "true" if recursive else "false"}
        body = await self._call("GET", f"/sandboxes/{sandbox_id}/files", params=params)
        return list(body.get("entries") or [])

    async def read(
        self, sandbox_id: str, path: str, start: int = 1, end: int | None = None
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"path": path, "start": start}
        if end is not None:
            params["end"] = end
        body: dict[str, Any] = await self._call(
            "GET", f"/sandboxes/{sandbox_id}/files/content", params=params
        )
        return body

    async def search(
        self,
        sandbox_id: str,
        pattern: str,
        path: str = "",
        glob: str = "*",
        regex: bool = False,
        limit: int = 50,
    ) -> dict[str, Any]:
        spec = {"pattern": pattern, "path": path, "glob": glob, "regex": regex, "max_results": limit}
        body: dict[str, Any] = await self._call("POST", f"/sandboxes/{sandbox_id}/search", json=spec)
        return body

    async def destroy(self, sandbox_id: str) -> None:
        try:
            await self._call("DELETE", f"/sandboxes/{sandbox_id}")
        except SandboxError as exc:
            if "No such sandbox" not in str(exc):  # already gone is destroyed
                raise


def provider_from_settings(settings: Any, http: httpx.AsyncClient) -> HttpSandboxProvider | None:
    if settings is None or not settings.sandbox_url or settings.sandbox_api_key is None:
        return None
    return HttpSandboxProvider(settings.sandbox_url, settings.sandbox_api_key.get_secret_value(), http)
