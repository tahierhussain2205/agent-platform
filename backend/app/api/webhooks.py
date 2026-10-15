"""Inbound webhooks. Clerk: identity sync through Svix signed events."""

import json
import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app import kv
from app.auth.console import resources
from app.identity import svix
from app.identity.sync import apply_event
from app.settings import get_settings

log = logging.getLogger(__name__)
router = APIRouter()

PROCESSING_TTL = 60  # a delivery in flight; a concurrent retry is told to come back later
DONE_TTL = 24 * 3600  # Svix retries for about a day


@router.post("/webhooks/clerk", include_in_schema=False)
async def clerk_webhook(request: Request) -> JSONResponse:
    res = resources(request)
    body = await request.body()
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
    return JSONResponse({"status": outcome})
