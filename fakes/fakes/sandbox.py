"""Sandbox provider stand-in (B-008): the API of a managed sandbox provider, locally.

One directory per sandbox under the state directory, with a time to live and a disk limit. The
platform uploads a repository archive and reads it through the file API; there is no command
execution yet (phase 12 adds it in an isolated container). All routes need the provider API key.

- ``POST /sandbox/v1/sandboxes`` ``{"ttl_seconds", "limits": {"disk_mb"}, "metadata"}``
- ``GET`` / ``DELETE /sandbox/v1/sandboxes/{id}``
- ``PUT /sandbox/v1/sandboxes/{id}/archive?path=&strip=`` a gzipped tar, extracted safely
  (no absolute paths, no ``..``, no links or devices, within the disk limit)
- ``GET /sandbox/v1/sandboxes/{id}/files?path=&recursive=``
- ``GET /sandbox/v1/sandboxes/{id}/files/content?path=&start=&end=``
- ``POST /sandbox/v1/sandboxes/{id}/search`` ``{"pattern", "regex", "path", "glob", "max_results"}``
- ``POST /sandbox/v1/sandboxes/{id}/exec``: 501 for now.
"""

import fnmatch
import io
import re
import secrets
import shutil
import tarfile
import time
from pathlib import Path, PurePosixPath
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from fakes.config import Config
from fakes.store import Store

MAX_TTL = 4 * 3600
MAX_DISK_MB = 512
MAX_READ_LINES = 2000
MAX_LINE = 2000


def error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)


class Sandboxes:
    def __init__(self, config: Config, store: Store) -> None:
        self.config = config
        self.store = store
        self.root = config.state_dir / "sandboxes"

    @property
    def s(self) -> dict[str, Any]:
        state: dict[str, Any] = self.store.data.setdefault("sandbox", {"sandboxes": {}, "destroyed": []})
        return state

    def reset(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)
        self.store.data["sandbox"] = {"sandboxes": {}, "destroyed": []}

    def workspace(self, sandbox_id: str) -> Path:
        return self.root / sandbox_id / "workspace"

    def live(self, sandbox_id: str) -> dict[str, Any] | None:
        """The sandbox, if it exists and has not expired (expired ones are removed here)."""
        record = self.s["sandboxes"].get(sandbox_id)
        if record is None:
            return None
        if record["expires_at"] < time.time():
            self.destroy(sandbox_id, "expired")
            return None
        return record  # type: ignore[no-any-return]

    def destroy(self, sandbox_id: str, reason: str) -> None:
        with self.store.lock:
            if self.s["sandboxes"].pop(sandbox_id, None) is not None:
                self.s["destroyed"].append({"id": sandbox_id, "reason": reason, "at": time.time()})
                self.s["destroyed"] = self.s["destroyed"][-200:]
            self.store.save()
        shutil.rmtree(self.root / sandbox_id, ignore_errors=True)

    def resolve(self, sandbox_id: str, path: str) -> Path | None:
        """A path inside the sandbox's workspace, or None for anything that would leave it."""
        base = self.workspace(sandbox_id).resolve()
        target = (base / path.lstrip("/")).resolve()
        return target if target == base or base in target.parents else None


def build_router(sbx: Sandboxes) -> APIRouter:
    router = APIRouter(prefix="/sandbox/v1")
    cfg = sbx.config

    def authorized(request: Request) -> bool:
        return secrets.compare_digest(request.headers.get("authorization", ""), f"Bearer {cfg.sandbox_api_key}")

    def body(record: dict[str, Any]) -> dict[str, Any]:
        return {"id": record["id"], "status": "running", "expires_at": record["expires_at"],
                "limits": record["limits"], "bytes": record["bytes"], "metadata": record["metadata"]}  # fmt: skip

    @router.post("/sandboxes", status_code=201)
    async def create(request: Request) -> JSONResponse:
        if not authorized(request):
            return error(401, "unauthorized", "Invalid API key")
        spec = await request.json()
        ttl = int(spec.get("ttl_seconds") or 3600)
        disk = int((spec.get("limits") or {}).get("disk_mb") or 256)
        if not 60 <= ttl <= MAX_TTL or not 1 <= disk <= MAX_DISK_MB:
            return error(422, "invalid_limits", f"ttl_seconds 60 to {MAX_TTL}, disk_mb 1 to {MAX_DISK_MB}")
        sandbox_id = "sbx_" + secrets.token_hex(8)
        record = {"id": sandbox_id, "expires_at": time.time() + ttl, "limits": {"disk_mb": disk},
                  "bytes": 0, "metadata": dict(spec.get("metadata") or {}), "created_at": time.time()}  # fmt: skip
        sbx.workspace(sandbox_id).mkdir(parents=True, exist_ok=True)
        with sbx.store.lock:
            sbx.s["sandboxes"][sandbox_id] = record
            sbx.store.save()
        return JSONResponse(body(record), status_code=201)

    @router.get("/sandboxes/{sandbox_id}")
    async def describe(request: Request, sandbox_id: str) -> JSONResponse:
        if not authorized(request):
            return error(401, "unauthorized", "Invalid API key")
        record = sbx.live(sandbox_id)
        return JSONResponse(body(record)) if record else error(404, "not_found", "No such sandbox")

    @router.delete("/sandboxes/{sandbox_id}")
    async def delete(request: Request, sandbox_id: str) -> Response:
        if not authorized(request):
            return error(401, "unauthorized", "Invalid API key")
        if sbx.live(sandbox_id) is None:
            return error(404, "not_found", "No such sandbox")
        sbx.destroy(sandbox_id, "deleted")
        return Response(status_code=204)

    @router.put("/sandboxes/{sandbox_id}/archive")
    async def upload(request: Request, sandbox_id: str) -> JSONResponse:
        if not authorized(request):
            return error(401, "unauthorized", "Invalid API key")
        record = sbx.live(sandbox_id)
        if record is None:
            return error(404, "not_found", "No such sandbox")
        target = sbx.resolve(sandbox_id, request.query_params.get("path", ""))
        if target is None:
            return error(422, "invalid_path", "The path leaves the workspace")
        strip = int(request.query_params.get("strip") or 0)
        limit = record["limits"]["disk_mb"] * 1024 * 1024
        data = await request.body()
        files, total = 0, record["bytes"]
        try:
            with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
                for member in tar.getmembers():
                    parts = PurePosixPath(member.name).parts[strip:]
                    if not parts:
                        continue
                    if member.issym() or member.islnk() or member.isdev() or member.isfifo():
                        return error(422, "unsafe_archive", f"Links and devices are not allowed: {member.name}")
                    if PurePosixPath(member.name).is_absolute() or ".." in parts:
                        return error(422, "unsafe_archive", f"Path leaves the workspace: {member.name}")
                    destination = sbx.resolve(
                        sandbox_id, str(target.relative_to(sbx.workspace(sandbox_id).resolve()) / PurePosixPath(*parts))
                    )
                    if destination is None:
                        return error(422, "unsafe_archive", f"Path leaves the workspace: {member.name}")
                    if member.isdir():
                        destination.mkdir(parents=True, exist_ok=True)
                        continue
                    total += member.size
                    if total > limit:
                        return error(507, "disk_full", "The archive does not fit the sandbox's disk limit")
                    source = tar.extractfile(member)
                    if source is None:
                        continue
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_bytes(source.read())
                    files += 1
        except tarfile.TarError:
            return error(422, "invalid_archive", "Not a gzipped tar archive")
        with sbx.store.lock:
            record["bytes"] = total
            sbx.store.save()
        return JSONResponse({"files": files, "bytes": total})

    @router.get("/sandboxes/{sandbox_id}/files")
    async def files(request: Request, sandbox_id: str) -> JSONResponse:
        if not authorized(request):
            return error(401, "unauthorized", "Invalid API key")
        if sbx.live(sandbox_id) is None:
            return error(404, "not_found", "No such sandbox")
        base = sbx.workspace(sandbox_id).resolve()
        folder = sbx.resolve(sandbox_id, request.query_params.get("path", ""))
        if folder is None or not folder.is_dir():
            return error(404, "not_found", "No such directory")
        recursive = request.query_params.get("recursive") == "true"
        found = sorted(folder.rglob("*") if recursive else folder.iterdir())[:2000]
        entries = [{"path": str(p.relative_to(base)), "type": "dir" if p.is_dir() else "file",
                    "size": p.stat().st_size if p.is_file() else 0} for p in found]  # fmt: skip
        return JSONResponse({"entries": entries, "truncated": len(found) == 2000})

    @router.get("/sandboxes/{sandbox_id}/files/content")
    async def content(request: Request, sandbox_id: str) -> JSONResponse:
        if not authorized(request):
            return error(401, "unauthorized", "Invalid API key")
        if sbx.live(sandbox_id) is None:
            return error(404, "not_found", "No such sandbox")
        q = request.query_params
        target = sbx.resolve(sandbox_id, q.get("path", ""))
        if target is None or not target.is_file():
            return error(404, "not_found", "No such file")
        raw = target.read_bytes()
        if b"\0" in raw[:8000]:
            return error(422, "binary", "Binary files are not read")
        lines = raw.decode("utf-8", errors="replace").splitlines()
        start = max(1, int(q.get("start") or 1))
        end = min(len(lines), int(q.get("end") or start + MAX_READ_LINES - 1), start + MAX_READ_LINES - 1)
        return JSONResponse({"path": q.get("path"), "start": start, "end": end, "total_lines": len(lines),
                             "content": "\n".join(line[:MAX_LINE] for line in lines[start - 1 : end])})  # fmt: skip

    @router.post("/sandboxes/{sandbox_id}/search")
    async def search(request: Request, sandbox_id: str) -> JSONResponse:
        if not authorized(request):
            return error(401, "unauthorized", "Invalid API key")
        if sbx.live(sandbox_id) is None:
            return error(404, "not_found", "No such sandbox")
        spec = await request.json()
        pattern = str(spec.get("pattern") or "")
        if not 1 <= len(pattern) <= 200:
            return error(422, "invalid_pattern", "A pattern of 1 to 200 characters")
        try:
            rx = re.compile(pattern if spec.get("regex") else re.escape(pattern), re.IGNORECASE)
        except re.error as exc:
            return error(422, "invalid_pattern", f"Not a valid regular expression: {exc}")
        base = sbx.workspace(sandbox_id).resolve()
        folder = sbx.resolve(sandbox_id, str(spec.get("path") or ""))
        if folder is None or not folder.exists():
            return error(404, "not_found", "No such directory")
        glob = str(spec.get("glob") or "*")
        limit = max(1, min(int(spec.get("max_results") or 50), 500))
        matches: list[dict[str, Any]] = []
        for path in sorted(p for p in (folder.rglob("*") if folder.is_dir() else [folder]) if p.is_file()):
            relative = str(path.relative_to(base))
            if not fnmatch.fnmatch(path.name, glob) and not fnmatch.fnmatch(relative, glob):
                continue
            raw = path.read_bytes()
            if b"\0" in raw[:8000]:
                continue
            for n, line in enumerate(raw.decode("utf-8", errors="replace").splitlines(), 1):
                if rx.search(line[:MAX_LINE]):
                    matches.append({"path": relative, "line": n, "text": line[:MAX_LINE]})
                    if len(matches) >= limit:
                        return JSONResponse({"matches": matches, "truncated": True})
        return JSONResponse({"matches": matches, "truncated": False})

    @router.post("/sandboxes/{sandbox_id}/exec")
    async def exec_(request: Request, sandbox_id: str) -> JSONResponse:
        if not authorized(request):
            return error(401, "unauthorized", "Invalid API key")
        return error(501, "not_implemented", "Command execution is not available in this stand-in yet")

    return router


def build_admin_router(sbx: Sandboxes) -> APIRouter:
    router = APIRouter(prefix="/_admin/sandbox")

    @router.get("/sandboxes")
    def listing() -> dict[str, Any]:
        """Live sandboxes (expired ones removed) and the recently destroyed, for tests."""
        for sandbox_id in list(sbx.s["sandboxes"]):
            sbx.live(sandbox_id)
        return {"live": list(sbx.s["sandboxes"].values()), "destroyed": sbx.s["destroyed"]}

    @router.post("/sandboxes/{sandbox_id}/grep")
    async def grep(sandbox_id: str, request: Request) -> dict[str, Any]:
        """Whether any file in a sandbox contains a string (tests check no token ever entered one)."""
        needle = (await request.json())["text"].encode()
        root = sbx.root / sandbox_id
        hits = [str(p.relative_to(root)) for p in root.rglob("*") if p.is_file() and needle in p.read_bytes()]
        return {"hits": hits}

    @router.post("/sandboxes/{sandbox_id}/expire")
    def expire(sandbox_id: str) -> dict[str, bool]:
        record = sbx.s["sandboxes"].get(sandbox_id)
        if record:
            record["expires_at"] = 0
        return {"ok": record is not None}

    return router
