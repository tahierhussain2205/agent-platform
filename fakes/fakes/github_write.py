"""GitHub stand-in, the write side: Git Data API, branches, pulls, comments, checks, CI and webhooks.

- Git Data API: ``POST /git/blobs``, ``POST /git/trees`` (``base_tree``; an entry with ``sha: null``
  deletes a file), ``POST /git/commits``, ``GET /git/commits/{sha}``, ``GET /git/ref/heads/{branch}``,
  ``POST /git/refs``, ``PATCH /git/refs/heads/{branch}`` (fast forward unless ``force``).
  As GitHub does: the default branch is protected, malformed paths are refused, and a GitHub App
  without the ``workflows`` permission cannot create or update ``.github/workflows/`` files.
- Pulls: create (one open pull per head), list (``head``, ``base``, ``state``), get, update, files;
  review comments and reviews; issue comments (list, create).
- Checks: ``GET /commits/{ref}/check-runs``. CI is simulated per repository: a push to a branch
  starts its checks, which complete after a delay with the next conclusion queued by
  ``POST /_admin/github/ci`` (success by default) and deliver a ``check_suite`` webhook.
- Webhooks: signed (``X-Hub-Signature-256``) and delivered once each to ``GITHUB_WEBHOOK_URL``, with
  ``X-GitHub-Event`` and ``X-GitHub-Delivery``; GitHub does not retry, but a delivery can be
  redelivered (same delivery id) from ``/_admin/github/deliveries/{id}/redeliver``.
- ``POST /_admin/github/review`` submits a reviewer's review with inline comments.
- ``/github/{owner}/{repo}/pull/{n}``: a plain page of the pull (title, body, checks, diff, comments).

Blob contents are kept as text (binary blobs are decoded with replacement characters).
"""

import asyncio
import base64
import hashlib
import hmac
import html
import json
import re
import secrets
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from fakes.github import GitHub, gh_error, tree_sha
from fakes.github_fixture import TIME, _patch

WORKFLOWS = ".github/workflows/"
BLOB_MODES = {"100644", "100755", "120000"}
BAD_COMPONENT = re.compile(r"^(|\.|\.\.|\.git)$", re.IGNORECASE)


def now() -> str:
    return datetime.now(UTC).strftime(TIME)


def sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def validation(message: str) -> JSONResponse:
    return JSONResponse(
        {"message": "Validation Failed", "errors": [{"resource": "PullRequest", "code": "custom", "message": message}],
         "documentation_url": "https://docs.github.com/rest"}, status_code=422,
    )  # fmt: skip


class Webhooks:
    """Delivers webhook events once each, after an optional delay; the log keeps every attempt."""

    def __init__(self, gh: GitHub) -> None:
        self.gh = gh
        self.pending: set[asyncio.Task[None]] = set()
        self._http: httpx.AsyncClient | None = None

    async def stop(self) -> None:
        for task in self.pending:
            task.cancel()
        if self._http is not None:
            await self._http.aclose()

    def later(self, delay: float, make: Callable[[], list[tuple[str, dict[str, Any]]]]) -> None:
        """Build the events when they are due (CI's payload names the pulls open at that time)."""

        async def run() -> None:
            await asyncio.sleep(delay)
            for event, payload in make():
                await self.deliver(event, payload)

        task = asyncio.create_task(run())
        self.pending.add(task)
        task.add_done_callback(self.pending.discard)

    def record(self, event: str, payload: dict[str, Any]) -> dict[str, Any]:
        delivery: dict[str, Any] = {"id": str(uuid.uuid4()), "event": event, "action": payload.get("action"),
                    "body": json.dumps(payload), "attempts": [], "delivered": False}  # fmt: skip
        with self.gh.store.lock:
            self.gh.s["deliveries"].append(delivery)
            del self.gh.s["deliveries"][:-200]
            self.gh.store.save()
        return delivery

    async def deliver(self, event: str, payload: dict[str, Any]) -> None:
        await self.send(self.record(event, payload))

    async def send(self, delivery: dict[str, Any]) -> None:
        cfg = self.gh.config
        if not cfg.github_webhook_url:
            delivery["attempts"].append({"error": "no webhook url"})
            return
        body = delivery["body"].encode()
        headers = {"content-type": "application/json", "x-github-event": delivery["event"],
                   "x-github-delivery": delivery["id"], "x-hub-signature-256": sign(cfg.github_webhook_secret, body),
                   "user-agent": "GitHub-Hookshot/fake"}  # fmt: skip
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=10.0)  # GitHub gives up after 10 seconds
        try:
            response = await self._http.post(cfg.github_webhook_url, content=body, headers=headers)
            delivery["attempts"].append({"status": response.status_code})
            delivery["delivered"] = delivery["delivered"] or response.is_success
        except httpx.HTTPError as exc:
            delivery["attempts"].append({"error": type(exc).__name__})
        with self.gh.store.lock:
            self.gh.store.save()

    async def drain(self, limit_seconds: float = 30.0) -> None:
        async with asyncio.timeout(limit_seconds):
            while self.pending:
                await asyncio.gather(*list(self.pending), return_exceptions=True)


def build_write_router(gh: GitHub, hooks: Webhooks) -> APIRouter:
    router = APIRouter()
    cfg = gh.config
    api = "/github/api/repos/{owner}/{repo}"

    def repo_of(name: str) -> dict[str, Any]:
        return gh.s["repos"][name]  # type: ignore[no-any-return]

    def pull_url(name: str, number: int) -> str:
        return f"{cfg.github_public_url}/{name}/pull/{number}"

    def repository_json(name: str) -> dict[str, Any]:
        owner, short = name.split("/")
        return {"id": abs(sum(map(ord, name))), "name": short, "full_name": name, "owner": {"login": owner},
                "private": repo_of(name)["private"], "default_branch": "main"}  # fmt: skip

    def installation_json(owner: str) -> dict[str, Any] | None:
        inst = next((i for i in gh.s["installations"].values() if i["account"] == owner), None)
        return {"id": inst["id"]} if inst else None

    def event(name: str, sender: str, **body: Any) -> dict[str, Any]:
        return {**body, "repository": repository_json(name), "installation": installation_json(name.split("/")[0]),
                "sender": {"login": sender}}  # fmt: skip

    def find_tree(name: str, sha: str) -> tuple[dict[str, str], dict[str, str]] | None:
        r = repo_of(name)
        if sha in r["trees"]:
            return dict(r["trees"][sha]["tree"]), dict(r["trees"][sha]["modes"])
        for c in gh.all_commits(name):
            if (c.get("tree_sha") or tree_sha(c["tree"], c.get("modes"))) == sha:
                return dict(c["tree"]), dict(c.get("modes") or {})
        return None

    def bad_path(path: str) -> bool:
        return path.startswith("/") or any(BAD_COMPONENT.match(part) for part in path.split("/"))

    def changed(old: dict[str, str], new: dict[str, str]) -> list[str]:
        return sorted(p for p in set(old) | set(new) if old.get(p) != new.get(p))

    def file_changes(old: dict[str, str], new: dict[str, str]) -> list[dict[str, Any]]:
        return [_patch(p, old.get(p), new.get(p)) for p in changed(old, new)]

    def pull_json(name: str, pr: dict[str, Any]) -> dict[str, Any]:
        owner = name.split("/")[0]
        head = gh.commit_at(name, pr["head"])
        base = gh.commit_at(name, pr["base"])
        return {
            "number": pr["number"], "state": pr["state"], "title": pr["title"], "body": pr["body"],
            "draft": pr["draft"], "merged": False, "html_url": pull_url(name, pr["number"]),
            "user": {"login": pr["user"]}, "created_at": pr["created_at"], "updated_at": pr["updated_at"],
            "head": {"ref": pr["head"], "sha": head["sha"] if head else pr["head_sha"], "label": f"{owner}:{pr['head']}",
                     "repo": {"full_name": name}},
            "base": {"ref": pr["base"], "sha": base["sha"] if base else None, "repo": {"full_name": name}},
        }  # fmt: skip

    def pull_of(name: str, number: int) -> dict[str, Any] | None:
        return next((p for p in repo_of(name)["pulls"] if p["number"] == number), None)

    def open_pulls_for(name: str, branch: str) -> list[dict[str, Any]]:
        return [p for p in repo_of(name)["pulls"] if p["head"] == branch and p["state"] == "open"]

    # ------------------------------------------------------------------ Git Data API

    @router.post(api + "/git/blobs")
    async def create_blob(request: Request, owner: str, repo: str) -> JSONResponse:
        name, _, denied = gh.writable(request, owner, repo)
        if denied:
            return denied
        body = await request.json()
        try:
            raw = (base64.b64decode(str(body.get("content", ""))) if body.get("encoding") == "base64"
                   else str(body.get("content", "")).encode())  # fmt: skip
        except ValueError:
            return gh_error(422, "content is not valid Base64")
        sha = hashlib.sha1(b"blob %d\0" % len(raw) + raw).hexdigest()
        with gh.store.lock:
            repo_of(name)["blobs"][sha] = raw.decode("utf-8", errors="replace")
            gh.store.save()
        return JSONResponse({"sha": sha, "url": f"{cfg.github_public_url}/api/repos/{name}/git/blobs/{sha}"},
                            status_code=201)  # fmt: skip

    @router.post(api + "/git/trees")
    async def create_tree(request: Request, owner: str, repo: str) -> JSONResponse:
        name, _, denied = gh.writable(request, owner, repo)
        if denied:
            return denied
        body = await request.json()
        tree: dict[str, str] = {}
        modes: dict[str, str] = {}
        if body.get("base_tree"):
            found = find_tree(name, str(body["base_tree"]))
            if found is None:
                return gh_error(422, "base_tree is not a valid tree")
            tree, modes = found
        r = repo_of(name)
        for entry in body.get("tree") or []:
            path, mode = str(entry.get("path", "")), str(entry.get("mode", "100644"))
            if bad_path(path):
                return gh_error(422, f"tree.path contains a malformed path component: {path!r}")
            if mode not in BLOB_MODES or entry.get("type", "blob") != "blob":
                return gh_error(422, f"tree.mode {mode} is not supported here")
            if "sha" in entry and entry["sha"] is None:
                tree.pop(path, None)
                modes.pop(path, None)
                continue
            if "content" in entry:
                content = str(entry["content"])
            elif entry.get("sha") in r["blobs"]:
                content = r["blobs"][entry["sha"]]
            else:
                return gh_error(422, f"tree.sha {entry.get('sha')} is not a valid blob")
            tree[path] = content
            if mode == "100644":
                modes.pop(path, None)
            else:
                modes[path] = mode
        sha = tree_sha(tree, modes)
        with gh.store.lock:
            r["trees"][sha] = {"tree": tree, "modes": modes}
            gh.store.save()
        entries = [{"path": p, "mode": modes.get(p, "100644"), "type": "blob"} for p in sorted(tree)]
        return JSONResponse({"sha": sha, "tree": entries, "truncated": False}, status_code=201)

    def git_commit_json(name: str, c: dict[str, Any]) -> dict[str, Any]:
        return {"sha": c["sha"], "message": c["message"], "html_url": f"https://github.com/{name}/commit/{c['sha']}",
                "tree": {"sha": c.get("tree_sha") or tree_sha(c["tree"], c.get("modes"))},
                "parents": [{"sha": p} for p in c["parents"]],
                "author": {"name": c["author"], "email": f"{c['author']}@users.noreply.github.com",
                           "date": c["date"]}}  # fmt: skip

    @router.post(api + "/git/commits")
    async def create_commit(request: Request, owner: str, repo: str) -> JSONResponse:
        name, writer, denied = gh.writable(request, owner, repo)
        if denied:
            return denied
        body = await request.json()
        found = find_tree(name, str(body.get("tree", "")))
        if found is None:
            return gh_error(422, "tree is not a valid tree")
        parents = [str(p) for p in body.get("parents") or []]
        parent_commits = [gh.commit_by_sha(name, p) for p in parents]
        if len(parents) != 1 or None in parent_commits:
            return gh_error(422, "parents must name one existing commit")
        parent = parent_commits[0]
        assert parent is not None
        message = str(body.get("message") or "")
        if not message.strip():
            return gh_error(422, "message is required")
        tree, modes = found
        sha = hashlib.sha1(f"{name}{message}{parents[0]}{tree_sha(tree, modes)}{time.time_ns()}".encode()).hexdigest()
        commit = {"sha": sha, "message": message, "author": writer, "parents": parents, "date": now(),
                  "files": file_changes(parent["tree"], tree), "tree": tree, "modes": modes,
                  "tree_sha": tree_sha(tree, modes)}  # fmt: skip
        with gh.store.lock:
            repo_of(name)["objects"][sha] = commit
            gh.store.save()
        return JSONResponse(git_commit_json(name, commit), status_code=201)

    @router.get(api + "/git/commits/{sha}")
    async def get_git_commit(request: Request, owner: str, repo: str, sha: str) -> JSONResponse:
        name, denied = gh.readable(request, owner, repo)
        if denied:
            return denied
        c = gh.commit_by_sha(name, sha)
        return JSONResponse(git_commit_json(name, c)) if c else gh_error(404, "Not Found")

    def ref_json(name: str, branch: str) -> dict[str, Any]:
        sha = repo_of(name)["refs"][branch]
        return {"ref": f"refs/heads/{branch}", "object": {"sha": sha, "type": "commit"},
                "url": f"{cfg.github_public_url}/api/repos/{name}/git/refs/heads/{branch}"}  # fmt: skip

    @router.get(api + "/git/ref/heads/{branch:path}")
    async def get_ref(request: Request, owner: str, repo: str, branch: str) -> JSONResponse:
        name, denied = gh.readable(request, owner, repo)
        if denied:
            return denied
        return JSONResponse(ref_json(name, branch)) if branch in repo_of(name)["refs"] else gh_error(404, "Not Found")

    def refuse_update(name: str, old_tree: dict[str, str], new: dict[str, Any], login: str | None) -> str | None:
        """GitHub's checks before moving a branch: an app may not touch workflow files."""
        workflows = [p for p in changed(old_tree, new["tree"]) if p.startswith(WORKFLOWS)]
        if workflows and login is None:
            return (f"refusing to allow a GitHub App to create or update workflow `{workflows[0]}` "
                    "without `workflows` permission")  # fmt: skip
        return None

    def pushed(name: str, branch: str, before: str | None, after: str, forced: bool, writer: str) -> None:
        r = repo_of(name)
        r["refs"][branch] = after
        r["pushes"].append({"ref": f"refs/heads/{branch}", "before": before, "after": after, "forced": forced,
                            "by": writer, "at": now()})  # fmt: skip
        start_ci(name, branch, after, writer)

    @router.post(api + "/git/refs")
    async def create_ref(request: Request, owner: str, repo: str) -> JSONResponse:
        name, writer, denied = gh.writable(request, owner, repo)
        if denied:
            return denied
        body = await request.json()
        ref, sha = str(body.get("ref", "")), str(body.get("sha", ""))
        branch = ref.removeprefix("refs/heads/")
        if not ref.startswith("refs/heads/") or bad_path(branch):
            return gh_error(422, f"{ref} is not a valid ref name.")
        commit = gh.commit_by_sha(name, sha)
        if commit is None:
            return gh_error(422, "Object does not exist")
        login, _ = gh.caller(request)
        with gh.store.lock:
            if branch in repo_of(name)["refs"]:
                return gh_error(422, "Reference already exists")
            refused = refuse_update(name, gh.merge_base(name, commit)["tree"], commit, login)
            if refused:
                return gh_error(422, refused)
            pushed(name, branch, None, sha, False, writer)
            gh.store.save()
        return JSONResponse(ref_json(name, branch), status_code=201)

    @router.patch(api + "/git/refs/heads/{branch:path}")
    async def update_ref(request: Request, owner: str, repo: str, branch: str) -> JSONResponse:
        name, writer, denied = gh.writable(request, owner, repo)
        if denied:
            return denied
        body = await request.json()
        r = repo_of(name)
        if branch not in r["refs"]:
            return gh_error(422, "Reference does not exist")
        if branch == "main":
            return gh_error(422, "Protected branch update failed for refs/heads/main.")
        commit = gh.commit_by_sha(name, str(body.get("sha", "")))
        if commit is None:
            return gh_error(422, "Object does not exist")
        force = bool(body.get("force"))
        login, _ = gh.caller(request)
        with gh.store.lock:
            before = r["refs"][branch]
            current = gh.commit_by_sha(name, before)
            assert current is not None
            if not force and before not in {c["sha"] for c in gh.ancestors(name, commit)}:
                return gh_error(422, "Update is not a fast forward")
            refused = refuse_update(name, current["tree"], commit, login)
            if refused:
                return gh_error(422, refused)
            pushed(name, branch, before, commit["sha"], force, writer)
            gh.store.save()
        return JSONResponse(ref_json(name, branch))

    @router.delete(api + "/git/refs/heads/{branch:path}")
    async def delete_ref(request: Request, owner: str, repo: str, branch: str) -> Response:
        name, _, denied = gh.writable(request, owner, repo)
        if denied:
            return denied
        if branch == "main":
            return gh_error(422, "Protected branch update failed for refs/heads/main.")
        with gh.store.lock:
            if repo_of(name)["refs"].pop(branch, None) is None:
                return gh_error(422, "Reference does not exist")
            gh.store.save()
        return Response(status_code=204)

    # ------------------------------------------------------------------ CI

    def start_ci(name: str, branch: str, sha: str, writer: str) -> None:
        """Called with the lock held: the push's checks start now and complete after a delay."""
        r = repo_of(name)
        ci = r["ci"]
        if not ci["enabled"] or branch == "main":
            return
        suite = secrets.randbelow(10**9)
        for check in ci["checks"]:
            r["checks"].append({"id": secrets.randbelow(10**9), "suite": suite, "sha": sha, "name": check,
                                "status": "in_progress", "conclusion": None, "started_at": now(),
                                "completed_at": None, "summary": None})  # fmt: skip

        def complete() -> list[tuple[str, dict[str, Any]]]:
            with gh.store.lock:
                conclusion = ci["next"].pop(0) if ci["next"] else "success"
                for run in r["checks"]:
                    if run["suite"] == suite:
                        failed = conclusion != "success"
                        run.update({"status": "completed", "conclusion": conclusion, "completed_at": now(),
                                    "summary": (ci.get("failure_summary") or "Tests failed" if failed
                                                else "All tests passed")})  # fmt: skip
                pulls = [{"number": p["number"], "head": {"ref": p["head"], "sha": sha}, "base": {"ref": p["base"]}}
                         for p in open_pulls_for(name, branch)]  # fmt: skip
                gh.store.save()
            return [("check_suite", event(name, writer, action="completed", check_suite={
                "id": suite, "head_branch": branch, "head_sha": sha, "status": "completed", "conclusion": conclusion,
                "pull_requests": pulls}))]  # fmt: skip

        hooks.later(float(ci["delay"]), complete)

    def check_run_json(name: str, run: dict[str, Any]) -> dict[str, Any]:
        return {"id": run["id"], "name": run["name"], "head_sha": run["sha"], "status": run["status"],
                "conclusion": run["conclusion"], "started_at": run["started_at"], "completed_at": run["completed_at"],
                "html_url": f"{cfg.github_public_url}/{name}/runs/{run['id']}", "check_suite": {"id": run["suite"]},
                "output": {"title": run["name"], "summary": run["summary"]}}  # fmt: skip

    @router.get(api + "/commits/{ref:path}/check-runs")
    async def check_runs(request: Request, owner: str, repo: str, ref: str) -> JSONResponse:
        name, denied = gh.readable(request, owner, repo)
        if denied:
            return denied
        c = gh.commit_at(name, ref)
        if c is None:
            return gh_error(422, f"No commit found for SHA: {ref}")
        runs = [check_run_json(name, run) for run in repo_of(name)["checks"] if run["sha"] == c["sha"]]
        return JSONResponse({"total_count": len(runs), "check_runs": runs})

    # ------------------------------------------------------------------ pulls

    @router.get(api + "/pulls")
    async def list_pulls(request: Request, owner: str, repo: str) -> JSONResponse:
        name, denied = gh.readable(request, owner, repo)
        if denied:
            return denied
        q = request.query_params
        state = q.get("state") or "open"
        head = q.get("head")
        found = [pull_json(name, p) for p in repo_of(name)["pulls"]
                 if (state == "all" or p["state"] == state) and (not q.get("base") or p["base"] == q["base"])
                 and (not head or f"{owner}:{p['head']}" == head)]  # fmt: skip
        return JSONResponse(sorted(found, key=lambda p: -int(p["number"])))

    @router.post(api + "/pulls")
    async def create_pull(request: Request, owner: str, repo: str) -> JSONResponse:
        name, writer, denied = gh.writable(request, owner, repo)
        if denied:
            return denied
        body = await request.json()
        head = str(body.get("head", "")).removeprefix(f"{owner}:")
        base = str(body.get("base") or "main")
        title = str(body.get("title") or "").strip()
        r = repo_of(name)
        if not title:
            return validation("title is required")
        if head not in r["refs"] or base not in r["refs"]:
            return validation(f"head or base is not a branch: {head}, {base}")
        if r["refs"][head] == r["refs"][base]:
            return validation(f"No commits between {base} and {head}")
        with gh.store.lock:
            if open_pulls_for(name, head):
                return validation(f"A pull request already exists for {owner}:{head}.")
            number = 1 + max([p["number"] for p in r["pulls"]], default=0)
            pr = {"number": number, "title": title[:256], "body": str(body.get("body") or "")[:65536], "head": head,
                  "base": base, "head_sha": r["refs"][head], "state": "open", "draft": bool(body.get("draft")),
                  "user": writer, "created_at": now(), "updated_at": now()}  # fmt: skip
            r["pulls"].append(pr)
            gh.store.save()
        return JSONResponse(pull_json(name, pr), status_code=201)

    @router.get(api + "/pulls/{number}")
    async def get_pull(request: Request, owner: str, repo: str, number: int) -> JSONResponse:
        name, denied = gh.readable(request, owner, repo)
        if denied:
            return denied
        pr = pull_of(name, number)
        return JSONResponse(pull_json(name, pr)) if pr else gh_error(404, "Not Found")

    @router.patch(api + "/pulls/{number}")
    async def update_pull(request: Request, owner: str, repo: str, number: int) -> JSONResponse:
        name, _, denied = gh.writable(request, owner, repo)
        if denied:
            return denied
        pr = pull_of(name, number)
        if pr is None:
            return gh_error(404, "Not Found")
        body = await request.json()
        with gh.store.lock:
            for key in ("title", "body"):
                if isinstance(body.get(key), str):
                    pr[key] = body[key]
            if body.get("state") in ("open", "closed"):
                pr["state"] = body["state"]
            pr["updated_at"] = now()
            gh.store.save()
        return JSONResponse(pull_json(name, pr))

    def pull_files(name: str, pr: dict[str, Any]) -> list[dict[str, Any]]:
        head = gh.commit_at(name, pr["head"]) or gh.commit_by_sha(name, pr["head_sha"])
        if head is None:
            return []
        return file_changes(gh.merge_base(name, head)["tree"], head["tree"])

    @router.get(api + "/pulls/{number}/files")
    async def files(request: Request, owner: str, repo: str, number: int) -> JSONResponse:
        name, denied = gh.readable(request, owner, repo)
        if denied:
            return denied
        pr = pull_of(name, number)
        return JSONResponse(pull_files(name, pr)) if pr else gh_error(404, "Not Found")

    def comment_json(name: str, c: dict[str, Any]) -> dict[str, Any]:
        body = {k: v for k, v in c.items() if k not in ("number", "user")}
        return {**body, "user": {"login": c["user"]},
                "html_url": f"{pull_url(name, c['number'])}#{c['kind']}-{c['id']}"}  # fmt: skip

    @router.get(api + "/pulls/{number}/comments")
    async def review_comments(request: Request, owner: str, repo: str, number: int) -> JSONResponse:
        name, denied = gh.readable(request, owner, repo)
        if denied:
            return denied
        found = [comment_json(name, c) for c in repo_of(name)["review_comments"] if c["number"] == number]
        return JSONResponse(found) if pull_of(name, number) else gh_error(404, "Not Found")

    @router.get(api + "/pulls/{number}/reviews")
    async def reviews(request: Request, owner: str, repo: str, number: int) -> JSONResponse:
        name, denied = gh.readable(request, owner, repo)
        if denied:
            return denied
        found = [comment_json(name, c) for c in repo_of(name)["reviews"] if c["number"] == number]
        return JSONResponse(found) if pull_of(name, number) else gh_error(404, "Not Found")

    @router.get(api + "/issues/{number}/comments")
    async def issue_comments(request: Request, owner: str, repo: str, number: int) -> JSONResponse:
        name, denied = gh.readable(request, owner, repo)
        if denied:
            return denied
        found = [comment_json(name, c) for c in repo_of(name)["issue_comments"] if c["number"] == number]
        return JSONResponse(found) if pull_of(name, number) else gh_error(404, "Not Found")

    @router.post(api + "/issues/{number}/comments")
    async def comment(request: Request, owner: str, repo: str, number: int) -> JSONResponse:
        name, writer, denied = gh.writable(request, owner, repo)
        if denied:
            return denied
        body = str((await request.json()).get("body") or "")
        if pull_of(name, number) is None:
            return gh_error(404, "Not Found")
        if not body.strip():
            return gh_error(422, "body is required")
        c = {"id": secrets.randbelow(10**9), "kind": "issuecomment", "number": number, "user": writer,
             "body": body[:65536], "created_at": now()}  # fmt: skip
        with gh.store.lock:
            repo_of(name)["issue_comments"].append(c)
            gh.store.save()
        return JSONResponse(comment_json(name, c), status_code=201)

    # ------------------------------------------------------------------ a page for people

    @router.get("/github/{owner}/{repo}/pull/{number}")
    async def pull_page(owner: str, repo: str, number: int) -> HTMLResponse:
        name = f"{owner}/{repo}"
        pr = pull_of(name, number) if name in gh.s["repos"] else None
        if pr is None:
            return HTMLResponse("Not Found", status_code=404)
        e = html.escape
        head = gh.commit_at(name, pr["head"])
        runs = [r for r in repo_of(name)["checks"] if head and r["sha"] == head["sha"]]
        parts = [f"<h1>{e(pr['title'])} <span>#{number}</span></h1>",
                 f"<p class=meta>{e(pr['state'])} · {e(pr['user'])} wants to merge <code>{e(pr['head'])}</code> "
                 f"into <code>{e(pr['base'])}</code></p>", f"<pre class=body>{e(pr['body'])}</pre>", "<h2>Checks</h2>",
                 "<ul>" + "".join(f"<li class=check>{e(r['name'])}: {e(r['conclusion'] or r['status'])}</li>"
                                  for r in runs) + "</ul>", "<h2>Files changed</h2>"]  # fmt: skip
        for f in pull_files(name, pr):
            parts.append(f"<h3 class=file>{e(f['filename'])}</h3><pre class=diff>{e(f['patch'])}</pre>")
        parts.append("<h2>Conversation</h2>")
        for c in [*repo_of(name)["reviews"], *repo_of(name)["review_comments"], *repo_of(name)["issue_comments"]]:
            if c["number"] == number and c.get("body"):
                where = f" on {e(c['path'])}:{c['line']}" if c.get("path") else ""
                parts.append(f"<div class=comment><b>{e(c['user'])}</b>{where}<p>{e(c['body'])}</p></div>")
        style = ("body{font-family:sans-serif;max-width:960px;margin:2em auto;padding:0 16px}"
                 "pre{background:#f6f8fa;padding:8px;overflow:auto}.comment{border:1px solid #ddd;padding:8px;margin:8px 0}")  # fmt: skip
        return HTMLResponse(f"<!doctype html><title>{e(pr['title'])} · {e(name)}</title><style>{style}</style>"
                            + "".join(parts))  # fmt: skip

    # ------------------------------------------------------------------ test admin

    @router.post("/_admin/github/ci")
    async def set_ci(request: Request) -> JSONResponse:
        """``{"repository", "conclusion"?, "count"?, "enabled"?, "delay"?}``: queue the next conclusions."""
        body = await request.json()
        name = str(body.get("repository", ""))
        if name not in gh.s["repos"]:
            return JSONResponse({"error": "unknown repository"}, status_code=404)
        with gh.store.lock:
            ci = repo_of(name)["ci"]
            if body.get("failure_summary"):
                ci["failure_summary"] = str(body["failure_summary"])[:500]
            if body.get("conclusion"):
                ci["next"].extend([str(body["conclusion"])] * int(body.get("count") or 1))
            for key in ("enabled", "delay"):
                if key in body:
                    ci[key] = body[key]
            gh.store.save()
            return JSONResponse(ci)

    @router.post("/_admin/github/review")
    async def review(request: Request) -> JSONResponse:
        """``{"repository", "number", "login", "state", "body", "comments": [{"path", "line", "body"}]}``."""
        body = await request.json()
        name, number = str(body.get("repository", "")), int(body.get("number") or 0)
        pr = pull_of(name, number) if name in gh.s["repos"] else None
        if pr is None:
            return JSONResponse({"error": "unknown pull request"}, status_code=404)
        login = str(body.get("login") or "bob-gh")
        state = str(body.get("state") or "commented")
        r = repo_of(name)
        head = gh.commit_at(name, pr["head"])
        sha = head["sha"] if head else pr["head_sha"]
        with gh.store.lock:
            rv = {"id": secrets.randbelow(10**9), "kind": "pullrequestreview", "number": number, "user": login,
                  "state": state.upper(), "body": str(body.get("body") or ""), "commit_id": sha,
                  "submitted_at": now()}  # fmt: skip
            r["reviews"].append(rv)
            comments = []
            for item in body.get("comments") or []:
                c = {"id": secrets.randbelow(10**9), "kind": "discussion_r", "number": number, "user": login,
                     "pull_request_review_id": rv["id"], "path": str(item["path"]), "line": int(item.get("line") or 1),
                     "body": str(item["body"]), "commit_id": sha, "created_at": now()}  # fmt: skip
                r["review_comments"].append(c)
                comments.append(c)
            gh.store.save()
        pull = pull_json(name, pr)
        await hooks.deliver("pull_request_review", event(name, login, action="submitted", pull_request=pull,
                                                         review=comment_json(name, rv)))  # fmt: skip
        for c in comments:
            await hooks.deliver("pull_request_review_comment", event(name, login, action="created", pull_request=pull,
                                                                     comment=comment_json(name, c)))  # fmt: skip
        return JSONResponse({"review": comment_json(name, rv), "comments": [comment_json(name, c) for c in comments]})

    @router.get("/_admin/github/repos/{owner}/{repo}")
    def repo_state(owner: str, repo: str) -> JSONResponse:
        name = f"{owner}/{repo}"
        if name not in gh.s["repos"]:
            return JSONResponse({"error": "unknown repository"}, status_code=404)
        r = repo_of(name)
        return JSONResponse({
            "refs": r["refs"], "pushes": r["pushes"], "pulls": [pull_json(name, p) for p in r["pulls"]],
            "checks": r["checks"], "ci": r["ci"], "issue_comments": r["issue_comments"],
            "commits": [{k: c[k] for k in ("sha", "message", "author", "parents", "files")}
                        for c in r["objects"].values()],
        })  # fmt: skip

    @router.get("/_admin/github/deliveries")
    def deliveries() -> list[dict[str, Any]]:
        return [{k: d[k] for k in ("id", "event", "action", "delivered", "attempts")} | {"payload": json.loads(d["body"])}
                for d in gh.s["deliveries"]]  # fmt: skip

    @router.post("/_admin/github/deliveries/drain")
    async def drain() -> dict[str, int]:
        await hooks.drain()
        return {"undelivered": sum(1 for d in gh.s["deliveries"] if not d["delivered"])}

    @router.post("/_admin/github/deliveries/{delivery_id}/redeliver")
    async def redeliver(delivery_id: str) -> JSONResponse:
        delivery = next((d for d in gh.s["deliveries"] if d["id"] == delivery_id), None)
        if delivery is None:
            return JSONResponse({"error": "unknown delivery"}, status_code=404)
        await hooks.send(delivery)
        return JSONResponse({k: delivery[k] for k in ("id", "event", "delivered", "attempts")})

    return router
