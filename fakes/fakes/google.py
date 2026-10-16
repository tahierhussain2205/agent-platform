"""Google stand-in: OAuth 2.0 (offline access, PKCE, refresh, revocation, userinfo) and a vendor MCP
server with Sheets and Drive tools, authenticated with the Google access token. Also a customer
run MCP server (API key auth), for the "custom MCP servers need admin approval" path."""

import base64
import hashlib
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from fakes import oauthkit
from fakes.config import Config
from fakes.mcp import McpServer, Tool
from fakes.store import Store, outdated


def seed() -> dict[str, Any]:
    return {
        "accounts": {
            "alice@acme.test": {"sub": "g-alice", "name": "Alice Admin"},
            "bob@acme.test": {"sub": "g-bob", "name": "Bob Builder"},
            "dave@globex.test": {"sub": "g-dave", "name": "Dave Globex"},
        },
        "sheets": {
            "alice@acme.test": {
                "bug-metrics": {
                    "title": "Bug metrics",
                    "values": [["Week", "Opened", "Closed"], ["W40", "12", "9"], ["W41", "15", "11"]],
                },
            },
            "bob@acme.test": {
                "bob-notes": {"title": "Bob's notes", "values": [["Item", "Status"], ["Cart fix", "In review"]]}
            },
            "dave@globex.test": {"ops": {"title": "Ops", "values": [["Incident", "Owner"], ["INC-17", "dave"]]}},
        },
        "files": {
            "alice@acme.test": [
                {"id": "doc-1", "name": "Postmortem: checkout 500s"},
                {"id": "doc-2", "name": "Q4 roadmap"},
            ],
            "bob@acme.test": [{"id": "doc-3", "name": "Cart service notes"}],
            "dave@globex.test": [{"id": "doc-9", "name": "Webhook runbook"}],
        },
        "incidents": [{"id": "INC-201", "title": "Elevated checkout errors", "status": "investigating"}],
        "codes": {},
        "access": {},
        "refresh": {},
    }


class Google:
    def __init__(self, config: Config, store: Store) -> None:
        self.config = config
        self.store = store
        self.mcp = McpServer("google-workspace", self._google_tools())
        self.custom = McpServer("status-page", self._custom_tools())

    @property
    def s(self) -> dict[str, Any]:
        state: dict[str, Any] = self.store.data["google"]
        return state

    def outdated(self) -> bool:
        return outdated(self.store.data.get("google") or {}, seed())

    def reset(self) -> None:
        self.store.data["google"] = seed()
        self.mcp.calls.clear()
        self.custom.calls.clear()

    def _google_tools(self) -> dict[str, Tool]:
        def read_range(who: str, args: dict[str, Any]) -> str:
            sheet = self.s["sheets"].get(who, {}).get(str(args["spreadsheet_id"]))
            if sheet is None:
                raise ValueError(f"spreadsheet {args['spreadsheet_id']} not found or not shared with you")
            return f"{sheet['title']}\n" + "\n".join(" | ".join(row) for row in sheet["values"])

        def drive_search(who: str, args: dict[str, Any]) -> str:
            query = str(args.get("query", "")).lower()
            hits = [f"{f['id']}: {f['name']}" for f in self.s["files"].get(who, []) if query in f["name"].lower()]
            sheets = [
                f"{k}: {v['title']} (spreadsheet)"
                for k, v in self.s["sheets"].get(who, {}).items()
                if query in v["title"].lower()
            ]
            return "\n".join(hits + sheets) or "No files match."

        def append_row(who: str, args: dict[str, Any]) -> str:
            sheet = self.s["sheets"].get(who, {}).get(str(args["spreadsheet_id"]))
            if sheet is None:
                raise ValueError(f"spreadsheet {args['spreadsheet_id']} not found")
            with self.store.lock:
                sheet["values"].append([str(v) for v in args.get("values") or []])
                self.store.save()
            return f"Appended a row to {sheet['title']}."

        return {
            "sheets_read_range": Tool(
                "sheets_read_range", "Read values from a Google Sheets spreadsheet you can access.",
                {"type": "object", "properties": {"spreadsheet_id": {"type": "string"}, "range": {"type": "string"}}, "required": ["spreadsheet_id"]},
                read_range,
            ),
            "drive_search": Tool(
                "drive_search", "Search your Google Drive files by name.",
                {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}, drive_search,
            ),
            "sheets_append_row": Tool(
                "sheets_append_row", "Append a row to a spreadsheet.",
                {"type": "object", "properties": {"spreadsheet_id": {"type": "string"}, "values": {"type": "array", "items": {"type": "string"}}}, "required": ["spreadsheet_id", "values"]},
                append_row, read_only=False,
            ),
        }  # fmt: skip

    def _custom_tools(self) -> dict[str, Tool]:
        def incidents(who: str, args: dict[str, Any]) -> str:
            return "\n".join(f"{i['id']}: {i['title']} ({i['status']})" for i in self.s["incidents"])

        def restart(who: str, args: dict[str, Any]) -> str:
            return f"Restarted {args['service']}."

        return {
            "status_incidents": Tool("status_incidents", "List open incidents on the status page.", {"type": "object", "properties": {}}, incidents),
            "restart_service": Tool(
                "restart_service", "Restart a production service.",
                {"type": "object", "properties": {"service": {"type": "string"}}, "required": ["service"]},
                restart, read_only=False, destructive=True,
            ),
        }  # fmt: skip


def build_router(google: Google) -> APIRouter:
    router = APIRouter()
    cfg = google.config

    @router.get("/google/o/oauth2/v2/auth")
    async def authorize(request: Request) -> HTMLResponse:
        p = request.query_params
        if p.get("client_id") != cfg.google_client_id:
            return HTMLResponse("Error 401: invalid_client", status_code=401)
        hidden = {
            k: p.get(k, "") for k in ("redirect_uri", "state", "scope", "code_challenge", "code_challenge_method")
        }
        accounts = [(email, f"{a['name']} ({email})") for email, a in google.s["accounts"].items()]
        return HTMLResponse(
            oauthkit.consent_page(
                "Sign in with Google", "/google/o/oauth2/v2/auth", hidden, accounts, p.get("scope", "")
            )
        )

    @router.post("/google/o/oauth2/v2/auth")
    async def decide(request: Request) -> RedirectResponse:
        f = await request.form()
        uri, state = str(f.get("redirect_uri", "")), str(f.get("state", ""))
        if f.get("decision") != "allow":
            return RedirectResponse(oauthkit.redirect_to(uri, error="access_denied", state=state), status_code=302)
        with google.store.lock:
            code = oauthkit.new_code(
                google.s["codes"], email=str(f.get("account")), scope=str(f.get("scope", "")), redirect_uri=uri,
                challenge=str(f.get("code_challenge", "")),
            )  # fmt: skip
            google.store.save()
        return RedirectResponse(
            oauthkit.redirect_to(uri, code=code, state=state, scope=str(f.get("scope", ""))), status_code=302
        )

    @router.post("/google/token")
    async def token(request: Request) -> JSONResponse:
        f = await request.form()
        if f.get("client_id") != cfg.google_client_id or f.get("client_secret") != cfg.google_client_secret:
            return JSONResponse({"error": "invalid_client", "error_description": "Unauthorized"}, status_code=401)
        with google.store.lock:
            if f.get("grant_type") == "refresh_token":
                old = google.s["refresh"].get(str(f.get("refresh_token")))  # Google refresh tokens are long lived
                if old is None:
                    return JSONResponse(
                        {"error": "invalid_grant", "error_description": "Token has been expired or revoked."},
                        status_code=400,
                    )
                tokens = oauthkit.issue_tokens(google.s, "ya29", cfg.google_token_ttl, **old)
                google.s["refresh"].pop(tokens["refresh_token"])
                google.store.save()
                return JSONResponse(
                    {
                        "access_token": tokens["access_token"],
                        "expires_in": tokens["expires_in"],
                        "scope": old["scope"],
                        "token_type": "Bearer",
                    }
                )
            data = oauthkit.take_code(google.s["codes"], str(f.get("code")))
            if data is None or data["redirect_uri"] != f.get("redirect_uri"):
                return JSONResponse(
                    {"error": "invalid_grant", "error_description": "Malformed auth code."}, status_code=400
                )
            if data["challenge"]:
                verifier = str(f.get("code_verifier", ""))
                digest = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
                if digest != data["challenge"]:
                    return JSONResponse(
                        {"error": "invalid_grant", "error_description": "Invalid code verifier."}, status_code=400
                    )
            tokens = oauthkit.issue_tokens(
                google.s, "ya29", cfg.google_token_ttl, email=data["email"], scope=data["scope"]
            )
            google.store.save()
        return JSONResponse({**tokens, "scope": data["scope"], "token_type": "Bearer"})

    @router.post("/google/revoke")
    async def revoke(request: Request) -> Response:
        token = request.query_params.get("token") or str((await request.form()).get("token", ""))
        with google.store.lock:
            record = google.s["access"].pop(token, None) or google.s["refresh"].pop(token, None)
            if record:  # revoking either token revokes the grant
                for bucket in ("access", "refresh"):
                    for key in [k for k, v in google.s[bucket].items() if v.get("email") == record["email"]]:
                        google.s[bucket].pop(key)
            google.store.save()
        return Response(status_code=200) if record else JSONResponse({"error": "invalid_token"}, status_code=400)

    @router.get("/google/userinfo")
    async def userinfo(request: Request) -> JSONResponse:
        record = oauthkit.bearer(google.s, request.headers.get("authorization"))
        if record is None:
            return JSONResponse({"error": "invalid_token"}, status_code=401)
        account = google.s["accounts"][record["email"]]
        return JSONResponse(
            {"sub": account["sub"], "email": record["email"], "name": account["name"], "email_verified": True}
        )

    @router.post("/google/mcp")
    async def mcp(request: Request) -> Response:
        record = oauthkit.bearer(google.s, request.headers.get("authorization"))
        return await google.mcp.handle(request, record["email"] if record else None)

    @router.post("/mcp/status")
    async def custom_mcp(request: Request) -> Response:
        ok = request.headers.get("authorization") == f"Bearer {cfg.custom_mcp_key}"
        return await google.custom.handle(request, "api-key" if ok else None)

    @router.get("/_admin/google/mcp-calls")
    def calls() -> dict[str, Any]:
        return {"google": google.mcp.calls, "custom": google.custom.calls}

    @router.post("/_admin/google/expire-tokens")
    def expire() -> dict[str, int]:
        with google.store.lock:
            n = oauthkit.expire_all(google.s)
            google.store.save()
        return {"expired": n}

    return router
