"""GitHub stand-in: a GitHub App and the REST subset the platform reads.

- App installation: ``/github/apps/{slug}/installations/new`` (pick the account and the GitHub user
  installing it) redirects to the app's setup URL with ``installation_id``, ``setup_action`` and a
  user authorization ``code`` (GitHub's "request user authorization during installation").
- Installation tokens: ``POST /github/api/app/installations/{id}/access_tokens`` with an RS256 app
  JWT verified against the configured public key; one hour tokens.
- User OAuth (user-to-server): ``/github/login/oauth/authorize`` and ``/access_token`` (expiring
  user tokens with rotating refresh tokens), ``DELETE /applications/{client_id}/token``.
- REST: ``/installation/repositories``, ``/user``, ``/user/installations``,
  ``/repos/{o}/{r}/contents/{path}``, ``/repos/{o}/{r}/collaborators/{u}/permission``.
"""

import base64
import io
import secrets
import tarfile
import time
from datetime import UTC, datetime
from typing import Any

import jwt
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from fakes import oauthkit
from fakes.config import Config
from fakes.github_fixture import repositories
from fakes.store import Store, outdated

PERMISSION_RANK = {"none": 0, "read": 1, "triage": 2, "write": 3, "maintain": 4, "admin": 5}


def seed() -> dict[str, Any]:
    users = {
        "alice-gh": {"login": "alice-gh", "id": 1001, "email": "alice@acme.test", "orgs": ["acme"]},
        "bob-gh": {"login": "bob-gh", "id": 1002, "email": "bob@acme.test", "orgs": ["acme"]},
        "quinn-gh": {"login": "quinn-gh", "id": 1003, "email": "quinn@acme.test", "orgs": ["acme"]},
        "dave-gh": {"login": "dave-gh", "id": 1004, "email": "dave@globex.test", "orgs": ["globex"]},
    }
    repos = repositories()
    return {
        "version": 3,  # phase 11: histories, deployments, archives (and a committed secret file)
        "users": users,
        "repos": repos,
        "installations": {},
        "codes": {},
        "access": {},
        "refresh": {},
        "install_tokens": {},
    }


class GitHub:
    def __init__(self, config: Config, store: Store) -> None:
        self.config = config
        self.store = store

    @property
    def s(self) -> dict[str, Any]:
        state: dict[str, Any] = self.store.data["github"]
        return state

    def outdated(self) -> bool:
        return outdated(self.store.data.get("github") or {}, seed())

    def reset(self) -> None:
        self.store.data["github"] = seed()

    def permission(self, login: str, repo: str) -> str:
        r = self.s["repos"].get(repo)
        if r is None:
            return "none"
        if login in r["access"]:
            return str(r["access"][login])
        owner = repo.split("/")[0]
        if not r["private"] and owner in self.s["users"].get(login, {}).get("orgs", []):
            return "read"
        return "none"

    def commit_at(self, name: str, ref: str | None) -> dict[str, Any] | None:
        """A commit by sha (or a prefix of at least 7), or the head for none, "main" and "HEAD"."""
        history = self.s["repos"][name].get("history") or []
        if not history:
            return None
        if not ref or ref in ("main", "HEAD", "heads/main"):
            return history[-1]  # type: ignore[no-any-return]
        if len(ref) < 7:
            return None
        return next((c for c in history if c["sha"].startswith(ref)), None)

    def repo_json(self, name: str) -> dict[str, Any]:
        r = self.s["repos"][name]
        owner, short = name.split("/")
        return {
            "id": abs(sum(map(ord, name))), "name": short, "full_name": name, "private": r["private"],
            "owner": {"login": owner}, "html_url": f"https://github.com/{name}", "default_branch": "main",
            "description": r["files"].get("README.md", "").splitlines()[-1] if r["files"].get("README.md") else "",
        }  # fmt: skip


def gh_error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"message": message, "documentation_url": "https://docs.github.com/rest"}, status_code=status)


def build_router(gh: GitHub) -> APIRouter:
    router = APIRouter()
    cfg = gh.config

    def accounts_page(title: str, action: str, hidden: dict[str, str], with_org: bool) -> str:
        options = []
        for user in gh.s["users"].values():
            if with_org:
                options += [(f"{user['login']}|{org}", f"{user['login']} installing on {org}") for org in user["orgs"]]
            else:
                options.append((user["login"], f"{user['login']} ({user['email']})"))
        return oauthkit.consent_page(title, action, hidden, options, "contents: read, metadata: read")

    @router.get("/github/apps/{slug}/installations/new")
    async def install_page(request: Request, slug: str) -> HTMLResponse:
        if slug != cfg.github_app_slug:
            return HTMLResponse("Not Found", status_code=404)
        hidden = {"state": request.query_params.get("state", "")}
        return HTMLResponse(accounts_page(f"Install {slug}", f"/github/apps/{slug}/installations/new", hidden, True))

    @router.post("/github/apps/{slug}/installations/new")
    async def install(request: Request, slug: str) -> RedirectResponse:
        f = await request.form()
        state = str(f.get("state", ""))
        if f.get("decision") != "allow":
            return RedirectResponse(
                oauthkit.redirect_to(cfg.github_setup_url, state=state, error="access_denied"), status_code=302
            )
        login, org = str(f.get("account", "")).split("|")
        with gh.store.lock:
            existing = next((i for i in gh.s["installations"].values() if i["account"] == org), None)
            if existing is None:
                existing = {"id": 70000 + len(gh.s["installations"]) + 1, "account": org}
                gh.s["installations"][str(existing["id"])] = existing
            code = oauthkit.new_code(gh.s["codes"], login=login, redirect_uri=cfg.github_setup_url)
            gh.store.save()
        return RedirectResponse(
            oauthkit.redirect_to(
                cfg.github_setup_url,
                installation_id=str(existing["id"]),
                setup_action="install",
                code=code,
                state=state,
            ),
            status_code=302,
        )

    @router.get("/github/login/oauth/authorize")
    async def authorize(request: Request) -> HTMLResponse:
        p = request.query_params
        if p.get("client_id") != cfg.github_client_id:
            return HTMLResponse("Unknown client_id", status_code=404)
        hidden = {"state": p.get("state", ""), "redirect_uri": p.get("redirect_uri", "")}
        return HTMLResponse(
            accounts_page("Authorize the agent platform", "/github/login/oauth/authorize", hidden, False)
        )

    @router.post("/github/login/oauth/authorize")
    async def user_decide(request: Request) -> RedirectResponse:
        f = await request.form()
        uri, state = str(f.get("redirect_uri", "")), str(f.get("state", ""))
        if f.get("decision") != "allow":
            return RedirectResponse(oauthkit.redirect_to(uri, error="access_denied", state=state), status_code=302)
        with gh.store.lock:
            code = oauthkit.new_code(gh.s["codes"], login=str(f.get("account")), redirect_uri=uri)
            gh.store.save()
        return RedirectResponse(oauthkit.redirect_to(uri, code=code, state=state), status_code=302)

    @router.post("/github/login/oauth/access_token")
    async def access_token(request: Request) -> JSONResponse:
        is_json = request.headers.get("content-type", "").startswith("application/json")
        body: dict[str, Any] = await request.json() if is_json else dict(await request.form())
        if body.get("client_id") != cfg.github_client_id or body.get("client_secret") != cfg.github_client_secret:
            return JSONResponse({"error": "incorrect_client_credentials"})  # GitHub answers 200 with an error
        with gh.store.lock:
            if body.get("grant_type") == "refresh_token":
                old = gh.s["refresh"].pop(str(body.get("refresh_token")), None)
                if old is None:
                    return JSONResponse(
                        {"error": "bad_refresh_token", "error_description": "The refresh token is invalid."}
                    )
                login = old["login"]
            else:
                data = oauthkit.take_code(gh.s["codes"], str(body.get("code")))
                if data is None or (body.get("redirect_uri") and data["redirect_uri"] != body.get("redirect_uri")):
                    return JSONResponse(
                        {
                            "error": "bad_verification_code",
                            "error_description": "The code passed is incorrect or expired.",
                        }
                    )
                login = data["login"]
            tokens = oauthkit.issue_tokens(gh.s, "gh", cfg.github_user_token_ttl, login=login)
            gh.store.save()
        return JSONResponse({**tokens, "refresh_token_expires_in": 15897600, "token_type": "bearer", "scope": ""})

    def user_of(request: Request) -> dict[str, Any] | None:
        return oauthkit.bearer(gh.s, request.headers.get("authorization"))

    def installation_of(request: Request) -> dict[str, Any] | None:
        auth = request.headers.get("authorization", "")
        if not auth.lower().startswith(("bearer ", "token ")):
            return None
        record = gh.s["install_tokens"].get(auth.split(" ", 1)[1].strip())
        if record is None or record["exp"] < time.time():
            return None
        return gh.s["installations"].get(str(record["installation"]))  # type: ignore[no-any-return]

    @router.delete("/github/api/applications/{client_id}/token")
    async def revoke(request: Request, client_id: str) -> Response:
        auth = request.headers.get("authorization", "")
        expected = base64.b64encode(f"{cfg.github_client_id}:{cfg.github_client_secret}".encode()).decode()
        if client_id != cfg.github_client_id or auth != f"Basic {expected}":
            return gh_error(404, "Not Found")
        token = (await request.json()).get("access_token")
        with gh.store.lock:
            gone = gh.s["access"].pop(str(token), None)
            if gone:
                for key in [k for k, v in gh.s["refresh"].items() if v.get("login") == gone["login"]]:
                    gh.s["refresh"].pop(key)
            gh.store.save()
        return Response(status_code=204) if gone else gh_error(404, "Not Found")

    @router.post("/github/api/app/installations/{installation_id}/access_tokens")
    async def installation_token(request: Request, installation_id: str) -> JSONResponse:
        auth = request.headers.get("authorization", "")
        if not auth.startswith("Bearer ") or not cfg.github_app_public_key:
            return gh_error(401, "A JSON web token could not be decoded")
        try:
            claims = jwt.decode(
                auth[7:], cfg.github_app_public_key, algorithms=["RS256"], options={"require": ["iat", "exp", "iss"]}
            )
        except jwt.PyJWTError as exc:
            return gh_error(401, f"A JSON web token could not be decoded ({type(exc).__name__})")
        if str(claims["iss"]) != cfg.github_app_id or claims["exp"] - claims["iat"] > 600 + 60:
            return gh_error(401, "'Expiration time' claim ('exp') is too far in the future")
        if installation_id not in gh.s["installations"]:
            return gh_error(404, "Not Found")
        token = f"ghs_{secrets.token_urlsafe(24)}"
        expires = time.time() + 3600
        with gh.store.lock:
            gh.s["install_tokens"][token] = {"installation": installation_id, "exp": expires}
            gh.store.save()
        return JSONResponse(
            {"token": token, "expires_at": datetime.fromtimestamp(expires, UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
             "permissions": {"contents": "read", "metadata": "read"}, "repository_selection": "all"},
            status_code=201,
        )  # fmt: skip

    @router.get("/github/api/installation/repositories")
    async def installation_repos(request: Request) -> JSONResponse:
        inst = installation_of(request)
        if inst is None:
            return gh_error(401, "Bad credentials")
        names = sorted(n for n in gh.s["repos"] if n.split("/")[0] == inst["account"])
        return JSONResponse({"total_count": len(names), "repositories": [gh.repo_json(n) for n in names]})

    @router.get("/github/api/user")
    async def user(request: Request) -> JSONResponse:
        record = user_of(request)
        if record is None:
            return gh_error(401, "Bad credentials")
        u = gh.s["users"][record["login"]]
        return JSONResponse({"login": u["login"], "id": u["id"], "email": u["email"], "type": "User"})

    @router.get("/github/api/user/installations")
    async def user_installations(request: Request) -> JSONResponse:
        record = user_of(request)
        if record is None:
            return gh_error(401, "Bad credentials")
        orgs = gh.s["users"][record["login"]]["orgs"]
        mine = [
            {"id": i["id"], "account": {"login": i["account"]}}
            for i in gh.s["installations"].values()
            if i["account"] in orgs
        ]
        return JSONResponse({"total_count": len(mine), "installations": mine})

    def caller(request: Request) -> tuple[str | None, dict[str, Any] | None]:
        """(user login, installation) for whichever kind of token was sent."""
        record = user_of(request)
        return (record["login"], None) if record else (None, installation_of(request))

    @router.get("/github/api/repos/{owner}/{repo}")
    async def repository(request: Request, owner: str, repo: str) -> JSONResponse:
        login, inst = caller(request)
        name = f"{owner}/{repo}"
        if login is None and inst is None:
            return gh_error(401, "Bad credentials")
        visible = name in gh.s["repos"] and (
            (inst is not None and inst["account"] == owner)
            or (login is not None and gh.permission(login, name) != "none")
        )
        return JSONResponse(gh.repo_json(name)) if visible else gh_error(404, "Not Found")

    @router.get("/github/api/repos/{owner}/{repo}/contents/{path:path}")
    async def contents(request: Request, owner: str, repo: str, path: str) -> JSONResponse:
        login, inst = caller(request)
        name = f"{owner}/{repo}"
        r = gh.s["repos"].get(name)
        allowed = r is not None and (
            (inst is not None and inst["account"] == owner)
            or (login is not None and gh.permission(login, name) != "none")
        )
        if login is None and inst is None:
            return gh_error(401, "Bad credentials")
        if not allowed or r is None:
            return gh_error(404, "Not Found")
        ref = request.query_params.get("ref")
        at = gh.commit_at(name, ref) if ref else None
        if ref and at is None:
            return gh_error(404, "No commit found for the ref " + ref)
        files = at["tree"] if at else r["files"]
        if path not in files:
            return gh_error(404, "Not Found")
        content = files[path].encode()
        return JSONResponse(
            {"type": "file", "encoding": "base64", "size": len(content), "name": path.rsplit("/", 1)[-1], "path": path,
             "content": base64.encodebytes(content).decode(), "sha": secrets.token_hex(20),
             "html_url": f"https://github.com/{name}/blob/main/{path}"}
        )  # fmt: skip

    @router.get("/github/api/repos/{owner}/{repo}/collaborators/{username}/permission")
    async def permission(request: Request, owner: str, repo: str, username: str) -> JSONResponse:
        _, inst = caller(request)
        name = f"{owner}/{repo}"
        if inst is None or inst["account"] != owner or name not in gh.s["repos"]:
            return gh_error(404, "Not Found")
        if username not in gh.s["users"]:
            return gh_error(404, "Not Found")
        level = gh.permission(username, name)
        return JSONResponse(
            {"permission": "read" if level == "triage" else level, "role_name": level, "user": {"login": username}}
        )

    def readable(request: Request, owner: str, repo: str) -> tuple[str, JSONResponse | None]:
        """The repository's name, and the error to answer when the caller may not read it."""
        login, inst = caller(request)
        name = f"{owner}/{repo}"
        if login is None and inst is None:
            return name, gh_error(401, "Bad credentials")
        ok = name in gh.s["repos"] and (
            (inst is not None and inst["account"] == owner)
            or (login is not None and gh.permission(login, name) != "none")
        )
        return name, None if ok else gh_error(404, "Not Found")

    def commit_json(name: str, c: dict[str, Any], files: bool = False) -> dict[str, Any]:
        body: dict[str, Any] = {
            "sha": c["sha"], "html_url": f"https://github.com/{name}/commit/{c['sha']}",
            "commit": {"message": c["message"], "author": {
                "name": c["author"], "email": f"{c['author']}@users.noreply.github.com", "date": c["date"]}},
            "author": {"login": c["author"]}, "parents": [{"sha": p} for p in c["parents"]],
        }  # fmt: skip
        if files:
            body["files"] = c["files"]
            body["stats"] = {k: sum(f[k] for f in c["files"]) for k in ("additions", "deletions")}
        return body

    @router.get("/github/api/repos/{owner}/{repo}/commits")
    async def commits(request: Request, owner: str, repo: str) -> JSONResponse:
        name, denied = readable(request, owner, repo)
        if denied:
            return denied
        q = request.query_params
        start = gh.commit_at(name, q.get("sha"))
        if start is None:
            return JSONResponse([])
        history = gh.s["repos"][name]["history"]
        found = [c for c in history[: history.index(start) + 1] if
                 (not q.get("path") or any(f["filename"].startswith(q["path"]) for f in c["files"]))
                 and (not q.get("since") or c["date"] >= q["since"][:19] + "Z")
                 and (not q.get("until") or c["date"] <= q["until"][:19] + "Z")]  # fmt: skip
        per_page = min(int(q.get("per_page") or 30), 100)
        return JSONResponse([commit_json(name, c) for c in reversed(found)][:per_page])

    @router.get("/github/api/repos/{owner}/{repo}/commits/{ref}")
    async def commit(request: Request, owner: str, repo: str, ref: str) -> JSONResponse:
        name, denied = readable(request, owner, repo)
        if denied:
            return denied
        c = gh.commit_at(name, ref)
        if c is None:
            return gh_error(422, f"No commit found for SHA: {ref}")
        return JSONResponse(commit_json(name, c, files=True))

    @router.get("/github/api/repos/{owner}/{repo}/tarball")
    @router.get("/github/api/repos/{owner}/{repo}/tarball/{ref}")
    async def tarball(request: Request, owner: str, repo: str, ref: str = "") -> Response:
        """The repository at a commit as a gzipped tar with one top directory, as GitHub serves it."""
        name, denied = readable(request, owner, repo)
        if denied:
            return denied
        c = gh.commit_at(name, ref or None)
        if c is None:
            return gh_error(404, "Not Found")
        top = f"{owner}-{repo}-{c['sha'][:7]}"
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
            for path, content in sorted(c["tree"].items()):
                data = content.encode()
                info = tarfile.TarInfo(f"{top}/{path}")
                info.size, info.mtime = len(data), 0
                tar.addfile(info, io.BytesIO(data))
        return Response(buffer.getvalue(), media_type="application/x-gzip",
                        headers={"content-disposition": f"attachment; filename={top}.tar.gz"})  # fmt: skip

    @router.get("/github/api/repos/{owner}/{repo}/deployments")
    async def deployments(request: Request, owner: str, repo: str) -> JSONResponse:
        name, denied = readable(request, owner, repo)
        if denied:
            return denied
        q = request.query_params
        found = [
            {"id": d["id"], "sha": d["sha"], "ref": "main", "environment": d["environment"],
             "created_at": d["created_at"], "creator": {"login": d["creator"]}, "description": None}
            for d in gh.s["repos"][name].get("deployments") or []
            if (not q.get("environment") or d["environment"] == q["environment"]) and (not q.get("sha") or d["sha"] == q["sha"])
        ]  # fmt: skip
        return JSONResponse(sorted(found, key=lambda d: d["created_at"], reverse=True))

    @router.post("/_admin/github/expire-tokens")
    def expire() -> dict[str, int]:
        with gh.store.lock:
            n = oauthkit.expire_all(gh.s)
            for record in gh.s["install_tokens"].values():
                record["exp"] = 0
            gh.store.save()
        return {"expired": n}

    return router
