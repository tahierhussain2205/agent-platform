"""Code and recent changes for investigations (plan phase 11), as GitHub adapter operations.

- **Code** (``search_code``, ``read_code``, ``list_files``) is read in the task's sandbox, at the
  commit pinned when the task first read the repository. The platform fetches the archive with
  the installation's read only token; the token never enters the sandbox.
- **Recent changes** (``list_commits``, ``get_commit``, ``list_deployments``) come from the REST API.

Secret files (keys, ``.env`` files, credentials) are never read or searched. Output is capped and
redacted; every result cites the commit it was read at.
"""

import fnmatch
from typing import TYPE_CHECKING, Any

from app.policy.redaction import redact
from app.sandbox import SandboxError, provider_from_settings
from app.sandbox.workspaces import Workspace, Workspaces
from app.tools.base import Operation, Source, ToolContext, ToolResult, cap
from app.tools.http import request
from app.vault_service import Credential

if TYPE_CHECKING:
    from app.tools.github import GitHubAdapter

REPO = {"type": "string", "description": "owner/name, for example acme/cart-service."}
MAX_READ = 400
SECRET_GLOBS = (
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*.jks",
    "*.keystore",
    "*.tfstate",
    "*.tfvars",
    "id_rsa*",
    "id_dsa*",
    "id_ecdsa*",
    "id_ed25519*",
    ".npmrc",
    ".pypirc",
    ".netrc",
    ".git-credentials",
    "credentials",
    "credentials.*",
    "*service-account*.json",
    "*service_account*.json",
    "secrets.*",
    "secret.*",
)
SAFE_SUFFIXES = (".example", ".sample", ".template", ".dist")

CODE_OPERATIONS = {
    "search_code": Operation(
        "search_code",
        "Search a repository's code at the task's pinned commit: text (or a regular expression with regex "
        "true), optionally in a directory (path) and files matching a pattern (glob, e.g. *.py). Returns "
        "matching lines with refs.",
        {
            "type": "object",
            "properties": {
                "repo": REPO,
                "pattern": {"type": "string", "maxLength": 200},
                "path": {"type": "string"},
                "glob": {"type": "string"},
                "regex": {"type": "boolean"},
            },
            "required": ["repo", "pattern"],
        },
        credential_mode="org_with_check",
    ),
    "read_code": Operation(
        "read_code",
        "Read lines of a file at the task's pinned commit (start and end lines, at most 400).",
        {
            "type": "object",
            "properties": {
                "repo": REPO,
                "path": {"type": "string"},
                "start": {"type": "integer", "minimum": 1},
                "end": {"type": "integer", "minimum": 1},
            },
            "required": ["repo", "path"],
        },
        credential_mode="org_with_check",
    ),
    "list_files": Operation(
        "list_files",
        "List the files of a repository directory at the task's pinned commit (recursive for a tree).",
        {
            "type": "object",
            "properties": {"repo": REPO, "path": {"type": "string"}, "recursive": {"type": "boolean"}},
            "required": ["repo"],
        },
        credential_mode="org_with_check",
    ),
    "list_commits": Operation(
        "list_commits",
        "Recent commits of a repository, newest first, optionally in a time window (since, until as ISO "
        "times) and touching a path, with the files each changed.",
        {
            "type": "object",
            "properties": {
                "repo": REPO,
                "since": {"type": "string"},
                "until": {"type": "string"},
                "path": {"type": "string"},
            },
            "required": ["repo"],
        },
        credential_mode="org_with_check",
    ),
    "get_commit": Operation(
        "get_commit",
        "One commit: message, author, time and each changed file with its diff.",
        {
            "type": "object",
            "properties": {"repo": REPO, "sha": {"type": "string"}},
            "required": ["repo", "sha"],
        },
        credential_mode="org_with_check",
    ),
    "list_deployments": Operation(
        "list_deployments",
        "Deployments of a repository, newest first: which commit went to which environment, and when.",
        {
            "type": "object",
            "properties": {"repo": REPO, "environment": {"type": "string"}},
            "required": ["repo"],
        },
        credential_mode="org_with_check",
    ),
}


def secret_path(path: str) -> bool:
    name = path.rsplit("/", 1)[-1].lower()
    if name.endswith(SAFE_SUFFIXES):
        return False
    return any(fnmatch.fnmatch(name, g) for g in SECRET_GLOBS)


def clean_path(path: str) -> str | None:
    """A relative path inside the repository, or None."""
    p = path.strip().lstrip("/")
    if ".." in p.split("/") or "\\" in p or "\0" in p:
        return None
    return p


class CodeOperations:
    """The investigation operations of the GitHub adapter."""

    def __init__(self, adapter: "GitHubAdapter") -> None:
        self.gh = adapter

    async def _private(self, ctx: ToolContext, credential: Credential, repo: str) -> bool:
        info = await self.gh._repo(ctx, credential, repo) or {}
        return bool(info.get("private", True))

    def _cite(
        self, ctx: ToolContext, repo: str, private: bool, ref: str, label: str, link: str | None
    ) -> str:
        return ctx.refs.add(
            Source(
                ref=f"github:{ref}",
                channel_external_id="",
                channel_name=repo,
                visibility="restricted" if private else "public",
                ts="",
                label=label[:80],
                permalink=link,
                kind="github",
                audience=f"github:{repo}",
            )
        )

    async def workspace(
        self, ctx: ToolContext, credential: Credential, repo: str, baseline: bool = False
    ) -> Workspace:
        provider = provider_from_settings(ctx.settings, ctx.http)
        if provider is None:
            raise SandboxError(
                "no sandbox provider is configured, so code cannot be searched; "
                "read single files with read_file"
            )
        if ctx.task_id is None:
            raise SandboxError("code is read within a task only")
        info = await self.gh._repo(ctx, credential, repo) or {}
        branch = str(info.get("default_branch") or "main")

        async def head() -> str:
            pinned = ctx.cache.get(f"github:base:{repo}")  # a past incident's commit, chosen by the task
            if pinned:
                return str(pinned)
            body = await self.gh._get(ctx, credential.token, f"repos/{repo}/commits/{branch}")
            if not body or not body.get("sha"):
                raise SandboxError(f"{repo} has no commit on {branch}")
            return str(body["sha"])

        async def archive(sha: str) -> bytes:
            response = await request(
                ctx.http,
                "GET",
                f"{self.gh.api_url}/repos/{repo}/tarball/{sha}",
                headers={"authorization": f"Bearer {credential.token}"},
                follow_redirects=True,
            )
            if response.status_code != 200:
                raise SandboxError(f"GitHub did not provide the archive of {repo} ({response.status_code})")
            return response.content

        return await Workspaces(ctx.pool, provider).ensure(
            ctx.org_id, ctx.task_id, repo, head, archive, baseline=baseline
        )

    async def search(
        self, ctx: ToolContext, credential: Credential, repo: str, args: dict[str, Any]
    ) -> ToolResult:
        path = clean_path(str(args.get("path") or ""))
        if path is None:
            return ToolResult("Give a directory inside the repository.", is_error=True)
        ws = await self.workspace(ctx, credential, repo)
        result = await self.gh.sandbox_provider(ctx).search(
            ws.sandbox_id,
            str(args.get("pattern") or ""),
            ws.path(path),
            str(args.get("glob") or "*"),
            bool(args.get("regex")),
            limit=60,
        )
        private = await self._private(ctx, credential, repo)
        by_file: dict[str, list[dict[str, Any]]] = {}
        for m in result.get("matches") or []:
            relative = str(m["path"]).removeprefix(ws.root + "/")
            if not secret_path(relative):
                by_file.setdefault(relative, []).append(m)
        count = sum(map(len, by_file.values()))
        lines = [f"Matches in {repo}@{ws.base_sha[:7]} for {args.get('pattern')!r} ({count}):"]
        for relative, found in list(by_file.items())[:25]:
            first, last = found[0]["line"], found[-1]["line"]
            link = f"https://github.com/{repo}/blob/{ws.base_sha}/{relative}#L{first}-L{last}"
            short = self._cite(
                ctx,
                repo,
                private,
                f"{repo}@{ws.base_sha[:7]}:{relative}#L{first}-L{last}",
                f"{repo}/{relative}#L{first}",
                link,
            )
            hits = "; ".join(f"L{m['line']}: {str(m['text']).strip()[:160]}" for m in found[:6])
            lines.append(f"[{short}] {relative}: {hits}")
        if not by_file:
            lines.append("No matches.")
        if result.get("truncated"):
            lines.append("(More matches exist: narrow the pattern or the path.)")
        data = {"repo": repo, "base_sha": ws.base_sha, "files": list(by_file)}
        return ToolResult(cap(redact("\n".join(lines))[0]), data=data)

    async def read(
        self, ctx: ToolContext, credential: Credential, repo: str, args: dict[str, Any]
    ) -> ToolResult:
        path = clean_path(str(args.get("path") or ""))
        if not path:
            return ToolResult("Give a file path inside the repository.", is_error=True)
        if secret_path(path):
            return ToolResult(f"{path} looks like a secrets file; it is never read.", is_error=True)
        ws = await self.workspace(ctx, credential, repo)
        start = max(1, int(args.get("start") or 1))
        end = max(start, min(int(args.get("end") or start + MAX_READ - 1), start + MAX_READ - 1))
        try:
            body = await self.gh.sandbox_provider(ctx).read(ws.sandbox_id, ws.path(path), start, end)
        except SandboxError:
            return ToolResult(f"{repo} has no readable file {path} at {ws.base_sha[:7]}.", is_error=True)
        first, last, total = int(body["start"]), int(body["end"]), int(body["total_lines"])
        link = f"https://github.com/{repo}/blob/{ws.base_sha}/{path}#L{first}-L{last}"
        short = self._cite(
            ctx,
            repo,
            await self._private(ctx, credential, repo),
            f"{repo}@{ws.base_sha[:7]}:{path}#L{first}-L{last}",
            f"{repo}/{path}#L{first}",
            link,
        )
        numbered = "\n".join(
            f"{n}: {line}" for n, line in enumerate(str(body["content"]).splitlines(), first)
        )
        text = f"[{short}] {repo}@{ws.base_sha[:7]} {path} lines {first}-{last} of {total}:\n{numbered}"
        data = {"repo": repo, "path": path, "base_sha": ws.base_sha, "start": first, "end": last}
        return ToolResult(cap(redact(text)[0]), data=data)

    async def files(
        self, ctx: ToolContext, credential: Credential, repo: str, args: dict[str, Any]
    ) -> ToolResult:
        path = clean_path(str(args.get("path") or ""))
        if path is None:
            return ToolResult("Give a directory inside the repository.", is_error=True)
        ws = await self.workspace(ctx, credential, repo)
        try:
            entries = await self.gh.sandbox_provider(ctx).list_files(
                ws.sandbox_id, ws.path(path), bool(args.get("recursive"))
            )
        except SandboxError:
            return ToolResult(f"{repo} has no directory {path or '/'}.", is_error=True)
        shown = [e for e in entries if not secret_path(str(e["path"]))][:300]
        names = [
            str(e["path"]).removeprefix(ws.root + "/") + ("/" if e["type"] == "dir" else "") for e in shown
        ]
        text = f"Files of {repo}@{ws.base_sha[:7]} {path or '/'} ({len(names)}):\n" + "\n".join(names)
        return ToolResult(cap(text), data={"repo": repo, "base_sha": ws.base_sha, "files": names})

    async def commits(
        self, ctx: ToolContext, credential: Credential, repo: str, args: dict[str, Any]
    ) -> ToolResult:
        params = {k: str(args[k]) for k in ("since", "until", "path") if args.get(k)}
        found = (
            await self.gh._get(ctx, credential.token, f"repos/{repo}/commits", per_page=30, **params) or []
        )
        private = await self._private(ctx, credential, repo)
        lines = [
            f"Commits of {repo}"
            + (f" touching {params['path']}" if params.get("path") else "")
            + f" ({len(found)}):"
        ]
        data = []
        for c in found[:15]:
            sha = str(c["sha"])
            detail = await self.gh._get(ctx, credential.token, f"repos/{repo}/commits/{sha}") or {}
            files = [
                f"{f['filename']} +{f.get('additions', 0)} -{f.get('deletions', 0)}"
                for f in detail.get("files") or []
            ]
            when = str(c["commit"]["author"]["date"])[:16].replace("T", " ")
            author = (c.get("author") or {}).get("login") or c["commit"]["author"].get("name")
            message = str(c["commit"]["message"]).splitlines()[0][:160]
            short = self._cite(
                ctx, repo, private, f"{repo}@{sha[:7]}", f"commit {sha[:7]}", c.get("html_url")
            )
            lines.append(
                f"[{short}] {sha[:7]} {when} {author}: {message} ({', '.join(files[:8]) or 'no files'})"
            )
            data.append(
                {
                    "sha": sha,
                    "date": c["commit"]["author"]["date"],
                    "author": author,
                    "message": message,
                    "files": [f["filename"] for f in detail.get("files") or []],
                }
            )
        if not found:
            lines.append("No commits match.")
        return ToolResult(cap(redact("\n".join(lines))[0]), data={"repo": repo, "commits": data})

    async def commit(self, ctx: ToolContext, credential: Credential, repo: str, sha: str) -> ToolResult:
        if not sha.isalnum() or not 4 <= len(sha) <= 64:
            return ToolResult("Give a commit sha.", is_error=True)
        c = await self.gh._get(ctx, credential.token, f"repos/{repo}/commits/{sha}")
        if not c:
            return ToolResult(f"{repo} has no commit {sha}.", is_error=True)
        full = str(c["sha"])
        short = self._cite(
            ctx,
            repo,
            await self._private(ctx, credential, repo),
            f"{repo}@{full[:7]}",
            f"commit {full[:7]}",
            c.get("html_url"),
        )
        head = (
            f"[{short}] {full[:7]} {str(c['commit']['author']['date'])[:16]} "
            f"{(c.get('author') or {}).get('login')}: {c['commit']['message']}"
        )
        parts = [head]
        for f in (c.get("files") or [])[:20]:
            if secret_path(str(f["filename"])):
                parts.append(f"{f['filename']} ({f.get('status')}): a secrets file, diff not shown")
                continue
            parts.append(
                f"{f['filename']} ({f.get('status')}, +{f.get('additions', 0)} -{f.get('deletions', 0)}):\n"
                + str(f.get("patch") or "")[:1500]
            )
        data = {"repo": repo, "sha": full, "files": [f["filename"] for f in c.get("files") or []]}
        return ToolResult(cap(redact("\n".join(parts))[0]), data=data)

    async def deployments(
        self, ctx: ToolContext, credential: Credential, repo: str, args: dict[str, Any]
    ) -> ToolResult:
        params = {"environment": str(args["environment"])} if args.get("environment") else {}
        found = (
            await self.gh._get(ctx, credential.token, f"repos/{repo}/deployments", per_page=20, **params)
            or []
        )
        private = await self._private(ctx, credential, repo)
        lines = [f"Deployments of {repo} ({len(found)}):"]
        for d in found[:20]:
            sha = str(d["sha"])
            short = self._cite(
                ctx,
                repo,
                private,
                f"{repo}:deploy:{d['id']}",
                f"deploy {sha[:7]}",
                f"https://github.com/{repo}/commit/{sha}",
            )
            lines.append(
                f"[{short}] {d['environment']}: {sha[:7]} at {str(d['created_at'])[:16].replace('T', ' ')} "
                f"by {(d.get('creator') or {}).get('login')}"
            )
        data = {
            "repo": repo,
            "deployments": [
                {"sha": d["sha"], "environment": d["environment"], "created_at": d["created_at"]}
                for d in found[:20]
            ],
        }
        return ToolResult(cap("\n".join(lines)), data=data)
