"""Envelope encryption: per tenant keys, binding to org and row, tamper detection, rotation."""

import base64
import os
import uuid

import pytest
from cryptography.exceptions import InvalidTag

from app.crypto import DecryptionError, Envelope, KeyConfigError, LocalKeyProvider

K1, K2 = base64.b64encode(os.urandom(32)).decode(), base64.b64encode(os.urandom(32)).decode()
ORG_A, ORG_B = uuid.uuid4(), uuid.uuid4()
CTX = "connection_secrets:0192f4a0-0000-7000-8000-000000000001"


def envelope(spec: str = f"v1:{K1}", active: str = "v1") -> Envelope:
    return Envelope(LocalKeyProvider.from_config(spec, active))


def test_round_trip() -> None:
    env = envelope()
    sealed = env.seal(ORG_A, b"oauth-refresh-token", CTX)
    assert sealed.kms_key_id == "local:v1"
    assert b"oauth-refresh-token" not in sealed.ciphertext
    assert env.open(ORG_A, sealed.ciphertext, sealed.kms_key_id, CTX) == b"oauth-refresh-token"


def test_same_plaintext_encrypts_differently_each_time() -> None:
    env = envelope()
    assert env.seal(ORG_A, b"x", CTX).ciphertext != env.seal(ORG_A, b"x", CTX).ciphertext


@pytest.mark.parametrize(
    ("org", "context"),
    [(ORG_B, CTX), (ORG_A, "connection_secrets:another-row"), (ORG_A, "webhook_secret:" + CTX.split(":")[1])],
    ids=["other_org", "other_row", "other_purpose"],
)
def test_ciphertext_is_bound_to_org_row_and_purpose(org: uuid.UUID, context: str) -> None:
    env = envelope()
    sealed = env.seal(ORG_A, b"secret", CTX)
    with pytest.raises(DecryptionError):
        env.open(org, sealed.ciphertext, sealed.kms_key_id, context)


@pytest.mark.parametrize("position", [0, 5, 40, -1])
def test_tampering_is_detected(position: int) -> None:
    env = envelope()
    sealed = env.seal(ORG_A, b"secret", CTX)
    blob = bytearray(sealed.ciphertext)
    blob[position] ^= 0x01
    with pytest.raises(DecryptionError):
        env.open(ORG_A, bytes(blob), sealed.kms_key_id, CTX)


@pytest.mark.parametrize("blob", [b"", b"\x01", b"\x01\x00\xff" + b"x" * 10, b"\x09" + b"x" * 100])
def test_truncated_or_unknown_format_fails_cleanly(blob: bytes) -> None:
    with pytest.raises(DecryptionError):
        envelope().open(ORG_A, blob, "local:v1", CTX)


def test_errors_reveal_nothing() -> None:
    env = envelope()
    sealed = env.seal(ORG_A, b"secret", CTX)
    with pytest.raises(DecryptionError) as info:
        env.open(ORG_B, sealed.ciphertext, sealed.kms_key_id, CTX)
    assert str(info.value) == "could not decrypt the secret"


def test_rotation_old_versions_still_open_and_rewrap_moves_to_active() -> None:
    old = envelope(f"v1:{K1}", "v1")
    sealed = old.seal(ORG_A, b"secret", CTX)
    rotated = envelope(f"v1:{K1},v2:{K2}", "v2")
    assert rotated.open(ORG_A, sealed.ciphertext, sealed.kms_key_id, CTX) == b"secret"
    moved = rotated.rewrap(ORG_A, sealed.ciphertext, sealed.kms_key_id)
    assert moved.kms_key_id == "local:v2"
    only_v2 = envelope(f"v2:{K2}", "v2")
    assert only_v2.open(ORG_A, moved.ciphertext, moved.kms_key_id, CTX) == b"secret"
    with pytest.raises(DecryptionError):
        only_v2.open(ORG_A, sealed.ciphertext, sealed.kms_key_id, CTX)  # v1 retired


def test_tenants_get_different_wrapping_keys() -> None:
    provider = LocalKeyProvider.from_config(f"v1:{K1}", "v1")
    dek = os.urandom(32)
    wrapped_a = provider.wrap(ORG_A, "v1", dek)
    with pytest.raises(InvalidTag):  # org B's key cannot unwrap org A's data key
        provider.unwrap(ORG_B, "v1", wrapped_a)


def test_wrong_provider_name_in_key_id_fails() -> None:
    env = envelope()
    sealed = env.seal(ORG_A, b"secret", CTX)
    with pytest.raises(DecryptionError):
        env.open(ORG_A, sealed.ciphertext, "awskms:v1", CTX)


@pytest.mark.parametrize(
    ("spec", "active"),
    [("", "v1"), (f"v1:{K1}", "v2"), ("v1:not-base64!", "v1"), (f"V1:{K1}", "V1"),
     (f"v1:{base64.b64encode(b'short').decode()}", "v1"), ("v1", "v1")],
)  # fmt: skip
def test_bad_key_configuration_is_rejected(spec: str, active: str) -> None:
    with pytest.raises(KeyConfigError):
        LocalKeyProvider.from_config(spec, active)


def test_generated_keys_are_valid() -> None:
    key = LocalKeyProvider.generate_key()
    assert len(base64.b64decode(key)) == 32
    envelope(f"v1:{key}").seal(ORG_A, b"x", CTX)
