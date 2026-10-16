"""Sandboxes for repository work (spec 8): an interface to a managed provider, and its HTTP client.

A sandbox is untrusted: the platform puts code in (an archive at a pinned commit, fetched outside
with a read only token that never enters the sandbox) and reads results out through the provider's
API: files (list, read, search, write), commands (``exec``: run in the sandbox, which has no
network and holds no credential) and the change of a working tree (``diff``), which is all that
leaves it.

``HttpSandboxProvider`` speaks the provider API the local stand-in implements; ``E2BSandboxProvider``
(``app.sandbox.e2b_provider``) gives the same contract on E2B (B-008). ``SANDBOX_PROVIDER`` chooses.
"""

import base64
import functools
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from app.tools.http import request


class SandboxError(Exception):
    """The provider refused or failed; the message is safe to show (no tokens, no paths outside)."""


@dataclass(frozen=True)
class Ran:
    """What a command did. Output is capped by the provider (the end is kept)."""

    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool
    seconds: float

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


@dataclass(frozen=True)
class ChangedFile:
    path: str
    status: str  # A, M, D, T
    mode: str  # 100644, 100755, 120000 (a link), 160000 (a submodule)
    content: bytes | None  # None when deleted


@dataclass(frozen=True)
class Change:
    """A working tree's change against its baseline commit."""

    patch: str
    files: list[ChangedFile]


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

    async def exec(
        self,
        sandbox_id: str,
        command: str,
        cwd: str = "",
        env: dict[str, str] | None = None,
        seconds: int = 120,
    ) -> Ran: ...

    async def write(self, sandbox_id: str, path: str, content: bytes) -> int: ...

    async def read_bytes(self, sandbox_id: str, path: str) -> bytes: ...

    async def diff(self, sandbox_id: str, path: str) -> Change: ...


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

    async def exec(
        self,
        sandbox_id: str,
        command: str,
        cwd: str = "",
        env: dict[str, str] | None = None,
        seconds: int = 120,
    ) -> Ran:
        spec = {"command": command, "cwd": cwd, "env": env or {}, "timeout_seconds": seconds}
        body = await self._call("POST", f"/sandboxes/{sandbox_id}/exec", json=spec, timeout=seconds + 30)
        return Ran(
            int(body["exit_code"]),
            str(body.get("stdout") or ""),
            str(body.get("stderr") or ""),
            bool(body.get("timed_out")),
            float(body.get("seconds") or 0),
        )

    async def write(self, sandbox_id: str, path: str, content: bytes) -> int:
        try:
            response = await self.http.put(
                f"{self.base}/sandboxes/{sandbox_id}/files/content",
                params={"path": path},
                content=content,
                headers={**self.headers, "content-type": "application/octet-stream"},
            )
        except httpx.HTTPError as exc:
            raise SandboxError(f"the sandbox provider is unreachable ({type(exc).__name__})") from exc
        if response.status_code >= 400:
            raise SandboxError(f"the sandbox provider refused the file ({response.status_code})")
        return int(response.json().get("bytes", 0))

    async def read_bytes(self, sandbox_id: str, path: str) -> bytes:
        try:
            response = await request(
                self.http,
                "GET",
                f"{self.base}/sandboxes/{sandbox_id}/files/raw",
                params={"path": path},
                headers=self.headers,
            )
        except httpx.HTTPError as exc:
            raise SandboxError(f"the sandbox provider is unreachable ({type(exc).__name__})") from exc
        if response.status_code == 404:
            raise SandboxError(f"no file {path}")
        if response.status_code >= 400:
            raise SandboxError(f"the sandbox provider refused the read ({response.status_code})")
        return response.content

    async def diff(self, sandbox_id: str, path: str) -> Change:
        body = await self._call("POST", f"/sandboxes/{sandbox_id}/diff", json={"path": path}, timeout=120)
        files = [
            ChangedFile(
                str(f["path"]),
                str(f["status"]),
                str(f.get("mode") or "100644"),
                base64.b64decode(f["content_b64"]) if f.get("content_b64") is not None else None,
            )
            for f in body.get("files") or []
        ]
        return Change(str(body.get("patch") or ""), files)

    async def destroy(self, sandbox_id: str) -> None:
        try:
            await self._call("DELETE", f"/sandboxes/{sandbox_id}")
        except SandboxError as exc:
            if "No such sandbox" not in str(exc):  # already gone is destroyed
                raise


def provider_from_settings(settings: Any, http: httpx.AsyncClient) -> SandboxProvider | None:
    """The configured provider, or None (code tools then say there is none)."""
    if settings is None or settings.sandbox_api_key is None:
        return None
    key = settings.sandbox_api_key.get_secret_value()
    if getattr(settings, "sandbox_provider", "http") == "e2b":
        return _e2b(key, settings.sandbox_template)
    if not settings.sandbox_url:
        return None
    return HttpSandboxProvider(settings.sandbox_url, key, http)


@functools.cache
def _e2b(key: str, template: str) -> SandboxProvider:
    """One E2B client per process, so the sandboxes it connected to are reused across tool calls."""
    from app.sandbox.e2b_provider import E2BSandboxProvider

    return E2BSandboxProvider(key, template)
