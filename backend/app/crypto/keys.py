"""Key providers. A provider wraps and unwraps data keys with a per tenant key encryption key.

``LocalKeyProvider`` derives each tenant's key from a master key (a Render secret) with HKDF, so
every tenant has its own key without storing one. Key versions allow rotation: new data keys use
the active version, old ciphertexts keep naming the version that wrapped them. A cloud KMS
provider (one customer managed key per tenant) can implement the same interface later.
"""

import base64
import binascii
import os
import re
from typing import Protocol
from uuid import UUID

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

_KEY_ID = re.compile(r"^[a-z0-9]{1,16}$")
_NONCE = 12


class KeyConfigError(ValueError):
    pass


class KeyProvider(Protocol):
    name: str

    def active_key_id(self) -> str: ...

    def wrap(self, org_id: UUID, key_id: str, data_key: bytes) -> bytes: ...

    def unwrap(self, org_id: UUID, key_id: str, wrapped: bytes) -> bytes: ...


class LocalKeyProvider:
    name = "local"

    def __init__(self, keys: dict[str, bytes], active: str) -> None:
        if not keys:
            raise KeyConfigError("no encryption keys configured")
        for key_id, key in keys.items():
            if not _KEY_ID.match(key_id):
                raise KeyConfigError(f"bad key id {key_id!r} (1-16 lowercase letters or digits)")
            if len(key) != 32:
                raise KeyConfigError(f"key {key_id} must be 32 bytes")
        if active not in keys:
            raise KeyConfigError(f"active key {active!r} is not among the configured keys")
        self._keys = dict(keys)
        self._active = active

    @classmethod
    def from_config(cls, spec: str, active: str) -> "LocalKeyProvider":
        """``spec`` is ``id:base64key,id:base64key``."""
        keys: dict[str, bytes] = {}
        for item in filter(None, (part.strip() for part in spec.split(","))):
            key_id, sep, encoded = item.partition(":")
            if not sep:
                raise KeyConfigError("each key must look like id:base64")
            try:
                keys[key_id] = base64.b64decode(encoded, validate=True)
            except binascii.Error as exc:
                raise KeyConfigError(f"key {key_id} is not valid base64") from exc
        return cls(keys, active)

    @staticmethod
    def generate_key() -> str:
        return base64.b64encode(os.urandom(32)).decode()

    def active_key_id(self) -> str:
        return self._active

    def _tenant_kek(self, org_id: UUID, key_id: str) -> bytes:
        master = self._keys.get(key_id)
        if master is None:
            raise KeyConfigError(f"unknown key version {key_id!r}")
        hkdf = HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=b"agent-platform/tenant-kek",
            info=org_id.bytes + key_id.encode(),
        )
        return hkdf.derive(master)

    def _aad(self, org_id: UUID, key_id: str) -> bytes:
        return b"ap-dek:v1|" + org_id.bytes + b"|" + key_id.encode()

    def wrap(self, org_id: UUID, key_id: str, data_key: bytes) -> bytes:
        nonce = os.urandom(_NONCE)
        return nonce + AESGCM(self._tenant_kek(org_id, key_id)).encrypt(
            nonce, data_key, self._aad(org_id, key_id)
        )

    def unwrap(self, org_id: UUID, key_id: str, wrapped: bytes) -> bytes:
        nonce, body = wrapped[:_NONCE], wrapped[_NONCE:]
        return AESGCM(self._tenant_kek(org_id, key_id)).decrypt(nonce, body, self._aad(org_id, key_id))
