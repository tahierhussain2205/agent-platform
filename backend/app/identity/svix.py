"""Svix webhook signatures (Clerk delivers webhooks through Svix).

Signed content is ``{svix-id}.{svix-timestamp}.{raw body}``, HMAC-SHA256 with the endpoint
secret (``whsec_<base64>``), base64 encoded. The ``svix-signature`` header holds one or more
space separated ``v1,<signature>`` entries (several during secret rotation). Several of our own
secrets may be configured too (comma separated), so rotation works on both sides.
"""

import base64
import hashlib
import hmac
import time


class WebhookVerificationError(Exception):
    pass


def _key(secret: str) -> bytes:
    secret = secret.strip()
    if secret.startswith("whsec_"):
        secret = secret[len("whsec_") :]
    try:
        return base64.b64decode(secret, validate=True)
    except ValueError as exc:
        raise WebhookVerificationError("webhook secret is not valid base64") from exc


def sign(secret: str, msg_id: str, timestamp: int, body: bytes) -> str:
    """The ``svix-signature`` value for a payload (used by tests and the local Clerk stand-in)."""
    digest = hmac.new(_key(secret), f"{msg_id}.{timestamp}.".encode() + body, hashlib.sha256).digest()
    return "v1," + base64.b64encode(digest).decode()


def verify(
    secrets: list[str],
    msg_id: str | None,
    timestamp: str | None,
    signature_header: str | None,
    body: bytes,
    tolerance_seconds: int = 300,
    now: float | None = None,
) -> None:
    if not secrets:
        raise WebhookVerificationError("no webhook secret configured")
    if not (msg_id and timestamp and signature_header):
        raise WebhookVerificationError("missing svix headers")
    try:
        sent_at = int(timestamp)
    except ValueError as exc:
        raise WebhookVerificationError("bad timestamp") from exc
    if abs((now if now is not None else time.time()) - sent_at) > tolerance_seconds:
        raise WebhookVerificationError("timestamp outside the tolerance window")
    signed = f"{msg_id}.{timestamp}.".encode() + body
    expected = [
        base64.b64encode(hmac.new(_key(s), signed, hashlib.sha256).digest()).decode() for s in secrets
    ]
    for entry in signature_header.split():
        version, _, signature = entry.partition(",")
        if version == "v1" and any(hmac.compare_digest(signature, e) for e in expected):
            return
    raise WebhookVerificationError("no matching signature")
