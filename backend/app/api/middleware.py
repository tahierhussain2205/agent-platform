"""Request context: request id, tenant reset, access log, last resort error response."""

import json
import logging
import re
import time
import uuid

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.context import org_id_var, request_id_var

log = logging.getLogger("app.access")
_VALID_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{8,64}$")
QUIET_PATHS = {"/healthz", "/readyz"}  # polled constantly; logged at debug level


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        incoming = dict(scope.get("headers", [])).get(b"x-request-id", b"").decode("latin-1")
        request_id = incoming if _VALID_REQUEST_ID.match(incoming) else uuid.uuid4().hex
        request_id_var.set(request_id)
        org_id_var.set(None)
        started = time.perf_counter()
        status = 500
        response_started = False

        async def send_wrapper(message: Message) -> None:
            nonlocal status, response_started
            if message["type"] == "http.response.start":
                status, response_started = message["status"], True
                message["headers"] = [*message.get("headers", []), (b"x-request-id", request_id.encode())]
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            # Unhandled: the unit of work has already rolled back. Answer here (not in Starlette's
            # outermost handler) so the response still carries the request id; never leak details.
            log.exception("unhandled error")
            if response_started:
                raise
            body = json.dumps(
                {"error": {"code": "internal", "message": "Something went wrong.", "request_id": request_id}}
            ).encode()
            headers = [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]
            await send_wrapper({"type": "http.response.start", "status": 500, "headers": headers})
            await send_wrapper({"type": "http.response.body", "body": body})
        finally:
            route = scope.get("route")
            path = getattr(route, "path", None) or scope.get("path", "")
            level = logging.DEBUG if scope.get("path") in QUIET_PATHS and status < 500 else logging.INFO
            log.log(
                level,
                "%s %s %d",
                scope.get("method"),
                path,
                status,
                extra={"http_method": scope.get("method"), "http_path": path, "http_status": status,
                       "duration_ms": round((time.perf_counter() - started) * 1000, 1)},
            )  # fmt: skip
