"""Changes to repositories (plan phase 12, spec 8.3), as GitHub adapter operations.

- ``prepare_workspace`` (code only): the task's working copy of a repository at a base commit, with
  a baseline commit so its change can be exported exactly.
- ``get_pull_request``: a pull request with its reviews, review comments and check runs.
- ``push_changes``: the trusted pusher. It runs outside the sandbox and pushes a change exported
  from it, through the GitHub App: blobs, a tree on the base commit's tree, a commit on the base,
  then a new branch or a fast forward of an existing one (never the default branch, never a force
  push, always under ``agent/``); then it finds or opens the pull request. The requester must have
  write access to the repository. It is idempotent: the commit carries the action's key, so a retry
  after a lost answer finds its own commit on the branch. The change's blocking checks run again
  here, just before pushing.
"""

import base64
import json
import re
from typing import TYPE_CHECKING, Any
from uuid import UUID

from app.agent.changes import check_change
from app.db.tenant import tenant_tx
from app.policy.redaction import redact
from app.tools.base import Operation, Source, ToolContext, ToolResult, cap
from app.tools.http import request
from app.vault_service import Credential

if TYPE_CHECKING:
    from app.tools.github import GitHubAdapter

REPO = {"type": "string", "description": "owner/name, for example acme/cart-service."}
SHA = re.compile(r"^[0-9a-f]{40}$")
BRANCH = re.compile(r"^agent/[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*$")
WRITE_LEVELS = ("write", "maintain", "admin")
MARKER = "Agent-Action: "

CHANGE_OPERATIONS = {
    "prepare_workspace": Operation(
        "prepare_workspace",
        "The task's working copy of a repository at a base commit, the default branch's head when none is "
        "given; with push true, the requester must also be able to push there (code only).",
        {
            "type": "object",
            "properties": {"repo": REPO, "base_sha": {"type": "string"}, "push": {"type": "boolean"}},
            "required": ["repo"],
        },
        credential_mode="org_with_check",
        internal=True,
    ),
    "get_pull_request": Operation(
        "get_pull_request",
        "A pull request: its state, branch, reviews, review comments and the results of its checks (at its "
        "head, or at the commit sha given).",
        {
            "type": "object",
            "properties": {
                "repo": REPO,
                "number": {"type": "integer", "minimum": 1},
                "sha": {"type": "string"},
            },
            "required": ["repo", "number"],
        },
        credential_mode="org_with_check",
    ),
    "push_changes": Operation(
        "push_changes",
        "Push the task's checked change to a branch under agent/ and open (or update) its pull request.",
        {
            "type": "object",
            "properties": {
                "repo": REPO,
                "change": {"type": "string", "description": "The diff artifact of this task."},
                "base_sha": {"type": "string"},
                "branch": {"type": "string"},
                "title": {"type": "string"},
                "body": {"type": "string"},
                "pull_number": {"type": "integer"},
            },
            "required": ["repo", "change", "base_sha", "branch", "title"],
        },
        kind="write",
        reversible=True,  # a branch and a pull request can be closed and deleted
        credential_mode="org_with_check",
        approver_rule="requester_or_admin",
        # Planned in code from the task's checked change (only implement_change loads it), never by a
        # model; listed in the console so admins can turn it off.
    ),
}


class GitHubWriteError(Exception):
    pass


async def load_change(ctx: ToolContext, artifact_id: str) -> dict[str, Any] | None:
    """The task's own diff artifact (inline, or in the object store when large)."""
    try:
        UUID(artifact_id)
    except ValueError:
        return None
    async with tenant_tx(ctx.pool, ctx.org_id) as conn:
        row = await (
            await conn.execute(
                "select content, storage_ref from app.artifacts "
                "where id = %s and task_id = %s and kind = 'diff'",
                (artifact_id, ctx.task_id),
            )
        ).fetchone()
    if row is None:
        return None
    if row[0] is not None:
        return dict(row[0])
    if ctx.store is None:
        raise GitHubWriteError("the change is in object storage, which is not configured")
    return dict(json.loads(await ctx.store.get(ctx.org_id, row[1])))


class ChangeOperations:
    def __init__(self, adapter: "GitHubAdapter") -> None:
        self.gh = adapter

    async def _send(
        self, ctx: ToolContext, token: str, method: str, endpoint: str, body: dict[str, Any]
    ) -> tuple[int, Any]:
        response = await request(
            ctx.http,
            method,
            f"{self.gh.api_url}/{endpoint}",
            json=body,
            headers={"authorization": f"Bearer {token}", "accept": "application/vnd.github+json"},
        )
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        return response.status_code, payload

    async def _write(
        self, ctx: ToolContext, token: str, method: str, endpoint: str, body: dict[str, Any]
    ) -> Any:
        status, payload = await self._send(ctx, token, method, endpoint, body)
        if status >= 400:
            raise GitHubWriteError(_message(payload, status))
        return payload

    # ------------------------------------------------------------------ prepare

    async def prepare(
        self, ctx: ToolContext, credential: Credential, repo: str, args: dict[str, Any]
    ) -> ToolResult:
        base = str(args.get("base_sha") or "")
        if base and not SHA.match(base):
            return ToolResult("Give the full base commit sha.", is_error=True)
        if args.get("push"):
            denial = await self._can_write(ctx, credential, repo)
            if denial:
                return ToolResult(denial, is_error=True, data={"denied": True})
        if base:
            ctx.cache[f"github:base:{repo}"] = base
        ws = await self.gh.code.workspace(ctx, credential, repo, baseline=True)
        if base and ws.base_sha != base:
            return ToolResult(
                f"The task's working copy of {repo} is at {ws.base_sha[:7]}, not {base[:7]}.", is_error=True
            )
        info = await self.gh._repo(ctx, credential, repo) or {}
        data = {"repo": repo, "base_sha": ws.base_sha, "private": bool(info.get("private", True)),
                "default_branch": str(info.get("default_branch") or "main")}  # fmt: skip
        return ToolResult(f"Working copy of {repo} at {ws.base_sha[:7]}.", data=data)

    # ------------------------------------------------------------------ pull requests

    async def pull_request(
        self, ctx: ToolContext, credential: Credential, repo: str, args: dict[str, Any]
    ) -> ToolResult:
        number = int(args.get("number") or 0)
        pr = await self.gh._get(ctx, credential.token, f"repos/{repo}/pulls/{number}") if number > 0 else None
        if not pr:
            return ToolResult(f"{repo} has no pull request #{number}.", is_error=True)
        reviews = await self.gh._get(ctx, credential.token, f"repos/{repo}/pulls/{number}/reviews") or []
        comments = await self.gh._get(ctx, credential.token, f"repos/{repo}/pulls/{number}/comments") or []
        head_sha = str(pr["head"]["sha"])
        checked = str(args["sha"]) if SHA.match(str(args.get("sha") or "")) else head_sha
        runs = (
            await self.gh._get(ctx, credential.token, f"repos/{repo}/commits/{checked}/check-runs") or {}
        ).get("check_runs") or []
        info = await self.gh._repo(ctx, credential, repo) or {}
        short = ctx.refs.add(
            Source(
                ref=f"github:{repo}#{number}",
                channel_external_id="",
                channel_name=repo,
                visibility="restricted" if info.get("private", True) else "public",
                ts="",
                label=f"{repo}#{number}",
                permalink=pr.get("html_url"),
                kind="github",
                audience=f"github:{repo}",
            )
        )
        data = {
            "repo": repo,
            "number": number,
            "url": pr.get("html_url"),
            "state": pr.get("state"),
            "private": bool(info.get("private", True)),
            "title": str(pr.get("title") or "")[:256],
            "head_ref": pr["head"]["ref"],
            "head_sha": head_sha,
            "checks_sha": checked,
            "base_ref": pr["base"]["ref"],
            "reviews": [
                {"id": r.get("id"), "state": r.get("state"), "user": (r.get("user") or {}).get("login"),
                 "body": redact(str(r.get("body") or ""))[0][:2000]}
                for r in reviews[-10:]
            ],
            "comments": [
                {"id": c.get("id"), "path": c.get("path"), "line": c.get("line"),
                 "user": (c.get("user") or {}).get("login"),
                 "body": redact(str(c.get("body") or ""))[0][:2000],
                 "review": c.get("pull_request_review_id")}
                for c in comments[-30:]
            ],
            "checks": [
                {"name": r.get("name"), "status": r.get("status"), "conclusion": r.get("conclusion"),
                 "summary": redact(str((r.get("output") or {}).get("summary") or ""))[0][:1000]}
                for r in runs[:20]
            ],
        }  # fmt: skip
        lines = [f"[{short}] {repo}#{number} ({data['state']}): {data['title']} · branch {data['head_ref']} "
                 f"at {head_sha[:7]}"]  # fmt: skip
        lines += [f"Review by {r['user']} ({r['state']}): {r['body']}" for r in data["reviews"] if r["body"]]
        lines += [f"Comment by {c['user']} on {c['path']}:{c['line']}: {c['body']}" for c in data["comments"]]
        lines += [
            f"Check {r['name']}: {r['conclusion'] or r['status']} {r['summary']}".rstrip()
            for r in data["checks"]
        ]
        return ToolResult(cap("\n".join(lines)), data=data)

    # ------------------------------------------------------------------ the trusted pusher

    async def push(
        self, ctx: ToolContext, credential: Credential, repo: str, args: dict[str, Any]
    ) -> ToolResult:
        key = str(args.get("_idempotency_key") or "")
        branch = str(args.get("branch") or "")
        base = str(args.get("base_sha") or "")
        if not key:
            return ToolResult("A push needs its action's key.", is_error=True)
        if not BRANCH.match(branch) or ".." in branch or branch.endswith((".lock", "/")):
            return ToolResult(f"{branch!r} is not a branch under agent/.", is_error=True)
        if not SHA.match(base):
            return ToolResult("Give the full base commit sha.", is_error=True)
        denial = await self._can_write(ctx, credential, repo)
        if denial:
            return ToolResult(denial, is_error=True, data={"denied": True})
        info = await self.gh._repo(ctx, credential, repo) or {}
        default = str(info.get("default_branch") or "main")
        if branch == default:
            return ToolResult("The default branch is never pushed to.", is_error=True)
        change = await load_change(ctx, str(args.get("change") or ""))
        if change is None or change.get("repo") != repo or change.get("base_sha") != base:
            return ToolResult("This task has no such change for that repository and base.", is_error=True)
        files = [
            {**f, "content": base64.b64decode(f["content_b64"]) if f.get("content_b64") is not None else None}
            for f in change.get("files") or []
        ]
        checks = check_change(files, str(change.get("patch") or ""), [], blocking_only=True)
        if checks.blocked:
            return ToolResult("Not pushed: " + " ".join(checks.blocked), is_error=True)
        token = credential.token
        marker = MARKER + key
        ref = await self.gh._get(ctx, token, f"repos/{repo}/git/ref/heads/{branch}")
        if ref is not None:
            head = str(ref["object"]["sha"])
            if await self._ours(ctx, token, repo, head, marker):
                sha = head  # pushed already (a retry after a lost answer)
            elif head == base:
                sha = await self._commit(ctx, token, repo, base, files, args, marker)
                await self._write(ctx, token, "PATCH", f"repos/{repo}/git/refs/heads/{branch}",
                                  {"sha": sha, "force": False})  # fmt: skip
            else:
                return ToolResult(
                    f"Not pushed: {branch} moved to {head[:7]} since this change was prepared on {base[:7]}; "
                    "ask me again to prepare the change on its new head.",
                    is_error=True,
                )
        elif args.get("pull_number"):
            return ToolResult(
                f"Not pushed: the pull request's branch {branch} no longer exists.", is_error=True
            )
        else:
            sha = await self._commit(ctx, token, repo, base, files, args, marker)
            status, payload = await self._send(
                ctx, token, "POST", f"repos/{repo}/git/refs", {"ref": f"refs/heads/{branch}", "sha": sha}
            )
            if status >= 400:
                again = await self.gh._get(ctx, token, f"repos/{repo}/git/ref/heads/{branch}")
                head = str(again["object"]["sha"]) if again else ""
                if not head or not await self._ours(ctx, token, repo, head, marker):
                    raise GitHubWriteError(_message(payload, status))
                sha = head
        pr = await self._pull(ctx, token, repo, branch, default, args)
        if args.get("pull_number"):
            await self._send(ctx, token, "POST", f"repos/{repo}/issues/{pr['number']}/comments",
                             {"body": f"Pushed {sha[:7]}: {str(args.get('title') or '')[:200]}"})  # fmt: skip
        url = str(pr.get("html_url") or "")
        data = {"url": url, "number": int(pr["number"]), "branch": branch, "sha": sha, "repo": repo,
                "target_ref": f"github:{repo}#{pr['number']}",
                "files": [f["path"] for f in files]}  # fmt: skip
        return ToolResult(f"Pushed {sha[:7]} to {branch}: {url}", data=data)

    async def _can_write(self, ctx: ToolContext, credential: Credential, repo: str) -> str | None:
        if credential.scope == "user":
            return None  # the requester's own token: GitHub applies their access
        login = await self.gh._login(ctx)
        if login is None:
            return f"Link your GitHub account in the console so I can check you may push to {repo}."
        body = await self.gh._get(ctx, credential.token, f"repos/{repo}/collaborators/{login}/permission")
        if not body or body.get("permission") not in WRITE_LEVELS:
            return f"Access denied: your GitHub account ({login}) can't push to {repo}."
        return None

    async def _ours(self, ctx: ToolContext, token: str, repo: str, sha: str, marker: str) -> bool:
        commit = await self.gh._get(ctx, token, f"repos/{repo}/git/commits/{sha}")
        return bool(commit) and marker in str(commit.get("message") or "").splitlines()

    async def _commit(
        self,
        ctx: ToolContext,
        token: str,
        repo: str,
        base: str,
        files: list[dict[str, Any]],
        args: dict[str, Any],
        marker: str,
    ) -> str:
        parent = await self.gh._get(ctx, token, f"repos/{repo}/git/commits/{base}")
        if not parent:
            raise GitHubWriteError(f"the base commit {base[:7]} is not in {repo}")
        entries: list[dict[str, Any]] = []
        for f in files:
            if f["content"] is None:
                entries.append({"path": f["path"], "mode": "100644", "type": "blob", "sha": None})
                continue
            encoded = base64.b64encode(f["content"]).decode()
            blob = await self._write(
                ctx, token, "POST", f"repos/{repo}/git/blobs", {"content": encoded, "encoding": "base64"}
            )
            entries.append(
                {"path": f["path"], "mode": f.get("mode") or "100644", "type": "blob", "sha": blob["sha"]}
            )
        tree = await self._write(ctx, token, "POST", f"repos/{repo}/git/trees",
                                 {"base_tree": parent["tree"]["sha"], "tree": entries})  # fmt: skip
        message = (
            f"{str(args.get('title') or 'Agent change')[:200]}\n\n{str(args.get('summary') or '')[:2000]}"
        )
        commit = await self._write(ctx, token, "POST", f"repos/{repo}/git/commits",
                                   {"message": message.rstrip() + f"\n\n{marker}\n", "tree": tree["sha"],
                                    "parents": [base]})  # fmt: skip
        return str(commit["sha"])

    async def _pull(
        self, ctx: ToolContext, token: str, repo: str, branch: str, default: str, args: dict[str, Any]
    ) -> dict[str, Any]:
        owner = repo.split("/")[0]
        for attempt in range(2):
            found = await self.gh._get(
                ctx, token, f"repos/{repo}/pulls", head=f"{owner}:{branch}", state="open"
            )
            if found:
                return dict(found[0])
            if attempt:
                break
            status, payload = await self._send(ctx, token, "POST", f"repos/{repo}/pulls", {
                "title": str(args.get("title") or "Agent change")[:256], "head": branch, "base": default,
                "body": str(args.get("body") or "")[:60000], "draft": False})  # fmt: skip
            if status < 400:
                return dict(payload)
            if "already exists" not in json.dumps(payload):
                raise GitHubWriteError(_message(payload, status))
        raise GitHubWriteError("the pull request could not be opened")


def _message(payload: Any, status: int) -> str:
    if isinstance(payload, dict):
        errors = payload.get("errors") or []
        detail = "; ".join(str(e.get("message")) for e in errors if isinstance(e, dict) and e.get("message"))
        return (detail or str(payload.get("message") or f"HTTP {status}"))[:300]
    return f"HTTP {status}"
