"""Checks on a change before it is pushed (plan phase 12, spec 8.3), in code.

The change is what left the sandbox: the patch and each changed file with its mode and content.
Some findings block the push: secrets in added lines, workflow files, unsafe paths (``..``, ``.git``,
links, submodules), binary files, no change at all. Others need a person's approval even in Auto:
a large change, protected paths (the repository's list), generated files, tests that could not run,
no test added. The pusher runs the blocking checks again just before it pushes.
"""

import fnmatch
import re
from dataclasses import dataclass, field
from typing import Any

from app.policy.redaction import PATTERNS
from app.tools.github_code import secret_path

MAX_FILES = 20
MAX_LINES = 800
WORKFLOWS = ".github/workflows/"
STRONG = {"private key", "slack token", "slack webhook", "github token", "anthropic key", "api key",
          "aws access key", "google api key", "jwt", "password in url"}  # fmt: skip
QUOTED_SECRET = re.compile(
    r"(?i)\b\w*(?:password|passwd|secret|api_?key|access_?token|auth_?token|private_?key|client_?secret)\w*"
    r"\s*[:=]\s*[\"']([A-Za-z0-9+/_=.-]{12,})[\"']"
)
PLACEHOLDER = re.compile(r"(?i)(change[-_]?me|example|placeholder|dummy|x{6,}|your[-_]|<|\$\{|test|fake)")
GENERATED = ("*.min.js", "*.min.css", "*.lock", "package-lock.json", "*.pb.go", "*_pb2.py", "*.generated.*")
GENERATED_DIRS = ("dist/", "build/", "vendor/", "node_modules/", "third_party/")
TEST_FILE = re.compile(
    r"(^|/)(tests?|__tests__|spec)/|(^|/)test_[^/]+$|_test\.[a-z]+$|\.(test|spec)\.[a-z]+$"
)


@dataclass
class ChangeChecks:
    blocked: list[str] = field(default_factory=list)
    ask: list[str] = field(default_factory=list)
    files: int = 0
    added: int = 0
    removed: int = 0
    tests: list[str] = field(default_factory=list)  # test files the change adds or edits

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


def unsafe(path: str) -> bool:
    parts = path.split("/")
    return (
        not path
        or path.startswith("/")
        or "\\" in path
        or "\0" in path
        or any(p in ("", ".", "..") for p in parts)
        or any(p.lower() == ".git" for p in parts)
    )


def protected(path: str, patterns: list[str]) -> bool:
    for pattern in patterns:
        p = pattern.strip().lstrip("/")
        if not p:
            continue
        if p.endswith("/") and path.startswith(p):
            return True
        if fnmatch.fnmatch(path, p) or fnmatch.fnmatch(path, p.rstrip("/") + "/*"):
            return True
    return False


def generated(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return any(fnmatch.fnmatch(name, g) for g in GENERATED) or any(
        path.startswith(d) or f"/{d}" in path for d in GENERATED_DIRS
    )


def secrets_in(line: str) -> list[str]:
    found = [label for label, pattern in PATTERNS if label in STRONG and pattern.search(line)]
    m = QUOTED_SECRET.search(line)
    if m and not PLACEHOLDER.search(m.group(1)):
        found.append("secret")
    return found


def added_lines(patch: str) -> list[tuple[str, str]]:
    """(file, line) for every added line of a unified diff."""
    out: list[tuple[str, str]] = []
    current = ""
    for line in patch.splitlines():
        if line.startswith("+++ "):
            current = line[4:].removeprefix("b/")
        elif line.startswith("+") and not line.startswith("+++"):
            out.append((current, line[1:]))
    return out


def check_change(
    files: list[dict[str, Any]], patch: str, protected_paths: list[str], blocking_only: bool = False
) -> ChangeChecks:
    """``files``: ``{"path", "status", "mode", "content"}`` (content as bytes, None when deleted)."""
    checks = ChangeChecks(files=len(files))
    if not files:
        checks.blocked.append("There is no change to push.")
    for line in patch.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            checks.added += 1
        elif line.startswith("-") and not line.startswith("---"):
            checks.removed += 1
    for f in files:
        path, mode, content = str(f["path"]), str(f.get("mode") or "100644"), f.get("content")
        if unsafe(path):
            checks.blocked.append(f"Unsafe path: {path[:200]!r}.")
            continue
        if mode == "120000":
            checks.blocked.append(f"{path} is a symbolic link; links are not pushed.")
        elif mode == "160000":
            checks.blocked.append(f"{path} is a submodule; submodules are not pushed.")
        elif mode not in ("100644", "100755") and content is not None:
            checks.blocked.append(f"{path} has an unsupported file mode {mode}.")
        if path.startswith(WORKFLOWS):
            checks.blocked.append(f"{path} is a workflow file; the agent never changes CI workflows.")
        if content is not None and secret_path(path):
            checks.blocked.append(f"{path} looks like a secrets file.")
        if content is not None and (b"\0" in content[:8000] or not _text(content)):
            checks.blocked.append(f"{path} is a binary file; the agent pushes text changes only.")
        if TEST_FILE.search(path) and content is not None:
            checks.tests.append(path)
        if not blocking_only:
            if protected(path, protected_paths):
                checks.ask.append(f"{path} is a protected path.")
            if generated(path):
                checks.ask.append(f"{path} looks generated or vendored.")
    for path, line in added_lines(patch):
        kinds = secrets_in(line)
        if kinds:
            checks.blocked.append(f"{path} adds what looks like a secret ({', '.join(sorted(set(kinds)))}).")
    if not blocking_only and (checks.files > MAX_FILES or checks.added + checks.removed > MAX_LINES):
        checks.ask.append(
            f"The change is large: {checks.files} files, +{checks.added} -{checks.removed} lines "
            f"(more than {MAX_FILES} files or {MAX_LINES} lines needs a person's approval)."
        )
    checks.blocked = list(dict.fromkeys(checks.blocked))[:20]
    checks.ask = list(dict.fromkeys(checks.ask))[:20]
    return checks


def _text(content: bytes) -> bool:
    try:
        content.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True
