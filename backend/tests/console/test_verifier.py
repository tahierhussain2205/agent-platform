"""Clerk session token verification (spec 14.3)."""

import base64
import hashlib
import hmac
import json
import time
from collections.abc import Mapping

import httpx
import pytest
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from app.auth.clerk import AuthError, ClerkVerifier
from tests.console.conftest import ISSUER, ORIGIN, Jwks, SigningKey, mint


def verifier(jwks: Jwks, refresh_interval: float = 10.0, clock: list[float] | None = None) -> ClerkVerifier:
    now = clock if clock is not None else [0.0]
    return ClerkVerifier(
        ISSUER, "https://jwks.test/.well-known/jwks.json", [ORIGIN],
        http=httpx.AsyncClient(transport=httpx.MockTransport(jwks.handler)),
        refresh_interval=refresh_interval, clock=lambda: now[0],
    )  # fmt: skip


async def expect(code: str, status: int, v: ClerkVerifier, token: str) -> None:
    with pytest.raises(AuthError) as info:
        await v.verify(token)
    assert (info.value.code, info.value.status) == (code, status)


async def test_valid_v2_token(signing_key: SigningKey, jwks: Jwks) -> None:
    session = await verifier(jwks).verify(mint(signing_key, "user_alice", "org_acme", rol="admin"))
    assert (session.clerk_user_id, session.clerk_org_id, session.org_role) == (
        "user_alice",
        "org_acme",
        "admin",
    )
    assert session.session_id.startswith("sess_")


async def test_v1_org_claim_fallback(signing_key: SigningKey, jwks: Jwks) -> None:
    session = await verifier(jwks).verify(mint(signing_key, "user_alice", "org_acme", version=1))
    assert session.clerk_org_id == "org_acme"
    assert session.org_role == "org:member"


async def test_no_active_org(signing_key: SigningKey, jwks: Jwks) -> None:
    assert (await verifier(jwks).verify(mint(signing_key, "user_alice", None))).clerk_org_id is None


async def test_keys_are_cached(signing_key: SigningKey, jwks: Jwks) -> None:
    v = verifier(jwks)
    for _ in range(5):
        await v.verify(mint(signing_key, "user_alice", "org_acme"))
    assert jwks.fetches == 1


async def test_tampered_signature(signing_key: SigningKey, jwks: Jwks) -> None:
    header, payload, sig = mint(signing_key, "user_alice", "org_acme").split(".")
    forged = json.loads(base64.urlsafe_b64decode(payload + "=="))
    forged["sub"] = "user_dave"
    payload = base64.urlsafe_b64encode(json.dumps(forged).encode()).decode().rstrip("=")
    await expect("invalid_token", 401, verifier(jwks), f"{header}.{payload}.{sig}")


async def test_token_signed_by_another_key_with_same_kid(signing_key: SigningKey, jwks: Jwks) -> None:
    impostor = SigningKey(kid=signing_key.kid)
    await expect("invalid_token", 401, verifier(jwks), mint(impostor, "user_alice", "org_acme"))


async def test_key_rotation_is_picked_up(signing_key: SigningKey, jwks: Jwks) -> None:
    clock = [0.0]
    v = verifier(jwks, clock=clock)
    await v.verify(mint(signing_key, "user_alice", "org_acme"))
    new_key = SigningKey()
    jwks.keys.append(new_key)
    clock[0] = 11.0  # past the refresh interval
    await v.verify(mint(new_key, "user_alice", "org_acme"))
    assert jwks.fetches == 2


async def test_unknown_kid_refresh_is_rate_limited(signing_key: SigningKey, jwks: Jwks) -> None:
    clock = [0.0]
    v = verifier(jwks, clock=clock)
    await v.verify(mint(signing_key, "user_alice", "org_acme"))
    stranger = SigningKey()
    for _ in range(3):
        await expect("invalid_token", 401, v, mint(stranger, "user_alice", "org_acme"))
    assert jwks.fetches == 1  # no refetch within the interval, however many bad tokens arrive


async def test_expired_and_not_yet_valid(signing_key: SigningKey, jwks: Jwks) -> None:
    v = verifier(jwks)
    await expect("token_expired", 401, v, mint(signing_key, "user_alice", "org_acme", exp_in=-30))
    await expect("invalid_token", 401, v, mint(signing_key, "user_alice", "org_acme", nbf_in=60))
    await v.verify(mint(signing_key, "user_alice", "org_acme", exp_in=-2))  # within the 5 s leeway


@pytest.mark.parametrize(
    "kwargs",
    [
        {"iss": "https://evil.example"},
        {"azp": "https://evil.example"},
        {"azp": None},
        {"drop": ("sid",)},
        {"drop": ("exp",)},
        {"drop": ("sub",)},
    ],
    ids=["wrong_issuer", "wrong_azp", "missing_azp", "missing_sid", "missing_exp", "missing_sub"],
)
async def test_claim_checks(signing_key: SigningKey, jwks: Jwks, kwargs: dict[str, object]) -> None:
    await expect("invalid_token", 401, verifier(jwks), mint(signing_key, "user_alice", "org_acme", **kwargs))  # type: ignore[arg-type]


async def test_pending_session_is_refused(signing_key: SigningKey, jwks: Jwks) -> None:
    await expect("session_pending", 403, verifier(jwks), mint(signing_key, "user_new", None, sts="pending"))


def _raw_token(header: Mapping[str, object], claims: Mapping[str, object], signature: bytes) -> str:
    def b64(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).decode().rstrip("=")

    return f"{b64(json.dumps(header).encode())}.{b64(json.dumps(claims).encode())}.{b64(signature)}"


async def test_algorithm_confusion_is_refused(signing_key: SigningKey, jwks: Jwks) -> None:
    now = int(time.time())
    claims = {
        "sub": "user_alice",
        "sid": "s",
        "iss": ISSUER,
        "iat": now,
        "nbf": now,
        "exp": now + 60,
        "azp": ORIGIN,
        "o": {"id": "org_acme"},
    }
    public_pem = signing_key.private.public_key().public_bytes(
        Encoding.PEM, PublicFormat.SubjectPublicKeyInfo
    )
    # HS256 "signed" with the public key as the HMAC secret: the classic RS256/HS256 confusion.
    hs_header = {"alg": "HS256", "kid": signing_key.kid, "typ": "JWT"}
    unsigned_part = _raw_token(hs_header, claims, b"").rsplit(".", 1)[0]
    hs_sig = hmac.new(public_pem, unsigned_part.encode(), hashlib.sha256).digest()
    hs256 = f"{unsigned_part}.{base64.urlsafe_b64encode(hs_sig).decode().rstrip('=')}"
    unsigned = _raw_token({"alg": "none", "kid": signing_key.kid}, claims, b"")
    v = verifier(jwks)
    for token in (hs256, unsigned):
        await expect("invalid_token", 401, v, token)


@pytest.mark.parametrize("token", ["", "not.a.jwt", "a.b", "eyJhbGciOiJSUzI1NiJ9.e30.sig"])
async def test_garbage_tokens(jwks: Jwks, token: str) -> None:
    await expect("invalid_token", 401, verifier(jwks), token)


async def test_jwks_outage(signing_key: SigningKey, jwks: Jwks) -> None:
    jwks.down = True
    await expect("auth_unavailable", 503, verifier(jwks), mint(signing_key, "user_alice", "org_acme"))


async def test_cached_keys_survive_a_later_outage(signing_key: SigningKey, jwks: Jwks) -> None:
    clock = [0.0]
    v = verifier(jwks, clock=clock)
    await v.verify(mint(signing_key, "user_alice", "org_acme"))
    jwks.down = True
    clock[0] = 100.0
    await v.verify(mint(signing_key, "user_alice", "org_acme"))  # known kid: no fetch needed
