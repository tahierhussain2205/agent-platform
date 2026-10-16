"""The E2B sandbox client (B-008) without E2B: the SDK's sandbox class is replaced by a small fake
VM, so the stand-in's rules can be checked here (archives, paths, caps, errors that never carry
the key). ``app check sandbox`` runs the same operations against E2B itself."""

import io
import tarfile
from dataclasses import dataclass, field
from typing import Any

import pytest
from e2b import (
    AuthenticationException,
    CommandExitException,
    FileNotFoundException,
    FileType,
    SandboxNotFoundException,
    TimeoutException,
)

from app.sandbox import Limits, SandboxError, provider_from_settings
from app.sandbox import e2b_provider as module
from app.sandbox.e2b_provider import MAX_OUTPUT, ROOT, E2BSandboxProvider, checked_archive, inside
from app.settings import Settings

KEY = "e2b_secret_key_that_must_never_show_0123456789"


def archive(members: dict[str, bytes], links: dict[str, str] | None = None, top: str = "repo-abc/") -> bytes:
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w:gz") as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(top + name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        for name, target in (links or {}).items():
            info = tarfile.TarInfo(top + name)
            info.type = tarfile.SYMTYPE
            info.linkname = target
            tar.addfile(info)
    return out.getvalue()


@dataclass
class Entry:
    path: str
    type: FileType
    size: int = 0


@dataclass
class Result:
    stdout: str
    stderr: str
    exit_code: int
    error: str | None = None


class Files:
    def __init__(self, vm: "FakeSandbox") -> None:
        self.vm = vm

    async def make_dir(self, path: str, **_: Any) -> bool:
        self.vm.dirs.add(path)
        return True

    async def write(self, path: str, data: bytes | str, **_: Any) -> None:
        self.vm.files[path] = data.encode() if isinstance(data, str) else data

    async def read(self, path: str, format: str = "text", **_: Any) -> bytes:
        if path not in self.vm.files:
            raise FileNotFoundException(f"{path} not found")
        return self.vm.files[path]

    async def exists(self, path: str, **_: Any) -> bool:
        return path in self.vm.files or path in self.vm.dirs

    async def get_info(self, path: str, **_: Any) -> Entry:
        if path in self.vm.dirs:
            return Entry(path, FileType.DIR)
        if path in self.vm.files:
            return Entry(path, FileType.FILE, len(self.vm.files[path]))
        raise FileNotFoundException(f"{path} not found")

    async def list(self, path: str, depth: int = 1, **_: Any) -> list[Entry]:
        found = [
            Entry(p, FileType.FILE, len(d)) for p, d in self.vm.files.items() if p.startswith(path + "/")
        ]
        found += [Entry(p, FileType.DIR) for p in self.vm.dirs if p.startswith(path + "/")]
        if depth == 1:
            found = [e for e in found if "/" not in e.path[len(path) + 1 :]]
        return found


class Commands:
    def __init__(self, vm: "FakeSandbox") -> None:
        self.vm = vm

    async def run(self, cmd: str, cwd: str | None = None, envs: dict[str, str] | None = None,
                  on_stdout: Any = None, on_stderr: Any = None, **kw: Any) -> Result:  # fmt: skip
        self.vm.ran.append({"cmd": cmd, "cwd": cwd, "envs": envs, **kw})
        answer = self.vm.answer(cmd)
        if isinstance(answer, BaseException):
            raise answer
        if on_stdout and answer.stdout:
            on_stdout(answer.stdout)
        if on_stderr and answer.stderr:
            on_stderr(answer.stderr)
        if answer.exit_code:
            raise CommandExitException(answer.stderr, answer.stdout, answer.exit_code, None)
        return answer


@dataclass
class FakeSandbox:
    sandbox_id: str = "sbx-1"
    files: dict[str, bytes] = field(default_factory=dict)
    dirs: set[str] = field(default_factory=set)
    ran: list[dict[str, Any]] = field(default_factory=list)
    answers: list[tuple[str, Result | BaseException]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.files_api, self.commands_api = Files(self), Commands(self)

    def answer(self, cmd: str) -> Result | BaseException:
        for needle, result in self.answers:
            if needle in cmd:
                return result
        return Result("", "", 0)


class FakeSDK:
    """Stands in for ``e2b.AsyncSandbox``: create, connect and kill on one fake VM."""

    def __init__(self) -> None:
        self.vm = FakeSandbox()
        self.created: list[dict[str, Any]] = []
        self.killed: list[str] = []
        self.connects = 0
        self.fail: BaseException | None = None

    def wrap(self) -> Any:
        vm = self.vm

        class Handle:
            sandbox_id = vm.sandbox_id
            files = vm.files_api
            commands = vm.commands_api

        return Handle()

    async def create(self, **kw: Any) -> Any:
        if self.fail:
            raise self.fail
        self.created.append(kw)
        return self.wrap()

    async def connect(self, sandbox_id: str, **kw: Any) -> Any:
        self.connects += 1
        if sandbox_id != self.vm.sandbox_id:
            raise SandboxNotFoundException(f"sandbox {sandbox_id} not found")
        return self.wrap()

    async def kill(self, sandbox_id: str, **kw: Any) -> bool:
        self.killed.append(sandbox_id)
        return sandbox_id == self.vm.sandbox_id


@pytest.fixture
def sdk(monkeypatch: pytest.MonkeyPatch) -> FakeSDK:
    fake = FakeSDK()
    monkeypatch.setattr(module, "AsyncSandbox", fake)
    return fake


def provider() -> E2BSandboxProvider:
    return E2BSandboxProvider(KEY, "agent-platform-sandbox")


# ------------------------------------------------------------------ rules in code


def test_paths_stay_inside_the_workspace() -> None:
    assert inside("") == ROOT
    assert inside("/") == ROOT
    assert inside("acme__cart/src/a.py") == f"{ROOT}/acme__cart/src/a.py"
    assert inside("/acme__cart") == f"{ROOT}/acme__cart"  # absolute means from the workspace
    assert inside("a/../b") == f"{ROOT}/b"
    for leaving in ("..", "../etc/passwd", "a/../../b", "/../x"):
        assert inside(leaving) is None, leaving


def test_archives_are_checked_like_the_stand_in_checks_them() -> None:
    assert checked_archive(archive({"a.py": b"x", "tests/t.py": b"y"}), 1, 10_000) == 2
    with pytest.raises(SandboxError, match="Links and devices are not allowed"):
        checked_archive(archive({"a.py": b"x"}, links={"evil": "/etc/passwd"}), 1, 10_000)
    with pytest.raises(SandboxError, match="Path leaves the workspace"):
        checked_archive(archive({"../../outside.py": b"x"}), 1, 10_000)
    with pytest.raises(SandboxError, match="disk limit"):
        checked_archive(archive({"big.bin": b"x" * 2000}), 1, 1000)
    with pytest.raises(SandboxError, match="Not a gzipped tar"):
        checked_archive(b"not an archive", 1, 1000)


# ------------------------------------------------------------------ the provider on a fake VM


async def test_a_sandbox_is_made_without_internet_from_the_template(sdk: FakeSDK) -> None:
    sandbox = await provider().create(Limits(ttl_seconds=2 * 3600), {"repository": "acme/cart", "org": "o1"})
    assert sandbox == "sbx-1"
    (made,) = sdk.created
    assert made["allow_internet_access"] is False
    assert made["template"] == "agent-platform-sandbox"
    assert made["timeout"] == 3600  # E2B's free plan keeps a sandbox at most an hour
    assert made["metadata"] == {"repository": "acme/cart", "org": "o1"}
    assert ROOT in sdk.vm.dirs


async def test_upload_refuses_unsafe_archives_before_anything_reaches_the_sandbox(sdk: FakeSDK) -> None:
    p = provider()
    with pytest.raises(SandboxError, match="Links and devices"):
        await p.upload("sbx-1", archive({"a.py": b"x"}, links={"l": "/"}), "acme__cart")
    assert sdk.vm.files == {}
    assert sdk.vm.ran == []
    with pytest.raises(SandboxError, match="leaves the workspace"):
        await p.upload("sbx-1", archive({"a.py": b"x"}), "../elsewhere")
    assert await p.upload("sbx-1", archive({"a.py": b"x", "b.py": b"y"}), "acme__cart") == 2
    (unpack,) = sdk.vm.ran
    assert "tar -xzf /home/user/.jobs/upload-" in unpack["cmd"]
    assert f"-C {ROOT}/acme__cart --no-same-owner" in unpack["cmd"]
    assert "--strip-components=1" in unpack["cmd"]


async def test_commands_run_in_the_workspace_with_a_time_limit_and_capped_output(sdk: FakeSDK) -> None:
    p = provider()
    sdk.vm.answers = [("pytest", Result("x" * (3 * MAX_OUTPUT), "boom", 1))]
    ran = await p.exec("sbx-1", "python -m pytest -q", cwd="acme__cart", env={"CI": "1", "bad-name": "x"})
    assert (ran.exit_code, ran.timed_out, ran.ok) == (1, False, False)
    assert ran.stdout.startswith("…(truncated)\n")
    assert len(ran.stdout) <= MAX_OUTPUT + 20
    assert ran.stderr == "boom"
    call = sdk.vm.ran[-1]
    assert call["cwd"] == f"{ROOT}/acme__cart"
    assert call["cmd"].startswith("timeout -k 5 120 /bin/sh -c ")
    assert call["envs"]["CI"] == "1"
    assert "bad-name" not in call["envs"]
    assert call["envs"]["HOME"] == "/home/user"
    with pytest.raises(SandboxError, match="working directory leaves the workspace"):
        await p.exec("sbx-1", "ls", cwd="..")
    with pytest.raises(SandboxError, match="A command of 1 to 4000"):
        await p.exec("sbx-1", "x" * 4001)
    sdk.vm.answers = [("sleep", TimeoutException("too long"))]
    slow = await p.exec("sbx-1", "sleep 999", seconds=5)
    assert slow.timed_out
    assert slow.exit_code == 124


async def test_reads_writes_and_listings_keep_the_stand_ins_shapes(sdk: FakeSDK) -> None:
    p = provider()
    vm = sdk.vm
    vm.dirs |= {ROOT, f"{ROOT}/r", f"{ROOT}/r/src"}
    vm.files[f"{ROOT}/r/src/a.py"] = b"one\ntwo\nthree\n"
    vm.files[f"{ROOT}/r/logo.png"] = b"\x89PNG\0\0"
    assert await p.read("sbx-1", "r/src/a.py", start=2) == {
        "path": "r/src/a.py", "start": 2, "end": 3, "total_lines": 3, "content": "two\nthree",
    }  # fmt: skip
    with pytest.raises(SandboxError, match="Binary files are not read"):
        await p.read("sbx-1", "r/logo.png")
    with pytest.raises(SandboxError, match="No such file"):
        await p.read("sbx-1", "r/missing.py")
    with pytest.raises(SandboxError, match=r"no file r/missing\.py"):
        await p.read_bytes("sbx-1", "r/missing.py")
    assert await p.write("sbx-1", "r/src/b.py", b"new") == 3
    assert vm.files[f"{ROOT}/r/src/b.py"] == b"new"
    for bad in ("", "../x.py", "r/src"):  # the workspace itself, outside it, a directory
        with pytest.raises(SandboxError, match="A file path inside the workspace"):
            await p.write("sbx-1", bad, b"x")
    listed = await p.list_files("sbx-1", "r", recursive=True)
    assert [(e["path"], e["type"]) for e in listed] == [
        ("r/logo.png", "file"), ("r/src", "dir"), ("r/src/a.py", "file"), ("r/src/b.py", "file"),
    ]  # fmt: skip
    with pytest.raises(SandboxError, match="No such directory"):
        await p.list_files("sbx-1", "nowhere")


async def test_a_diff_is_read_from_git_in_the_sandbox(sdk: FakeSDK) -> None:
    p = provider()
    vm = sdk.vm
    vm.dirs |= {ROOT, f"{ROOT}/r", f"{ROOT}/r/.git"}
    with pytest.raises(SandboxError, match="not a git working tree"):
        await p.diff("sbx-1", "elsewhere")
    vm.files[f"{ROOT}/.agent/raw"] = (
        b":100644 100644 aaaa bbbb M\0src/a.py\0:100644 000000 cccc 0000 D\0old.py\0"
    )
    vm.files[f"{ROOT}/.agent/diff.patch"] = b"--- a/src/a.py\n+++ b/src/a.py\n"
    vm.files[f"{ROOT}/r/src/a.py"] = b"changed"
    change = await p.diff("sbx-1", "r")
    assert [(f.path, f.status, f.mode, f.content) for f in change.files] == [
        ("src/a.py", "M", "100644", b"changed"), ("old.py", "D", "000000", None),
    ]  # fmt: skip
    assert change.patch.startswith("--- a/src/a.py")
    git = vm.ran[-1]
    assert git["cwd"] == f"{ROOT}/r"
    assert "git add -A && git diff --cached --binary" in git["cmd"]


async def test_failures_never_show_the_key_and_gone_sandboxes_are_said_so(sdk: FakeSDK) -> None:
    p = provider()
    sdk.fail = AuthenticationException(f"invalid key {KEY}")
    with pytest.raises(SandboxError) as refused:
        await p.create(Limits(), {})
    assert str(refused.value) == "the sandbox provider refused our credentials"
    assert KEY not in str(refused.value)
    with pytest.raises(SandboxError, match="No such sandbox"):
        await p.exec("sbx-gone", "ls")
    await p.destroy("sbx-gone")  # already gone is destroyed
    assert sdk.killed == ["sbx-gone"]


async def test_one_connection_per_sandbox_is_reused(sdk: FakeSDK) -> None:
    p = provider()
    await p.exec("sbx-1", "true")
    await p.exec("sbx-1", "true")
    assert sdk.connects == 1


def test_settings_choose_the_provider() -> None:
    http: Any = None
    e2b = Settings(sandbox_provider="e2b", sandbox_api_key=KEY, sandbox_url=None)
    chosen = provider_from_settings(e2b, http)
    assert isinstance(chosen, E2BSandboxProvider)
    assert provider_from_settings(e2b, http) is chosen  # one client per process
    assert provider_from_settings(Settings(sandbox_provider="e2b", sandbox_api_key=None), http) is None
    local = provider_from_settings(
        Settings(sandbox_url="http://fakes:4100/sandbox/v1", sandbox_api_key="k"), http
    )
    assert type(local).__name__ == "HttpSandboxProvider"
    assert provider_from_settings(Settings(sandbox_url=None, sandbox_api_key="k"), http) is None
