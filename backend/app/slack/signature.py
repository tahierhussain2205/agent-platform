"""Slack request signatures: HMAC-SHA256 over ``v0:{timestamp}:{raw body}`` with the signing secret,
sent as ``X-Slack-Signature: v0=<hex>`` with ``X-Slack-Request-Timestamp`` (spec 14.2)."""

import hashlib
import hmac
import time

TOLERANCE_SECONDS = 5 * 60


class SlackSignatureError(Exception):
    pass


def sign(secret: str, timestamp: int | str, body: bytes) -> str:
    base = f"v0:{timestamp}:".encode() + body
    return "v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()


def verify(
    secret: str, timestamp: str | None, signature: str | None, body: bytes, now: float | None = None
) -> None:
    if not timestamp or not signature:
        raise SlackSignatureError("missing signature headers")
    try:
        sent_at = int(timestamp)
    except ValueError as exc:
        raise SlackSignatureError("bad timestamp") from exc
    if abs((now if now is not None else time.time()) - sent_at) > TOLERANCE_SECONDS:
        raise SlackSignatureError("timestamp outside the tolerance window")
    if not hmac.compare_digest(sign(secret, timestamp, body), signature):
        raise SlackSignatureError("signature mismatch")
