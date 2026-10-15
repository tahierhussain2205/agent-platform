"""Token vault: the only code path that reads or writes connection secrets (spec 9.4).

Runs as ``vault_service`` (role ``app_vault``, the only role with privileges on
``connection_secrets``), inside a tenant transaction, and envelope encrypts every secret bound to
its org and connection. Decrypted tokens never go to logs or model context; callers hold them
only for the API call that needs them.
"""

import time
from uuid import UUID

from psycopg_pool import AsyncConnectionPool

from app.crypto import Envelope
from app.db.tenant import tenant_tx


class SecretNotFoundError(LookupError):
    pass


def _context(connection_id: UUID | str) -> str:
    return f"connection_secrets:{connection_id}"


class Vault:
    def __init__(self, pool: AsyncConnectionPool, envelope: Envelope, cache_seconds: float = 60.0) -> None:
        self.pool = pool
        self.envelope = envelope
        self.cache_seconds = cache_seconds
        self._cache: dict[tuple[str, str], tuple[float, str]] = {}

    async def store(self, org_id: UUID | str, connection_id: UUID | str, secret: str) -> None:
        sealed = self.envelope.seal(UUID(str(org_id)), secret.encode(), _context(connection_id))
        async with tenant_tx(self.pool, org_id) as conn:
            await conn.execute(
                """
                insert into app.connection_secrets (connection_id, org_id, ciphertext, kms_key_id)
                values (%s, %s, %s, %s)
                on conflict (connection_id) do update
                  set ciphertext = excluded.ciphertext, kms_key_id = excluded.kms_key_id, encrypted_at = now()
                """,
                (connection_id, org_id, sealed.ciphertext, sealed.kms_key_id),
            )
        self._cache.pop((str(org_id), str(connection_id)), None)

    async def read(self, org_id: UUID | str, connection_id: UUID | str) -> str:
        key = (str(org_id), str(connection_id))
        cached = self._cache.get(key)
        if cached and cached[0] > time.monotonic():
            return cached[1]
        async with tenant_tx(self.pool, org_id) as conn:
            row = await (
                await conn.execute(
                    "select ciphertext, kms_key_id from app.connection_secrets where connection_id = %s",
                    (connection_id,),
                )
            ).fetchone()
        if row is None:
            raise SecretNotFoundError(str(connection_id))
        secret = self.envelope.open(
            UUID(str(org_id)), bytes(row[0]), row[1], _context(connection_id)
        ).decode()
        self._cache[key] = (time.monotonic() + self.cache_seconds, secret)
        return secret

    async def delete(self, org_id: UUID | str, connection_id: UUID | str) -> None:
        async with tenant_tx(self.pool, org_id) as conn:
            await conn.execute(
                "delete from app.connection_secrets where connection_id = %s", (connection_id,)
            )
        self._cache.pop((str(org_id), str(connection_id)), None)
