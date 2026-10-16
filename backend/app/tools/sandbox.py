"""Sandbox tools (plan phase 12): write and edit files, run commands and show the change, in the
task's own working copy of a repository.

The working copy is the task's sandbox at its base commit: no network, no credential. These tools
change only that copy. Nothing outside sees it until the change is exported, checked in code and
pushed, and the push is a write through the policy. So in the policy's terms they are reads: the
playbook loads them and the policy audits every call. The implementation step prepares the copy
(``github.prepare_workspace``) before the implementer starts; these tools never create one.

Secret files and workflow files are never written here (the checks at export block them too, in
case a command writes them). Command output is redacted and cut to its end.
"""

import base64
from typing import Any

from app.policy.redaction import redact
from app.sandbox import SandboxError, SandboxProvider, provider_from_settings
from app.sandbox.workspaces import Workspace, Workspaces
from app.tools.base import Operation, Target, ToolContext, ToolResult, cap
from app.tools.github_code import clean_path, secret_path

REPO = {"type": "string", "description": "owner/name of the repository being changed."}
PATH = {"type": "string", "description": "A path inside the repository, like src/pricing/cart.py."}
MAX_FILE = 200_000
MAX_TIMEOUT = 600
WORKFLOWS = ".github/workflows/"

OPERATIONS = {
    "write_file": Operation(
        "write_file",
        "Create or replace a file in the task's working copy of the repository, with its whole content.",
        {
            "type": "object",
            "properties": {"repo": REPO, "path": PATH, "content": {"type": "string"}},
            "required": ["repo", "path", "content"],
        },
    ),
    "edit_file": Operation(
        "edit_file",
        "Replace one exact piece of text in a file of the working copy. old must appear exactly once in the "
        "file: include enough surrounding lines to make it unique. new replaces it.",
        {
            "type": "object",
            "properties": {
                "repo": REPO,
                "path": PATH,
                "old": {"type": "string"},
                "new": {"type": "string"},
            },
            "required": ["repo", "path", "old", "new"],
        },
    ),
    "run_command": Operation(
        "run_command",
        "Run a shell command in the repository's working copy, for example its tests. There is no network. "
        "At most 10 minutes; long output is cut to its end.",
        {
            "type": "object",
            "properties": {
                "repo": REPO,
                "command": {"type": "string", "maxLength": 2000},
                "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": MAX_TIMEOUT},
            },
            "required": ["repo", "command"],
        },
    ),
    "show_diff": Operation(
        "show_diff",
        "Show the change made so far in the working copy, as a unified diff.",
        {"type": "object", "properties": {"repo": REPO}, "required": ["repo"]},
    ),
    "export_change": Operation(
        "export_change",
        "The working copy's change: the patch and every changed file (code only).",
        {"type": "object", "properties": {"repo": REPO}, "required": ["repo"]},
        internal=True,
    ),
}


def writable(path: str) -> str | None:
    """Why a path may not be written, or None."""
    if path.startswith(".git/") or path == ".git" or "/.git/" in path:
        return "The repository's .git directory is not written."
    if path.startswith(WORKFLOWS):
        return "Workflow files (.github/workflows/) are not changed by the agent."
    if secret_path(path):
        return f"{path} looks like a secrets file; it is never written."
    return None


class SandboxAdapter:
    key = "sandbox"

    def __init__(self) -> None:
        self.operations = OPERATIONS

    def provider(self, ctx: ToolContext) -> SandboxProvider:
        provider = provider_from_settings(ctx.settings, ctx.http)
        if provider is None:
            raise SandboxError("no sandbox provider is configured")
        return provider

    async def resolve_target(self, ctx: ToolContext, operation: str, args: dict[str, Any]) -> Target | None:
        return None

    async def workspace(self, ctx: ToolContext, repo: str) -> Workspace:
        if ctx.task_id is None:
            raise SandboxError("working copies exist within a task only")
        found = await Workspaces(ctx.pool, self.provider(ctx)).find(ctx.org_id, ctx.task_id, repo)
        if found is None:
            raise SandboxError(f"this task has no working copy of {repo}")
        return found

    async def execute(
        self, ctx: ToolContext, operation: str, args: dict[str, Any], credential: Any = None
    ) -> ToolResult:
        repo = str(args.get("repo") or "").strip()
        try:
            ws = await self.workspace(ctx, repo)
            provider = self.provider(ctx)
            if operation in ("write_file", "edit_file"):
                return await self._write(ctx, provider, ws, operation, args)
            if operation == "run_command":
                return await self.run(
                    provider, ws, str(args.get("command") or ""), args.get("timeout_seconds")
                )
            if operation == "show_diff":
                change = await provider.diff(ws.sandbox_id, ws.root)
                listed = ", ".join(f"{f.path} ({f.status})" for f in change.files) or "no files"
                return ToolResult(
                    cap(redact(f"Changed: {listed}\n{change.patch or '(no change yet)'}")[0]),
                    data={"files": [f.path for f in change.files]},
                )
            if operation == "export_change":
                change = await provider.diff(ws.sandbox_id, ws.root)
                files = [
                    {
                        "path": f.path,
                        "status": f.status,
                        "mode": f.mode,
                        "content_b64": base64.b64encode(f.content).decode()
                        if f.content is not None
                        else None,
                    }
                    for f in change.files
                ]
                data = {"repo": repo, "base_sha": ws.base_sha, "patch": change.patch, "files": files}
                return ToolResult(f"{len(files)} files changed in {repo}.", data=data)
        except SandboxError as exc:
            return ToolResult(f"The working copy is not available: {exc}.", is_error=True)
        return ToolResult(f"Unknown operation {operation}.", is_error=True)

    async def _write(
        self, ctx: ToolContext, provider: SandboxProvider, ws: Workspace, operation: str, args: dict[str, Any]
    ) -> ToolResult:
        path = clean_path(str(args.get("path") or ""))
        if not path:
            return ToolResult("Give a file path inside the repository.", is_error=True)
        refused = writable(path)
        if refused:
            return ToolResult(refused, is_error=True)
        if operation == "write_file":
            content = str(args.get("content") or "")
        else:
            old, new = str(args.get("old") or ""), str(args.get("new") or "")
            if not old:
                return ToolResult("Give the exact text to replace (old).", is_error=True)
            try:
                current = (await provider.read_bytes(ws.sandbox_id, ws.path(path))).decode("utf-8")
            except UnicodeDecodeError:
                return ToolResult(f"{path} is not a text file.", is_error=True)
            except SandboxError:
                return ToolResult(f"{path} does not exist; create it with write_file.", is_error=True)
            count = current.count(old)
            if count != 1:
                where = "does not appear" if count == 0 else f"appears {count} times"
                return ToolResult(
                    f"The text to replace {where} in {path}; give it exactly, with enough context "
                    "to be unique.",
                    is_error=True,
                )
            content = current.replace(old, new, 1)
        if len(content) > MAX_FILE:
            return ToolResult(
                f"{path} would be over {MAX_FILE:,} characters; keep changes small.", is_error=True
            )
        written = await provider.write(ws.sandbox_id, ws.path(path), content.encode())
        verb = "Wrote" if operation == "write_file" else "Edited"
        return ToolResult(f"{verb} {path} ({written:,} bytes).", data={"path": path, "bytes": written})

    async def run(
        self, provider: SandboxProvider, ws: Workspace, command: str, limit: Any = None
    ) -> ToolResult:
        if not command.strip():
            return ToolResult("Give a command to run.", is_error=True)
        seconds = max(1, min(int(limit or 300), MAX_TIMEOUT))
        ran = await provider.exec(ws.sandbox_id, command, cwd=ws.root, seconds=seconds)
        status = "timed out" if ran.timed_out else f"exit {ran.exit_code}"
        text = f"$ {command}\n{status} ({ran.seconds:.1f} s)"
        if ran.stdout.strip():
            text += f"\n--- stdout ---\n{ran.stdout[-8000:]}"
        if ran.stderr.strip():
            text += f"\n--- stderr ---\n{ran.stderr[-3000:]}"
        data = {"command": command, "exit_code": ran.exit_code, "timed_out": ran.timed_out,
                "tail": redact((ran.stdout + "\n" + ran.stderr)[-2000:])[0]}  # fmt: skip
        return ToolResult(cap(redact(text)[0]), data=data)
