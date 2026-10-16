"""MCP tool adapter for the long tail (spec T5): vendor MCP servers (Google Workspace, with the
integration's OAuth token) and customer supplied ones (an API key, admin approval required).

Streamable HTTP transport: ``initialize`` (the server returns an ``Mcp-Session-Id``),
``notifications/initialized``, ``tools/list`` and ``tools/call``; JSON or event stream responses.
Operations come from discovery: a tool marked read only is a read; a destructive one is
irreversible; anything else is a reversible write. Output is capped and redacted like every tool's.
"""

import json
import re
import zlib
from typing import Any

import httpx

from app.policy import Member
from app.policy.redaction import redact
from app.tools.base import CredentialMode, Operation, Source, Target, ToolContext, ToolResult, cap
from app.vault_service import Credential

PROTOCOL = "2025-06-18"


class McpError(Exception):
    pass


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9_]+", "_", name.lower()).strip("_")[:40] or "tool"


def operation_for(tool: dict[str, Any], mode: CredentialMode) -> Operation:
    hints = tool.get("annotations") or {}
    read_only = bool(hints.get("readOnlyHint"))
    raw = tool.get("inputSchema")
    schema: dict[str, Any] = raw if isinstance(raw, dict) else {"type": "object"}
    return Operation(
        slug(str(tool.get("name"))),
        str(tool.get("description") or tool.get("title") or tool.get("name"))[:1000],
        schema,
        kind="read" if read_only else "write",
        reversible=not bool(hints.get("destructiveHint")),
        credential_mode=mode,
    )


class McpClient:
    def __init__(self, http: httpx.AsyncClient) -> None:
        self.http = http

    async def _send(
        self, url: str, token: str, message: dict[str, Any], session: str | None
    ) -> tuple[Any, str | None]:
        headers = {
            "authorization": f"Bearer {token}",
            "accept": "application/json, text/event-stream",
            "content-type": "application/json",
            "mcp-protocol-version": PROTOCOL,
        }
        if session:
            headers["mcp-session-id"] = session
        response = await self.http.post(url, json=message, headers=headers)
        if response.status_code == 401:
            raise McpError("the MCP server refused the credential")
        if response.status_code == 202:
            return None, response.headers.get("mcp-session-id")
        if response.status_code >= 400:
            raise McpError(f"the MCP server answered {response.status_code}")
        if response.headers.get("content-type", "").startswith("text/event-stream"):
            data = [line[5:].strip() for line in response.text.splitlines() if line.startswith("data:")]
            body = json.loads(data[-1]) if data else {}
        else:
            body = response.json()
        if "error" in body:
            raise McpError(str((body["error"] or {}).get("message", "error")))
        return body.get("result"), response.headers.get("mcp-session-id")

    async def open(self, url: str, token: str) -> str | None:
        init = {
            "protocolVersion": PROTOCOL,
            "capabilities": {},
            "clientInfo": {"name": "agent-platform", "version": "1"},
        }
        _, session = await self._send(
            url, token, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": init}, None
        )
        await self._send(url, token, {"jsonrpc": "2.0", "method": "notifications/initialized"}, session)
        return session

    async def list_tools(self, url: str, token: str) -> list[dict[str, Any]]:
        session = await self.open(url, token)
        result, _ = await self._send(
            url, token, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}, session
        )
        return list((result or {}).get("tools") or [])

    async def call(
        self, url: str, token: str, session: str | None, name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        message = {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
        result, _ = await self._send(url, token, message, session)
        return dict(result or {})


class McpAdapter:
    def __init__(self, key: str, url: str, tools: list[dict[str, Any]], mode: CredentialMode) -> None:
        self.key = key
        self.url = url
        self.mode = mode
        self.tools: dict[str, dict[str, Any]] = {}
        self.operations: dict[str, Operation] = {}
        self.load(tools)

    def load(self, tools: list[dict[str, Any]]) -> None:
        self.tools = {slug(str(t.get("name"))): t for t in tools if t.get("name")}
        self.operations = {name: operation_for(t, self.mode) for name, t in self.tools.items()}

    async def resolve_target(self, ctx: ToolContext, operation: str, args: dict[str, Any]) -> Target | None:
        return None

    async def discover(self, ctx: ToolContext, credential: Credential) -> list[dict[str, Any]]:
        tools = await McpClient(ctx.http).list_tools(self.url, credential.token)
        self.load(tools)
        return [{k: t.get(k) for k in ("name", "description", "inputSchema", "annotations")} for t in tools]

    async def audience_ok(
        self, ctx: ToolContext, credential: Credential, source: Source, members: list[Member]
    ) -> bool:
        # A customer's own server holds org data (every member listed is an identified org member);
        # a vendor server acting with one person's account holds their data only.
        return self.mode == "org"

    async def execute(
        self, ctx: ToolContext, operation: str, args: dict[str, Any], credential: Any = None
    ) -> ToolResult:
        if not isinstance(credential, Credential):
            return ToolResult(f"{self.key} is not connected.", is_error=True)
        tool = self.tools.get(operation)
        if tool is None:
            return ToolResult(f"Unknown tool {operation}.", is_error=True)
        client = McpClient(ctx.http)
        try:
            key = f"mcp:session:{self.key}:{credential.connection_id}"
            if key not in ctx.cache:
                ctx.cache[key] = await client.open(self.url, credential.token)
            result = await client.call(self.url, credential.token, ctx.cache[key], str(tool["name"]), args)
        except (McpError, httpx.HTTPError, ValueError) as exc:
            return ToolResult(f"{self.key} error: {exc}", is_error=True)
        texts = [str(c.get("text", "")) for c in result.get("content") or [] if c.get("type") == "text"]
        body = "\n".join(texts).strip() or "(no output)"
        if result.get("isError"):
            return ToolResult(cap(redact(f"{tool['name']} failed: {body}")[0]), is_error=True)
        source = Source(
            ref=f"mcp:{self.key}:{operation}:{zlib.crc32(json.dumps(args, sort_keys=True).encode())}",
            channel_external_id="",
            channel_name=self.key,
            visibility="restricted",
            ts="",
            label=f"{self.key} {tool['name']}",
            kind="mcp",
            audience=f"{self.key}:{operation}",
        )
        short = ctx.refs.add(source)
        flat = body.replace("\n", " ⏎ ")
        return ToolResult(cap(redact(f"[{short}] ({self.key}) {tool['name']}: {flat}")[0]))
