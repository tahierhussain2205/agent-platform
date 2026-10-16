"""Inbound webhooks. Clerk: identity sync through Svix signed events."""

import json
import logging
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app import kv
from app.api.temporal_client import temporal_client
from app.auth.console import resources
from app.identity import svix
from app.identity.sync import apply_event
from app.schedules.temporal import start_member_removed
from app.settings import get_settings

log = logging.getLogger(__name__)
router = APIRouter()

MAX_BODY_BYTES = 1024 * 1024  # Clerk events are a few KB; refuse anything absurd before reading it
PROCESSING_TTL = 60  # a delivery in flight; a concurrent retry is told to come back later
DONE_TTL = 24 * 3600  # Svix retries for about a day


@router.post("/webhooks/clerk", include_in_schema=False)
async def clerk_webhook(request: Request) -> JSONResponse:
    res = resources(request)
    declared = request.headers.get("content-length")
    if declared is not None and (not declared.isdigit() or int(declared) > MAX_BODY_BYTES):
        return JSONResponse(
            {"error": {"code": "too_large", "message": "Payload too large."}}, status_code=413
        )
    body = await request.body()
    if len(body) > MAX_BODY_BYTES:
        return JSONResponse(
            {"error": {"code": "too_large", "message": "Payload too large."}}, status_code=413
        )
    headers = request.headers
    try:
        svix.verify(
            get_settings().clerk_webhook_secrets,
            headers.get("svix-id"), headers.get("svix-timestamp"), headers.get("svix-signature"), body,
        )  # fmt: skip
    except svix.WebhookVerificationError as exc:
        log.warning("rejected clerk webhook: %s", exc)
        return JSONResponse(
            {"error": {"code": "bad_signature", "message": "Invalid signature."}}, status_code=400
        )
    try:
        event = json.loads(body)
        event_type = str(event["type"])
    except (ValueError, KeyError, TypeError):
        return JSONResponse(
            {"error": {"code": "bad_payload", "message": "Invalid payload."}}, status_code=400
        )
    if res.identity_pool is None:
        return JSONResponse(
            {"error": {"code": "unavailable", "message": "Identity sync unavailable."}}, status_code=503
        )

    msg_id = headers["svix-id"]
    key = kv.global_key("clerk-webhook", msg_id)
    if res.kv is not None and not await kv.claim(res.kv, key, PROCESSING_TTL, "processing"):
        state = await res.kv.get(key)
        if state == b"done":
            return JSONResponse({"status": "duplicate"})
        return JSONResponse({"status": "in_progress"}, status_code=409)  # Svix retries later

    try:
        async with res.identity_pool.connection() as conn:
            outcome = await apply_event(conn, event)
    except Exception:
        log.exception("clerk webhook failed", extra={"event_type": event_type, "svix_id": msg_id})
        if res.kv is not None:
            await kv.release(res.kv, key)
        return JSONResponse({"error": {"code": "failed", "message": "Processing failed."}}, status_code=500)
    if res.kv is not None:
        await res.kv.set(key, "done", ex=DONE_TTL)
    log.info("clerk webhook %s", outcome, extra={"event_type": event_type, "svix_id": msg_id})
    if event_type == "organizationMembership.deleted" and outcome == "applied":
        await _after_removal(request, res, event)
    return JSONResponse({"status": outcome})


async def _after_removal(request: Request, res: Any, event: dict[str, Any]) -> None:
    """The removal is committed: cancel the person's running tasks and notify about their schedules."""
    data = event.get("data") or {}
    try:
        clerk_user = data["public_user_data"]["user_id"]
        clerk_org = data["organization"]["id"]
    except (KeyError, TypeError):
        return
    if res.db_pool is None:
        return
    async with res.db_pool.connection() as conn:
        row = await (
            await conn.execute(
                "select org_id, membership_id, membership_status from app.resolve_console_identity(%s, %s)",
                (clerk_user, clerk_org),
            )
        ).fetchone()
    if row is not None and row[2] == "removed":
        await start_member_removed(await temporal_client(request), get_settings(), row[0], row[1])
