"""Signed, expiring OAuth ``state`` for integration connect and link flows. Single use is enforced by
the caller (a Key Value claim on the nonce); the PKCE verifier, when there is one, is kept in Key
Value under the same nonce so it never travels through the browser."""

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass
from typing import Any

STATE_TTL = 600


class StateError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class FlowState:
    org_id: str
    membership_id: str
    integration: str
    scope: str  # org | user
    nonce: str


def make_state(
    secret: str, org_id: Any, membership_id: Any, integration: str, scope: str, now: float | None = None
) -> tuple[str, str]:
    """Returns (state, nonce)."""
    nonce = secrets.token_urlsafe(16)
    payload = {
        "o": str(org_id),
        "m": str(membership_id),
        "i": integration,
        "s": scope,
        "n": nonce,
        "e": int((now or time.time()) + STATE_TTL),
    }
    body = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode().rstrip("=")
    signature = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{signature}", nonce


def read_state(secret: str, state: str, now: float | None = None) -> FlowState:
    body, _, signature = state.partition(".")
    expected = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()
    if not body or not hmac.compare_digest(signature, expected):
        raise StateError("bad_state")
    try:
        payload: dict[str, Any] = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    except ValueError as exc:
        raise StateError("bad_state") from exc
    if int(payload["e"]) < (now or time.time()):
        raise StateError("expired_state")
    return FlowState(payload["o"], payload["m"], payload["i"], payload["s"], payload["n"])
