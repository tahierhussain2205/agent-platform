"""GitHub tool adapter (read): repositories, files and CODEOWNERS through the GitHub App.

The org connection is the app installation (one hour tokens from the vault). With credential mode
``org_with_check`` (the default for files) the requester's own GitHub login, from their linked
account, must have read access to the repository; without a link the agent cannot check, so it asks
them to link. Public repositories are visible to anyone; private ones are restricted sources.
"""

import base64
import re
from typing import Any

import httpx

from app.db.tenant import tenant_tx
from app.policy import Member
from app.policy.redaction import redact
from app.sandbox import SandboxError, SandboxProvider, provider_from_settings
from app.tools.base import Operation, Source, Target, ToolContext, ToolResult, cap
from app.tools.github_code import CODE_OPERATIONS, CodeOperations
from app.tools.http import request
from app.vault_service import Credential

REPO_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")
CODEOWNERS_PATHS = (".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS")
REPO_ARG = {"type": "string", "description": "owner/name, for example acme/cart-service."}

OPERATIONS = {
    "list_repos": Operation(
        "list_repos",
        "List the repositories the GitHub App can see for this organization (names and visibility).",
        {"type": "object", "properties": {}},
        credential_mode="org",
    ),
    "read_file": Operation(
        "read_file",
        "Read a file from a repository the requester can read. The file starts with a ref like [S4].",
        {
            "type": "object",
            "properties": {"repo": REPO_ARG, "path": {"type": "string"}, "ref": {"type": "string"}},
            "required": ["repo", "path"],
        },
        credential_mode="org_with_check",
    ),
    "get_codeowners": Operation(
        "get_codeowners",
        "Read a repository's CODEOWNERS file (who owns which paths).",
        {"type": "object", "properties": {"repo": REPO_ARG}, "required": ["repo"]},
        credential_mode="org_with_check",
    ),
}


OPERATIONS.update(CODE_OPERATIONS)


class GitHubAdapter:
    key = "github"

    def __init__(self, api_url: str) -> None:
        self.api_url = api_url.rstrip("/")
        self.operations = OPERATIONS
        self.code = CodeOperations(self)

    def sandbox_provider(self, ctx: ToolContext) -> SandboxProvider:
        provider = provider_from_settings(ctx.settings, ctx.http)
        if provider is None:
            raise SandboxError("no sandbox provider is configured")
        return provider

    async def resolve_target(self, ctx: ToolContext, operation: str, args: dict[str, Any]) -> Target | None:
        return None

    async def _get(self, ctx: ToolContext, token: str, endpoint: str, /, **params: Any) -> Any:
        response = await request(
            ctx.http,
            "GET",
            f"{self.api_url}/{endpoint}",
            params=params or None,
            headers={"authorization": f"Bearer {token}", "accept": "application/vnd.github+json"},
        )
        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            raise GitHubError(str(response.json().get("message", response.status_code)))
        return response.json()

    async def _login(self, ctx: ToolContext) -> str | None:
        """The requester's GitHub login, from their linked account."""
        if "github:me" not in ctx.cache:
            async with tenant_tx(ctx.pool, ctx.org_id) as conn:
                row = await (
                    await conn.execute(
                        "select external_account_id from app.connections where integration_key = 'github' "
                        "and scope = 'user' and membership_id = %s and status = 'active'",
                        (ctx.membership_id,),
                    )
                ).fetchone()
            ctx.cache["github:me"] = row[0] if row and row[0] else None
        return ctx.cache["github:me"]  # type: ignore[no-any-return]

    async def _can_read(self, ctx: ToolContext, credential: Credential, login: str, repo: str) -> bool:
        key = f"github:read:{login}:{repo}"
        if key not in ctx.cache:
            body = await self._get(ctx, credential.token, f"repos/{repo}/collaborators/{login}/permission")
            ctx.cache[key] = bool(body) and body.get("permission") not in (None, "none")
        return bool(ctx.cache[key])

    async def _repo(self, ctx: ToolContext, credential: Credential, repo: str) -> dict[str, Any] | None:
        key = f"github:repo:{repo}"
        if key not in ctx.cache:
            ctx.cache[key] = await self._get(ctx, credential.token, f"repos/{repo}")
        return ctx.cache[key]  # type: ignore[no-any-return]

    async def audience_ok(
        self, ctx: ToolContext, credential: Credential, source: Source, members: list[Member]
    ) -> bool:
        repo = source.audience.split(":", 1)[1] if ":" in source.audience else ""
        info = await self._repo(ctx, credential, repo) if REPO_RE.match(repo) else None
        if info is None:
            return False
        if not info.get("private"):
            return True
        for member in members:
            login = member.links.get("github")
            if not login or not await self._can_read(ctx, credential, login, repo):
                return False
        return True

    async def execute(
        self, ctx: ToolContext, operation: str, args: dict[str, Any], credential: Any = None
    ) -> ToolResult:
        if not isinstance(credential, Credential):
            return ToolResult("GitHub is not connected.", is_error=True)
        try:
            if operation == "list_repos":
                return await self._list(ctx, credential)
            repo = str(args.get("repo") or "").strip()
            if not REPO_RE.match(repo):
                return ToolResult(f"{repo or 'That'} is not a repository like owner/name.", is_error=True)
            denial = await self._check(ctx, credential, repo)
            if denial:
                return ToolResult(denial, is_error=True)
            if operation == "read_file":
                return await self._file(
                    ctx, credential, repo, str(args.get("path") or "").lstrip("/"), args.get("ref")
                )
            if operation == "search_code":
                return await self.code.search(ctx, credential, repo, args)
            if operation == "read_code":
                return await self.code.read(ctx, credential, repo, args)
            if operation == "list_files":
                return await self.code.files(ctx, credential, repo, args)
            if operation == "list_commits":
                return await self.code.commits(ctx, credential, repo, args)
            if operation == "get_commit":
                return await self.code.commit(ctx, credential, repo, str(args.get("sha") or "").strip())
            if operation == "list_deployments":
                return await self.code.deployments(ctx, credential, repo, args)
            if operation == "get_codeowners":
                for path in CODEOWNERS_PATHS:
                    result = await self._file(ctx, credential, repo, path, None)
                    if not result.is_error:
                        result.data["rules"] = parse_codeowners(str(result.data.pop("content", "")))
                        return result
                return ToolResult(f"{repo} has no CODEOWNERS file.", is_error=True)
        except GitHubError as exc:
            return ToolResult(f"GitHub refused the request: {exc}", is_error=True)
        except SandboxError as exc:
            return ToolResult(f"Code is not available: {exc}.", is_error=True)
        except httpx.HTTPError as exc:
            return ToolResult(f"GitHub error: {type(exc).__name__}", is_error=True)
        return ToolResult(f"Unknown operation {operation}.", is_error=True)

    async def _check(self, ctx: ToolContext, credential: Credential, repo: str) -> str | None:
        if credential.scope == "user":
            return None  # the requester's own token: GitHub applies their access
        login = await self._login(ctx)
        if login is None:
            return f"Link your GitHub account in the console so I can check you may read {repo}."
        if not await self._can_read(ctx, credential, login, repo):
            return f"Access denied: your GitHub account ({login}) can't read {repo}."
        return None

    async def _list(self, ctx: ToolContext, credential: Credential) -> ToolResult:
        body = await self._get(ctx, credential.token, "installation/repositories", per_page=100) or {}
        repos = body.get("repositories") or []
        lines = [f"{r['full_name']} ({'private' if r.get('private') else 'public'})" for r in repos]
        data = {
            "repos": [
                {
                    "id": str(r.get("id") or r["full_name"]),
                    "full_name": r["full_name"],
                    "private": bool(r.get("private")),
                    "default_branch": r.get("default_branch") or "main",
                }
                for r in repos
            ]
        }
        return ToolResult("Repositories:\n" + ("\n".join(lines) or "none"), data=data)

    async def _file(
        self, ctx: ToolContext, credential: Credential, repo: str, path: str, ref: Any
    ) -> ToolResult:
        if not path or ".." in path.split("/"):
            return ToolResult("Give a file path inside the repository.", is_error=True)
        params = {"ref": str(ref)} if ref else {}
        body = await self._get(ctx, credential.token, f"repos/{repo}/contents/{path}", **params)
        if body is None or body.get("type") != "file":
            return ToolResult(f"{repo} has no file {path}.", is_error=True)
        text = base64.b64decode(body.get("content", "")).decode("utf-8", errors="replace")
        info = await self._repo(ctx, credential, repo) or {}
        source = Source(
            ref=f"github:{repo}:{path}",
            channel_external_id="",
            channel_name=repo,
            visibility="restricted" if info.get("private", True) else "public",
            ts="",
            label=f"{repo}/{path}",
            permalink=body.get("html_url"),
            kind="github",
            audience=f"github:{repo}",
        )
        short = ctx.refs.add(source)
        flat = text.strip().replace("\n", " ⏎ ")
        data = {"repo": repo, "path": path, "content": text[:50_000]}  # in process only, never stored
        return ToolResult(cap(redact(f"[{short}] ({repo}) {path}: {flat}")[0]), data=data)


def parse_codeowners(text: str) -> list[dict[str, Any]]:
    """CODEOWNERS rules in file order: ``{"pattern", "owners"}`` (owners as written: @login, @org/team or an
    email). Comments and blank lines are skipped; a rule without owners clears ownership."""
    rules = []
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        pattern, *owners = line.split()
        rules.append({"pattern": pattern, "owners": owners})
    return rules


class GitHubError(Exception):
    pass
