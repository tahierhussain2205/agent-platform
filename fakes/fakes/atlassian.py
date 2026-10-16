"""Atlassian / Jira Cloud stand-in.

- OAuth 2.0 (3LO): ``/atlassian/authorize`` (a consent page) and ``/atlassian/oauth/token``
  (authorization code and rotating refresh tokens, JSON bodies like Atlassian's).
- ``/atlassian/api/oauth/token/accessible-resources`` and Jira REST v3 under
  ``/atlassian/api/ex/jira/{cloudid}/rest/api/3``: ``myself``, ``search/jql``, ``issue/{key}``,
  ``project/search``, ``user/search``, ``user/permission/search``. Like Jira, every read only
  returns what the token's account may browse.
"""

import re
import time
import zlib
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from fakes import oauthkit
from fakes.config import Config
from fakes.store import Store


def adf(text: str) -> dict[str, Any]:
    """Atlassian Document Format for plain paragraphs."""
    return {
        "type": "doc",
        "version": 1,
        "content": [{"type": "paragraph", "content": [{"type": "text", "text": p}]} for p in text.split("\n\n")],
    }


def _ago(days: float) -> str:
    return (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S.000+0000")


def seed() -> dict[str, Any]:
    accounts = {
        "acc-alice": {
            "accountId": "acc-alice",
            "displayName": "Alice Admin",
            "emailAddress": "alice@acme.test",
            "site": "cloud-acme",
        },
        "acc-bob": {
            "accountId": "acc-bob",
            "displayName": "Bob Builder",
            "emailAddress": "bob@acme.test",
            "site": "cloud-acme",
        },
        "acc-quinn": {
            "accountId": "acc-quinn",
            "displayName": "Quinn Tester",
            "emailAddress": "quinn@acme.test",
            "site": "cloud-acme",
        },
        "acc-dave": {
            "accountId": "acc-dave",
            "displayName": "Dave Globex",
            "emailAddress": "dave@globex.test",
            "site": "cloud-globex",
        },
    }
    projects = {
        "PAY": {
            "key": "PAY",
            "name": "Payments",
            "site": "cloud-acme",
            "browse": ["acc-alice", "acc-bob", "acc-quinn"],
        },
        "WEB": {"key": "WEB", "name": "Website", "site": "cloud-acme", "browse": ["acc-alice", "acc-bob", "acc-quinn"]},
        "SEC": {"key": "SEC", "name": "Security", "site": "cloud-acme", "browse": ["acc-alice"]},
        "OPS": {"key": "OPS", "name": "Operations", "site": "cloud-globex", "browse": ["acc-dave"]},
    }
    issues = [
        ("PAY-101", "Checkout returns 500 for carts with 20 or more items", "In Progress", "acc-bob", "High", 0.2,
         "Since this morning's deploy, carts with 20 or more items fail at checkout with a 500.\n\nStack trace points to cart-service pricing."),
        ("PAY-102", "Refunds for EU cards are delayed by two days", "To Do", None, "Medium", 3,
         "Customers in the EU report refunds arriving two days late."),
        ("PAY-103", "Add retries to the payment webhook consumer", "Done", "acc-alice", "Low", 9,
         "Webhook deliveries are dropped when the consumer restarts."),
        ("WEB-7", "Search is slow in the EU region", "In Progress", "acc-quinn", "High", 1,
         "p95 above 4 seconds for EU customers since yesterday."),
        ("WEB-8", "SSO login loops on Safari 17", "To Do", None, "High", 0.5,
         "Signing in with Okta on Safari 17 lands on the sign in page again."),
        ("SEC-3", "Rotate the staging token pasted in Slack", "In Progress", "acc-alice", "Highest", 0.1,
         "A staging token was pasted in #bugs. Rotate it and audit its use."),
        ("OPS-1", "Payment webhooks delayed by 15 minutes", "In Progress", "acc-dave", "High", 0.3,
         "Provider status page shows degraded webhooks."),
    ]  # fmt: skip
    return {
        "sites": {
            "cloud-acme": {"id": "cloud-acme", "url": "https://acme.atlassian.net", "name": "acme"},
            "cloud-globex": {"id": "cloud-globex", "url": "https://globex.atlassian.net", "name": "globex"},
        },
        "accounts": accounts,
        "projects": projects,
        "issues": {
            key: {
                "key": key,
                "summary": s,
                "status": st,
                "assignee": a,
                "priority": p,
                "updated": _ago(d),
                "created": _ago(d + 1),
                "description": desc,
                "project": key.split("-")[0],
            }
            for key, s, st, a, p, d, desc in issues
        },
        "codes": {},
        "access": {},
        "refresh": {},
    }


class JqlError(ValueError):
    pass


CLAUSE = re.compile(
    r"""^\s*(?P<field>[a-zA-Z]+)\s*(?P<op>=|!=|~|>=|<=|\bin\b|\bis\b)\s*(?P<value>\([^)]*\)|"[^"]*"|'[^']*'|[^\s]+(?:\(\))?)\s*$""",
    re.IGNORECASE,
)


def _values(raw: str) -> list[str]:
    raw = raw.strip()
    if raw.startswith("("):
        return [v.strip().strip("\"'") for v in raw[1:-1].split(",") if v.strip()]
    return [raw.strip("\"'")]


def match_jql(jql: str, issue: dict[str, Any], me: str) -> bool:
    query = re.split(r"\border\s+by\b", jql, flags=re.IGNORECASE)[0].strip()
    if not query:
        return True
    for clause in re.split(r"\s+and\s+", query, flags=re.IGNORECASE):
        m = CLAUSE.match(clause)
        if not m:
            raise JqlError(f"Error in the JQL Query: cannot parse '{clause.strip()}'")
        field, op, values = m["field"].lower(), m["op"].lower(), _values(m["value"])
        if field in ("project",):
            actual = issue["project"]
        elif field in ("key", "issuekey", "issue"):
            actual = issue["key"]
        elif field == "status":
            actual = issue["status"]
        elif field == "priority":
            actual = issue["priority"]
        elif field == "assignee":
            actual = issue["assignee"] or "EMPTY"
            values = [
                me if v.lower() == "currentuser()" else ("EMPTY" if v.upper() in ("EMPTY", "NULL") else v)
                for v in values
            ]
        elif field in ("text", "summary"):
            if op != "~":
                raise JqlError(f"The operator '{op}' is not supported by the '{field}' field.")
            haystack = (issue["summary"] + " " + (issue["description"] if field == "text" else "")).lower()
            if not all(w in haystack for w in values[0].lower().split()):
                return False
            continue
        elif field in ("updated", "created"):
            rel = re.fullmatch(r"-(\d+)([dh])", values[0])
            if not rel or op not in (">=", "<="):
                raise JqlError(f"Unsupported date condition '{clause.strip()}'")
            delta = timedelta(days=int(rel[1])) if rel[2] == "d" else timedelta(hours=int(rel[1]))
            when = datetime.strptime(issue[field], "%Y-%m-%dT%H:%M:%S.000+0000").replace(tzinfo=UTC)
            limit = datetime.now(UTC) - delta
            if (op == ">=" and when < limit) or (op == "<=" and when > limit):
                return False
            continue
        else:
            raise JqlError(f"Field '{field}' does not exist or you do not have permission to view it.")
        wanted = [v.lower() for v in values]
        hit = actual.lower() in wanted
        if (op in ("=", "in", "is") and not hit) or (op == "!=" and hit):
            return False
    return True


class Atlassian:
    def __init__(self, config: Config, store: Store) -> None:
        self.config = config
        self.store = store

    @property
    def s(self) -> dict[str, Any]:
        state: dict[str, Any] = self.store.data["atlassian"]
        return state

    def reset(self) -> None:
        self.store.data["atlassian"] = seed()

    def issue_json(self, issue: dict[str, Any], fields: list[str] | None = None) -> dict[str, Any]:
        account = self.s["accounts"].get(issue["assignee"] or "")
        project = self.s["projects"][issue["project"]]
        full = {
            "summary": issue["summary"],
            "status": {"name": issue["status"]},
            "assignee": {"accountId": account["accountId"], "displayName": account["displayName"]} if account else None,
            "priority": {"name": issue["priority"]},
            "project": {"key": project["key"], "name": project["name"]},
            "issuetype": {"name": "Bug"},
            "updated": issue["updated"],
            "created": issue["created"],
            "description": adf(issue["description"]),
        }
        chosen = full if not fields or "*all" in fields else {k: v for k, v in full.items() if k in fields}
        site = self.s["sites"][project["site"]]
        return {
            "id": str(zlib.crc32(issue["key"].encode()) % 100000),
            "key": issue["key"],
            "self": f"{site['url']}/rest/api/3/issue/{issue['key']}",
            "fields": chosen,
        }

    def can_browse(self, account_id: str, project_key: str) -> bool:
        project = self.s["projects"].get(project_key)
        return bool(project and account_id in project["browse"])


def error(status: int, *messages: str) -> JSONResponse:
    return JSONResponse({"errorMessages": list(messages), "errors": {}}, status_code=status)


def build_router(jira: Atlassian) -> APIRouter:
    router = APIRouter()
    cfg = jira.config

    @router.get("/atlassian/authorize")
    async def authorize(request: Request) -> HTMLResponse:
        p = request.query_params
        if p.get("client_id") != cfg.atlassian_client_id:
            return HTMLResponse("Unknown client_id", status_code=400)
        accounts = [
            (a["accountId"], f"{a['displayName']} ({a['emailAddress']}, {jira.s['sites'][a['site']]['url']})")
            for a in jira.s["accounts"].values()
        ]
        hidden = {k: p.get(k, "") for k in ("redirect_uri", "state", "scope")}
        return HTMLResponse(
            oauthkit.consent_page(
                "Jira wants to access your Atlassian account",
                "/atlassian/authorize",
                hidden,
                accounts,
                p.get("scope", ""),
            )
        )

    @router.post("/atlassian/authorize")
    async def decide(request: Request) -> RedirectResponse:
        f = await request.form()
        uri, state = str(f.get("redirect_uri", "")), str(f.get("state", ""))
        if f.get("decision") != "allow":
            return RedirectResponse(oauthkit.redirect_to(uri, error="access_denied", state=state), status_code=302)
        with jira.store.lock:
            code = oauthkit.new_code(
                jira.s["codes"], account=str(f.get("account")), scope=str(f.get("scope", "")), redirect_uri=uri
            )
            jira.store.save()
        return RedirectResponse(oauthkit.redirect_to(uri, code=code, state=state), status_code=302)

    @router.post("/atlassian/oauth/token")
    async def token(request: Request) -> JSONResponse:
        body = await request.json()
        if body.get("client_id") != cfg.atlassian_client_id or body.get("client_secret") != cfg.atlassian_client_secret:
            return JSONResponse({"error": "access_denied", "error_description": "Unauthorized"}, status_code=401)
        with jira.store.lock:
            if body.get("grant_type") == "authorization_code":
                data = oauthkit.take_code(jira.s["codes"], str(body.get("code")))
                if data is None or data["redirect_uri"] != body.get("redirect_uri"):
                    return JSONResponse(
                        {"error": "invalid_grant", "error_description": "Invalid authorization code"}, status_code=403
                    )
                account, scope = data["account"], data["scope"]
            elif body.get("grant_type") == "refresh_token":
                old = jira.s["refresh"].pop(str(body.get("refresh_token")), None)  # rotating: single use
                if old is None:
                    return JSONResponse(
                        {"error": "invalid_grant", "error_description": "Unknown or invalid refresh token."},
                        status_code=403,
                    )
                account, scope = old["account"], old["scope"]
            else:
                return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)
            tokens = oauthkit.issue_tokens(jira.s, "atl_", cfg.atlassian_token_ttl, account=account, scope=scope)
            jira.store.save()
        return JSONResponse({**tokens, "scope": scope, "token_type": "Bearer"})

    def who(request: Request) -> dict[str, Any] | None:
        return oauthkit.bearer(jira.s, request.headers.get("authorization"))

    @router.get("/atlassian/api/oauth/token/accessible-resources")
    async def resources(request: Request) -> JSONResponse:
        record = who(request)
        if record is None:
            return JSONResponse({"code": 401, "message": "Unauthorized"}, status_code=401)
        site = jira.s["sites"][jira.s["accounts"][record["account"]]["site"]]
        return JSONResponse([{**site, "scopes": record["scope"].split(), "avatarUrl": ""}])

    def context(request: Request, cloudid: str) -> tuple[dict[str, Any] | None, JSONResponse | None]:
        record = who(request)
        if record is None:
            return None, JSONResponse({"code": 401, "message": "Unauthorized"}, status_code=401)
        account = jira.s["accounts"][record["account"]]
        if account["site"] != cloudid:
            return None, error(404, "Site not found or you do not have access")
        return account, None

    base = "/atlassian/api/ex/jira/{cloudid}/rest/api/3"

    @router.get(base + "/myself")
    async def myself(request: Request, cloudid: str) -> JSONResponse:
        account, err = context(request, cloudid)
        if err:
            return err
        assert account is not None
        return JSONResponse({k: v for k, v in account.items() if k != "site"})

    @router.post(base + "/search/jql")
    async def search(request: Request, cloudid: str) -> JSONResponse:
        account, err = context(request, cloudid)
        if err:
            return err
        assert account is not None
        body = await request.json()
        jql = str(body.get("jql") or "")
        try:
            hits = [
                i for i in jira.s["issues"].values()
                if jira.s["projects"][i["project"]]["site"] == cloudid
                and jira.can_browse(account["accountId"], i["project"])
                and match_jql(jql, i, account["accountId"])
            ]  # fmt: skip
        except JqlError as exc:
            return error(400, str(exc))
        hits.sort(key=lambda i: i["updated"], reverse=True)
        start = int(body.get("nextPageToken") or 0)
        size = max(1, min(int(body.get("maxResults") or 50), 100))
        page = hits[start : start + size]
        more = start + size < len(hits)
        out: dict[str, Any] = {"issues": [jira.issue_json(i, body.get("fields")) for i in page], "isLast": not more}
        if more:
            out["nextPageToken"] = str(start + size)
        return JSONResponse(out)

    @router.get(base + "/issue/{key}")
    async def issue(request: Request, cloudid: str, key: str) -> JSONResponse:
        account, err = context(request, cloudid)
        if err:
            return err
        assert account is not None
        found = jira.s["issues"].get(key.upper())
        if (
            not found
            or jira.s["projects"][found["project"]]["site"] != cloudid
            or not jira.can_browse(account["accountId"], found["project"])
        ):
            return error(404, "Issue does not exist or you do not have permission to see it.")
        fields = request.query_params.get("fields")
        return JSONResponse(jira.issue_json(found, fields.split(",") if fields else None))

    @router.get(base + "/project/search")
    async def projects(request: Request, cloudid: str) -> JSONResponse:
        account, err = context(request, cloudid)
        if err:
            return err
        assert account is not None
        visible = [
            {"key": p["key"], "name": p["name"]}
            for p in jira.s["projects"].values()
            if p["site"] == cloudid and account["accountId"] in p["browse"]
        ]
        return JSONResponse({"values": visible, "startAt": 0, "maxResults": 50, "total": len(visible), "isLast": True})

    @router.get(base + "/user/search")
    async def user_search(request: Request, cloudid: str) -> JSONResponse:
        _, err = context(request, cloudid)
        if err:
            return err
        query = request.query_params.get("query", "").lower()
        found = [
            {k: v for k, v in a.items() if k != "site"}
            for a in jira.s["accounts"].values()
            if a["site"] == cloudid
            and query
            and (query in a["emailAddress"].lower() or query in a["displayName"].lower())
        ]
        return JSONResponse(found)

    @router.get(base + "/user/permission/search")
    async def permission_search(request: Request, cloudid: str) -> JSONResponse:
        _, err = context(request, cloudid)
        if err:
            return err
        p = request.query_params
        if p.get("permissions") != "BROWSE_PROJECTS":
            return error(400, "Only BROWSE_PROJECTS is supported here")
        project = p.get("projectKey") or (p.get("issueKey", "").split("-")[0] if p.get("issueKey") else "")
        account_id = p.get("accountId", "")
        accounts = [
            a for a in jira.s["accounts"].values() if a["site"] == cloudid and jira.can_browse(a["accountId"], project)
        ]
        if account_id:
            accounts = [a for a in accounts if a["accountId"] == account_id]
        return JSONResponse([{k: v for k, v in a.items() if k != "site"} for a in accounts])

    @router.post("/_admin/atlassian/expire-tokens")
    def expire() -> dict[str, int]:
        with jira.store.lock:
            n = oauthkit.expire_all(jira.s)
            jira.store.save()
        return {"expired": n}

    @router.get("/_admin/atlassian/tokens")
    def tokens() -> dict[str, int]:
        now = time.time()
        return {
            "access_live": sum(1 for r in jira.s["access"].values() if r["exp"] > now),
            "refresh": len(jira.s["refresh"]),
        }

    return router
