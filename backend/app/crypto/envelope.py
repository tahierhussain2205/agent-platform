"""Envelope encryption.

Each secret gets a fresh 256 bit data key. The payload is encrypted with AES-256-GCM under the
data key; the data key is wrapped by the tenant's key encryption key (``KeyProvider``). Associated
data binds the payload to its org and a caller supplied context (for example
``connection_secrets:<connection id>``), so a ciphertext copied to another row or tenant does not
decrypt.

Blob layout (stored in ``connection_secrets.ciphertext``):
``version (1) | wrapped key length (2, big endian) | wrapped key | nonce (12) | ciphertext+tag``
"""

import os
import struct
from dataclasses import dataclass
from uuid import UUID

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.crypto.keys import KeyConfigError, KeyProvider

_VERSION = 1
_NONCE = 12


class DecryptionError(Exception):
    """Deliberately vague: never says which part failed or why."""

    def __init__(self) -> None:
        super().__init__("could not decrypt the secret")


@dataclass(frozen=True)
class Sealed:
    ciphertext: bytes
    kms_key_id: str  # "<provider>:<key version>"


class Envelope:
    def __init__(self, provider: KeyProvider) -> None:
        self.provider = provider

    @staticmethod
    def _aad(org_id: UUID, context: str) -> bytes:
        return b"ap-secret:v1|" + org_id.bytes + b"|" + context.encode()

    def seal(self, org_id: UUID, plaintext: bytes, context: str) -> Sealed:
        key_id = self.provider.active_key_id()
        data_key = AESGCM.generate_key(bit_length=256)
        wrapped = self.provider.wrap(org_id, key_id, data_key)
        nonce = os.urandom(_NONCE)
        body = AESGCM(data_key).encrypt(nonce, plaintext, self._aad(org_id, context))
        blob = struct.pack(">BH", _VERSION, len(wrapped)) + wrapped + nonce + body
        return Sealed(blob, f"{self.provider.name}:{key_id}")

    def _parse(self, blob: bytes, kms_key_id: str) -> tuple[str, bytes, bytes, bytes]:
        provider, _, key_id = kms_key_id.partition(":")
        if provider != self.provider.name or not key_id:
            raise DecryptionError
        if len(blob) < 3:
            raise DecryptionError
        version, wrapped_len = struct.unpack(">BH", blob[:3])
        if version != _VERSION or len(blob) < 3 + wrapped_len + _NONCE + 16:
            raise DecryptionError
        wrapped = blob[3 : 3 + wrapped_len]
        nonce = blob[3 + wrapped_len : 3 + wrapped_len + _NONCE]
        body = blob[3 + wrapped_len + _NONCE :]
        return key_id, wrapped, nonce, body

    def open(self, org_id: UUID, blob: bytes, kms_key_id: str, context: str) -> bytes:
        key_id, wrapped, nonce, body = self._parse(blob, kms_key_id)
        try:
            data_key = self.provider.unwrap(org_id, key_id, wrapped)
            return AESGCM(data_key).decrypt(nonce, body, self._aad(org_id, context))
        except (InvalidTag, KeyConfigError, ValueError) as exc:
            raise DecryptionError from exc

    def rewrap(self, org_id: UUID, blob: bytes, kms_key_id: str) -> Sealed:
        """Re-wrap the data key with the active key version; the payload is untouched."""
        key_id, wrapped, nonce, body = self._parse(blob, kms_key_id)
        try:
            data_key = self.provider.unwrap(org_id, key_id, wrapped)
        except (InvalidTag, KeyConfigError, ValueError) as exc:
            raise DecryptionError from exc
        active = self.provider.active_key_id()
        new_wrapped = self.provider.wrap(org_id, active, data_key)
        new_blob = struct.pack(">BH", _VERSION, len(new_wrapped)) + new_wrapped + nonce + body
        return Sealed(new_blob, f"{self.provider.name}:{active}")
