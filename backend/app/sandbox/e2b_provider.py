"""E2B as the sandbox provider (B-008): the contract of the stand-in's API, on E2B's SDK.

Each sandbox is an E2B microVM made from the platform's template (git, Python with pytest, Node;
``scripts/e2b_template.py``) with no internet access. Code goes in as an archive checked here
first (the stand-in's rules: no absolute paths, ``..``, links or devices, within the disk limit),
is read and changed through the files and commands APIs, and only a diff comes out. The workspace
is ``ROOT`` in the VM: every path the platform gives is relative to it and may not leave it.
Output, reads and searches are capped as the stand-in caps them, so callers see the same shapes
and messages from either provider.

E2B's free plan keeps a sandbox at most an hour (``MAX_TTL``); a sandbox gone sooner than the
platform expected is "No such sandbox", which maintenance treats as destroyed.
"""

import asyncio
import io
import json
import posixpath
import re
import secrets
import shlex
import tarfile
import time
from collections import OrderedDict
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import PurePosixPath
from typing import Any

import httpx
from e2b import (
    AsyncSandbox,
    AuthenticationException,
    CommandExitException,
    FileNotFoundException,
    FileType,
    RateLimitException,
    SandboxException,
    SandboxNotFoundException,
    TimeoutException,
)

from app.sandbox import Change, ChangedFile, Limits, Ran, SandboxError

ROOT = "/home/user/workspace"
HOME = "/home/user"
JOBS = f"{HOME}/.jobs"  # the platform's scratch files in the VM, outside the workspace
MAX_TTL = 3600
MAX_OUTPUT = 64 * 1024
MAX_TIMEOUT = 600
MAX_COMMAND = 4000
MAX_READ_LINES = 2000
MAX_LINE = 2000
MAX_RAW = 1024 * 1024
MAX_READ = 10 * 1024 * 1024
MAX_LISTED = 2000
LIST_DEPTH = 32
OPEN_SANDBOXES = 64
BASE_ENV = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": HOME, "LANG": "C.UTF-8"}

# Runs inside the sandbox (its Python): the stand-in's search, file by file, written to a result file.
SEARCH = r"""
import fnmatch, json, re, sys
from pathlib import Path
spec = json.load(open(sys.argv[1]))
base, folder = Path(spec["root"]), Path(spec["folder"])
rx = re.compile(spec["pattern"] if spec["regex"] else re.escape(spec["pattern"]), re.IGNORECASE)
out = {"matches": [], "truncated": False}
if not folder.exists():
    out = {"error": "not_found"}
else:
    paths = sorted(p for p in (folder.rglob("*") if folder.is_dir() else [folder]) if p.is_file())
    for path in paths:
        relative = str(path.relative_to(base))
        if not fnmatch.fnmatch(path.name, spec["glob"]) and not fnmatch.fnmatch(relative, spec["glob"]):
            continue
        try:
            raw = path.read_bytes()
        except OSError:
            continue
        if b"\0" in raw[:8000]:
            continue
        for n, line in enumerate(raw.decode("utf-8", errors="replace").splitlines(), 1):
            if rx.search(line[:2000]):
                out["matches"].append({"path": relative, "line": n, "text": line[:2000]})
                if len(out["matches"]) >= spec["limit"]:
                    out["truncated"] = True
                    break
        if out["truncated"]:
            break
json.dump(out, open(sys.argv[2], "w"))
"""


def refused(message: str) -> SandboxError:
    """The message the HTTP provider gives for the same refusal by the stand-in."""
    return SandboxError(f"the sandbox provider refused: {message}"[:300])


def inside(path: str) -> str | None:
    """The absolute path in the VM for a workspace path, or None when it would leave the workspace."""
    relative = posixpath.normpath(str(path or "").lstrip("/"))
    if relative == ".":
        return ROOT
    if relative == ".." or relative.startswith("../"):
        return None
    return f"{ROOT}/{relative}"


def checked_archive(archive: bytes, strip: int, limit: int) -> int:
    """The number of files in a gzipped tar the stand-in would accept; otherwise why not."""
    files, total = 0, 0
    try:
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
            for member in tar.getmembers():
                parts = PurePosixPath(member.name).parts[strip:]
                if not parts:
                    continue
                if member.issym() or member.islnk() or member.isdev() or member.isfifo():
                    raise refused(f"Links and devices are not allowed: {member.name}")
                if PurePosixPath(member.name).is_absolute() or ".." in parts:
                    raise refused(f"Path leaves the workspace: {member.name}")
                if member.isdir():
                    continue
                total += member.size
                if total > limit:
                    raise refused("The archive does not fit the sandbox's disk limit")
                if member.isfile():
                    files += 1
    except tarfile.TarError as exc:
        raise refused("Not a gzipped tar archive") from exc
    return files


def tail(text: str) -> str:
    return text if len(text) <= MAX_OUTPUT else "…(truncated)\n" + text[-MAX_OUTPUT:]


class Output:
    """A command's output as it streams, keeping only the end."""

    def __init__(self) -> None:
        self.text = ""
        self.cut = False

    def add(self, chunk: str) -> None:
        self.text += chunk
        if len(self.text) > 2 * MAX_OUTPUT:
            self.text = self.text[-MAX_OUTPUT - 1 :]
            self.cut = True

    def value(self) -> str:
        return "…(truncated)\n" + self.text[-MAX_OUTPUT:] if self.cut else tail(self.text)


class E2BSandboxProvider:
    def __init__(self, api_key: str, template: str, domain: str | None = None) -> None:
        self.api_key = api_key
        self.template = template
        self.opts: dict[str, Any] = {"api_key": api_key, **({"domain": domain} if domain else {})}
        # Sandboxes connected to in this process, per event loop (their clients belong to it).
        self._open: OrderedDict[str, tuple[asyncio.AbstractEventLoop, AsyncSandbox]] = OrderedDict()

    # ------------------------------------------------------------------ plumbing

    @contextmanager
    def _errors(self, sandbox_id: str | None = None, missing: str = "No such sandbox") -> Iterator[None]:
        """SDK failures as SandboxErrors whose messages are safe to show (never the key)."""
        try:
            yield
        except SandboxError:
            raise
        except SandboxNotFoundException as exc:
            if sandbox_id:
                self._open.pop(sandbox_id, None)
            raise refused("No such sandbox") from exc
        except FileNotFoundException as exc:
            raise refused(missing) from exc
        except AuthenticationException as exc:
            raise SandboxError("the sandbox provider refused our credentials") from exc
        except RateLimitException as exc:
            raise SandboxError("the sandbox provider is limiting our requests; try again shortly") from exc
        except TimeoutException as exc:
            raise SandboxError("the sandbox provider did not answer in time") from exc
        except (SandboxException, httpx.HTTPError, OSError) as exc:
            if sandbox_id and "not running" in str(exc).lower():
                self._open.pop(sandbox_id, None)
                raise refused("No such sandbox") from exc
            raise SandboxError(f"the sandbox provider failed ({type(exc).__name__})") from exc

    async def _sandbox(self, sandbox_id: str) -> AsyncSandbox:
        loop = asyncio.get_running_loop()
        held = self._open.get(sandbox_id)
        if held is not None and held[0] is loop:
            self._open.move_to_end(sandbox_id)
            return held[1]
        with self._errors(sandbox_id):
            sandbox = await AsyncSandbox.connect(sandbox_id, request_timeout=30, **self.opts)
        self._keep(sandbox, loop)
        return sandbox

    def _keep(self, sandbox: AsyncSandbox, loop: asyncio.AbstractEventLoop) -> None:
        self._open[sandbox.sandbox_id] = (loop, sandbox)
        while len(self._open) > OPEN_SANDBOXES:
            self._open.popitem(last=False)

    def _path(self, path: str, what: str = "The path leaves the workspace") -> str:
        target = inside(path)
        if target is None:
            raise refused(what)
        return target

    async def _run(
        self, sandbox: AsyncSandbox, command: str, cwd: str, env: dict[str, str], seconds: int
    ) -> Ran:
        """Run ``command`` under ``timeout`` (killed with its process group when the time is up)."""
        out, err = Output(), Output()
        wrapped = f"timeout -k 5 {seconds} /bin/sh -c {shlex.quote(command)}"
        started = time.monotonic()
        code, timed_out = 0, False
        with self._errors(sandbox.sandbox_id):
            try:
                result = await sandbox.commands.run(
                    wrapped,
                    cwd=cwd,
                    envs={**BASE_ENV, **env},
                    timeout=seconds + 15,
                    request_timeout=seconds + 30,
                    on_stdout=out.add,
                    on_stderr=err.add,
                )
                code = result.exit_code
            except CommandExitException as exc:
                code = exc.exit_code
            except TimeoutException:
                code, timed_out = 124, True
        elapsed = time.monotonic() - started
        if code in (124, 137) and elapsed >= seconds:
            timed_out = True
        return Ran(code, out.value(), err.value(), timed_out, round(elapsed, 3))

    async def _info(self, sandbox: AsyncSandbox, path: str, missing: str) -> Any:
        with self._errors(sandbox.sandbox_id, missing):
            return await sandbox.files.get_info(path, request_timeout=30)

    async def _bytes(self, sandbox: AsyncSandbox, path: str, missing: str = "No such file") -> bytes:
        with self._errors(sandbox.sandbox_id, missing):
            data = await sandbox.files.read(path, format="bytes", request_timeout=60)
        return bytes(data)

    # ------------------------------------------------------------------ the provider API

    async def create(self, limits: Limits, metadata: dict[str, str]) -> str:
        ttl = max(60, min(int(limits.ttl_seconds), MAX_TTL))
        with self._errors():
            sandbox = await AsyncSandbox.create(
                template=self.template,
                timeout=ttl,
                metadata={str(k)[:64]: str(v)[:256] for k, v in metadata.items()},
                allow_internet_access=False,
                request_timeout=60,
                **self.opts,
            )
            await sandbox.files.make_dir(ROOT, request_timeout=30)
        self._keep(sandbox, asyncio.get_running_loop())
        return str(sandbox.sandbox_id)

    async def upload(self, sandbox_id: str, archive: bytes, path: str, strip: int = 1) -> int:
        target = self._path(path)
        files = checked_archive(archive, strip, Limits().disk_mb * 1024 * 1024)
        sandbox = await self._sandbox(sandbox_id)
        name = f"{JOBS}/upload-{secrets.token_hex(6)}.tar.gz"
        with self._errors(sandbox_id):
            await sandbox.files.write(name, archive, request_timeout=120)
        command = (
            f"mkdir -p {shlex.quote(target)} && tar -xzf {name} --strip-components={int(strip)} "
            f"-C {shlex.quote(target)} --no-same-owner; status=$?; rm -f {name}; exit $status"
        )
        ran = await self._run(sandbox, command, ROOT, {}, 120)
        if not ran.ok:
            raise SandboxError("the sandbox provider could not unpack the archive")
        return files

    async def list_files(self, sandbox_id: str, path: str, recursive: bool = False) -> list[dict[str, Any]]:
        folder = self._path(path)
        sandbox = await self._sandbox(sandbox_id)
        info = await self._info(sandbox, folder, "No such directory")
        if info.type != FileType.DIR:
            raise refused("No such directory")
        with self._errors(sandbox_id, "No such directory"):
            found = await sandbox.files.list(folder, depth=LIST_DEPTH if recursive else 1, request_timeout=60)
        entries = sorted(
            (
                {
                    "path": posixpath.relpath(e.path, ROOT),
                    "type": "dir" if e.type == FileType.DIR else "file",
                    "size": int(e.size or 0) if e.type != FileType.DIR else 0,
                }
                for e in found
            ),
            key=lambda e: str(e["path"]),
        )
        return entries[:MAX_LISTED]

    async def read(
        self, sandbox_id: str, path: str, start: int = 1, end: int | None = None
    ) -> dict[str, Any]:
        target = self._path(path, "No such file")
        sandbox = await self._sandbox(sandbox_id)
        info = await self._info(sandbox, target, "No such file")
        if info.type == FileType.DIR:
            raise refused("No such file")
        if int(info.size or 0) > MAX_READ:
            raise refused("Files over 10 MB are not read")
        raw = await self._bytes(sandbox, target)
        if b"\0" in raw[:8000]:
            raise refused("Binary files are not read")
        lines = raw.decode("utf-8", errors="replace").splitlines()
        first = max(1, int(start or 1))
        last = min(len(lines), int(end or first + MAX_READ_LINES - 1), first + MAX_READ_LINES - 1)
        return {
            "path": path,
            "start": first,
            "end": last,
            "total_lines": len(lines),
            "content": "\n".join(line[:MAX_LINE] for line in lines[first - 1 : last]),
        }

    async def search(
        self,
        sandbox_id: str,
        pattern: str,
        path: str = "",
        glob: str = "*",
        regex: bool = False,
        limit: int = 50,
    ) -> dict[str, Any]:
        if not 1 <= len(pattern) <= 200:
            raise refused("A pattern of 1 to 200 characters")
        if regex:
            try:
                re.compile(pattern)
            except re.error as exc:
                raise refused(f"Not a valid regular expression: {exc}") from exc
        folder = self._path(path, "No such directory")
        sandbox = await self._sandbox(sandbox_id)
        job = f"{JOBS}/search-{secrets.token_hex(6)}"
        spec = {"root": ROOT, "folder": folder, "pattern": pattern, "regex": bool(regex),
                "glob": str(glob or "*"), "limit": max(1, min(int(limit or 50), 500))}  # fmt: skip
        with self._errors(sandbox_id):
            await sandbox.files.write(f"{job}.py", SEARCH, request_timeout=30)
            await sandbox.files.write(f"{job}.json", json.dumps(spec), request_timeout=30)
        ran = await self._run(sandbox, f"python3 {job}.py {job}.json {job}.out; status=$?; rm -f {job}.py "
                                       f"{job}.json; exit $status", ROOT, {}, 60)  # fmt: skip
        if not ran.ok:
            raise SandboxError("the sandbox provider could not search the files")
        found = json.loads(await self._bytes(sandbox, f"{job}.out"))
        await self._run(sandbox, f"rm -f {job}.out", ROOT, {}, 10)
        if found.get("error"):
            raise refused("No such directory")
        return {"matches": list(found.get("matches") or []), "truncated": bool(found.get("truncated"))}

    async def exec(
        self,
        sandbox_id: str,
        command: str,
        cwd: str = "",
        env: dict[str, str] | None = None,
        seconds: int = 120,
    ) -> Ran:
        if not 1 <= len(command) <= MAX_COMMAND:
            raise refused("A command of 1 to 4000 characters")
        folder = self._path(cwd, "The working directory leaves the workspace")
        variables = {str(k): str(v)[:2000] for k, v in (env or {}).items() if str(k).isidentifier()}
        timeout = max(1, min(int(seconds or 120), MAX_TIMEOUT))
        sandbox = await self._sandbox(sandbox_id)
        return await self._run(sandbox, command, folder, variables, timeout)

    async def write(self, sandbox_id: str, path: str, content: bytes) -> int:
        target = inside(path)
        if target is None or target == ROOT:
            raise refused("A file path inside the workspace")
        sandbox = await self._sandbox(sandbox_id)
        with self._errors(sandbox_id):
            exists = await sandbox.files.exists(target, request_timeout=30)
            if exists and (await sandbox.files.get_info(target, request_timeout=30)).type == FileType.DIR:
                raise refused("A file path inside the workspace")
            await sandbox.files.write(target, content, request_timeout=120)
        return len(content)

    async def read_bytes(self, sandbox_id: str, path: str) -> bytes:
        target = inside(path)
        if target is None:
            raise SandboxError(f"no file {path}")
        sandbox = await self._sandbox(sandbox_id)
        try:
            info = await self._info(sandbox, target, "No such file")
        except SandboxError as exc:
            if "No such file" in str(exc):
                raise SandboxError(f"no file {path}") from exc
            raise
        if info.type == FileType.DIR:
            raise SandboxError(f"no file {path}")
        if int(info.size or 0) > MAX_RAW:
            raise SandboxError("the sandbox provider refused the read (files over 1 MB are not read whole)")
        return await self._bytes(sandbox, target)

    async def diff(self, sandbox_id: str, path: str) -> Change:
        repo = inside(path)
        not_a_repository = "The path is not a git working tree in the workspace"
        if repo is None:
            raise refused(not_a_repository)
        sandbox = await self._sandbox(sandbox_id)
        with self._errors(sandbox_id):
            if not await sandbox.files.exists(f"{repo}/.git", request_timeout=30):
                raise refused(not_a_repository)
        out = "../.agent"  # beside the repository, inside the workspace (as the stand-in keeps it)
        command = (f"mkdir -p {out} && git add -A && git diff --cached --binary --no-color > {out}/diff.patch"
                   f" && git diff --cached --raw -z --no-renames > {out}/raw")  # fmt: skip
        ran = await self._run(sandbox, command, repo, {}, 60)
        if not ran.ok:
            raise refused(ran.stderr[:300] or "git failed")
        agent = f"{posixpath.dirname(repo)}/.agent"
        raw = (await self._bytes(sandbox, f"{agent}/raw")).split(b"\0")
        files = []
        for i in range(0, len(raw) - 1, 2):
            meta, name = raw[i].decode().lstrip(":").split(), raw[i + 1].decode()
            status, mode = meta[4], meta[1]
            content = None
            if status != "D":
                try:
                    content = await self._bytes(sandbox, f"{repo}/{name}")
                except SandboxError:
                    content = None
            files.append(ChangedFile(name, status, mode, content))
        patch = (await self._bytes(sandbox, f"{agent}/diff.patch")).decode("utf-8", errors="replace")
        return Change(patch, files)

    async def destroy(self, sandbox_id: str) -> None:
        self._open.pop(sandbox_id, None)
        with self._errors():
            await AsyncSandbox.kill(sandbox_id, request_timeout=30, **self.opts)  # False: already gone
