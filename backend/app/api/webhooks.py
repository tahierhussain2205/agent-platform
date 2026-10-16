"""Inbound webhooks. Clerk: identity sync through Svix signed events. GitHub: feedback on the pull
requests the agent opened (reviews, failed checks, merges), posted in their threads."""

import contextlib
import hashlib
import hmac
import json
import logging
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from temporalio.exceptions import WorkflowAlreadyStartedError

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


# ------------------------------------------------------------------ GitHub

GITHUB_DONE_TTL = 7 * 24 * 3600  # GitHub redelivers on request, with the same delivery id
FAILED_CHECKS = {"failure", "timed_out", "cancelled", "action_required", "startup_failure"}


def github_signature_ok(secret: str, body: bytes, header: str | None) -> bool:
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return bool(header) and hmac.compare_digest(expected, str(header))


def github_notices(event: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
    """The pull request events posted in threads: a submitted review, a check suite that failed, a
    pull request closed or merged. Review comment events are covered by their review's event."""
    repo = str((payload.get("repository") or {}).get("full_name") or "")
    sender = (payload.get("sender") or {}).get("login")
    action = payload.get("action")
    if not repo:
        return []
    if event == "pull_request_review" and action == "submitted":
        review = payload.get("review") or {}
        number = (payload.get("pull_request") or {}).get("number")
        reviewer = (review.get("user") or {}).get("login") or sender
        return [{"kind": "review", "repo": repo, "number": number, "review_id": review.get("id"),
                 "state": review.get("state"), "sender": reviewer}]  # fmt: skip
    if event == "check_suite" and action == "completed":
        suite = payload.get("check_suite") or {}
        if suite.get("conclusion") not in FAILED_CHECKS:
            return []
        return [{"kind": "checks_failed", "repo": repo, "number": p.get("number"),
                 "state": suite.get("conclusion"), "sender": sender, "sha": suite.get("head_sha")}
                for p in suite.get("pull_requests") or []]  # fmt: skip
    if event == "pull_request" and action == "closed":
        pr = payload.get("pull_request") or {}
        return [{"kind": "closed", "repo": repo, "number": pr.get("number"),
                 "state": "merged" if pr.get("merged") else "closed", "sender": sender}]  # fmt: skip
    return []


@router.post("/webhooks/github", include_in_schema=False)
async def github_webhook(request: Request) -> JSONResponse:
    """Signature first (on the raw body), then the delivery claimed once; each pull request is mapped
    to the task that opened it (``resolve_task_links``) and a workflow posts the notice in its thread.
    Unknown pull requests are ignored."""
    res = resources(request)
    declared = request.headers.get("content-length")
    if declared is not None and (not declared.isdigit() or int(declared) > MAX_BODY_BYTES):
        return JSONResponse(
            {"error": {"code": "too_large", "message": "Payload too large."}}, status_code=413
        )
    body = await request.body()
    secret = get_settings().github_webhook_secret
    if secret is None:
        return JSONResponse(
            {"error": {"code": "not_configured", "message": "Not configured."}}, status_code=503
        )
    if len(body) > MAX_BODY_BYTES or not github_signature_ok(
        secret.get_secret_value(), body, request.headers.get("x-hub-signature-256")
    ):
        log.warning("rejected github webhook: bad signature")
        return JSONResponse(
            {"error": {"code": "bad_signature", "message": "Invalid signature."}}, status_code=401
        )
    event = request.headers.get("x-github-event", "")
    delivery = request.headers.get("x-github-delivery", "")
    if event == "ping":
        return JSONResponse({"status": "pong"})
    try:
        payload = json.loads(body)
        notices = [n for n in github_notices(event, payload) if isinstance(n.get("number"), int)]
    except (ValueError, TypeError, AttributeError):
        return JSONResponse(
            {"error": {"code": "bad_payload", "message": "Invalid payload."}}, status_code=400
        )
    if not notices or not delivery or res.db_pool is None:
        return JSONResponse({"status": "ignored"}, status_code=202)
    key = kv.global_key("github-delivery", delivery)
    if res.kv is not None and not await kv.claim(res.kv, key, PROCESSING_TTL, "processing"):
        return JSONResponse({"status": "duplicate"})
    try:
        started = await _start_notices(request, res, delivery, notices)
    except Exception:
        log.exception("github webhook failed", extra={"event": event, "delivery": delivery})
        if res.kv is not None:
            await kv.release(res.kv, key)
        return JSONResponse({"error": {"code": "failed", "message": "Processing failed."}}, status_code=500)
    if res.kv is not None:
        await res.kv.set(key, "done", ex=GITHUB_DONE_TTL)
    log.info("github webhook", extra={"event": event, "delivery": delivery, "notices": started})
    return JSONResponse({"status": "accepted" if started else "ignored", "notices": started},
                        status_code=200 if started else 202)  # fmt: skip


async def _start_notices(request: Request, res: Any, delivery: str, notices: list[dict[str, Any]]) -> int:
    from app.agent.types import GitHubNotice

    started = 0
    for n in notices:
        async with res.db_pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "select org_id, task_id, session_id from app.resolve_task_links('pull_request', %s)",
                    (f"{n['repo']}#{n['number']}",),
                )
            ).fetchall()
        latest: dict[Any, tuple[Any, Any]] = {}
        for (
            org_id,
            task_id,
            session_id,
        ) in rows:  # one notice per thread: its latest task (ids are time ordered)
            if session_id is not None and (
                session_id not in latest or str(task_id) > str(latest[session_id][1])
            ):
                latest[session_id] = (org_id, task_id)
        if not latest:
            continue
        client = await temporal_client(request)
        if client is None:
            raise RuntimeError("Temporal is not reachable")
        for org_id, task_id in latest.values():
            notice = GitHubNotice(
                str(org_id), str(task_id), delivery, n["kind"], n["repo"], int(n["number"]),
                n.get("review_id"), n.get("state"), n.get("sender"), n.get("sha"),
            )  # fmt: skip
            with contextlib.suppress(WorkflowAlreadyStartedError):
                await client.start_workflow(
                    "GitHubNoticeWorkflow", notice, id=f"github-{delivery}-{task_id}-{n['number']}",
                    task_queue=get_settings().temporal_task_queue,
                )  # fmt: skip
            started += 1
    return started
