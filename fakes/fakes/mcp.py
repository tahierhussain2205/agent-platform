"""A minimal MCP server (Streamable HTTP transport, JSON responses) for the stand-ins: ``initialize``
(with an ``Mcp-Session-Id``), ``notifications/initialized``, ``tools/list`` and ``tools/call``."""

import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse, Response

PROTOCOL = "2025-06-18"


@dataclass
class Tool:
    name: str
    description: str
    input_schema: dict[str, Any]
    run: Callable[[str, dict[str, Any]], str]  # (caller identity, arguments) -> text
    read_only: bool = True
    destructive: bool = False

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema,
            "annotations": {"readOnlyHint": self.read_only, "destructiveHint": self.destructive, "openWorldHint": True},
        }


@dataclass
class McpServer:
    name: str
    tools: dict[str, Tool]
    sessions: set[str] = field(default_factory=set)
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def handle(self, request: Request, identity: str | None) -> Response:
        if identity is None:
            return JSONResponse(
                {"jsonrpc": "2.0", "id": None, "error": {"code": -32001, "message": "Unauthorized"}},
                status_code=401,
                headers={"WWW-Authenticate": 'Bearer error="invalid_token"'},
            )
        try:
            msg = await request.json()
        except ValueError:
            return JSONResponse(
                {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}, status_code=400
            )
        method, rid, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
        if method == "initialize":
            session = secrets.token_hex(16)
            self.sessions.add(session)
            result = {
                "protocolVersion": PROTOCOL,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": self.name, "version": "1.0.0"},
            }
            return JSONResponse({"jsonrpc": "2.0", "id": rid, "result": result}, headers={"Mcp-Session-Id": session})
        if request.headers.get("mcp-session-id") not in self.sessions:
            return JSONResponse(
                {"jsonrpc": "2.0", "id": rid, "error": {"code": -32000, "message": "Bad Request: no valid session"}},
                status_code=400,
            )
        if method == "notifications/initialized":
            return Response(status_code=202)
        if method == "tools/list":
            return JSONResponse(
                {"jsonrpc": "2.0", "id": rid, "result": {"tools": [t.describe() for t in self.tools.values()]}}
            )
        if method == "tools/call":
            tool = self.tools.get(str(params.get("name")))
            if tool is None:
                return JSONResponse(
                    {
                        "jsonrpc": "2.0",
                        "id": rid,
                        "error": {"code": -32602, "message": f"Unknown tool: {params.get('name')}"},
                    }
                )
            self.calls.append({"tool": tool.name, "identity": identity, "arguments": params.get("arguments") or {}})
            try:
                text, is_error = tool.run(identity, params.get("arguments") or {}), False
            except (KeyError, ValueError) as exc:
                text, is_error = f"Error: {exc}", True
            return JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "id": rid,
                    "result": {"content": [{"type": "text", "text": text}], "isError": is_error},
                }
            )
        return JSONResponse(
            {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"Method not found: {method}"}}
        )
