"""Which vault a process uses: the vault service when ``VAULT_URL`` is set (the API and worker on
Render and in Compose), otherwise the vault in process (needs the vault database user)."""

from collections.abc import Awaitable, Callable

from app.settings import Settings
from app.vault_service import LocalVault, VaultClient


async def vault_from_settings(
    settings: Settings, closers: list[Callable[[], Awaitable[None]]]
) -> VaultClient | None:
    if settings.vault_url and settings.vault_service_token:
        from app.vault_service.client import RemoteVault

        remote = RemoteVault(settings.vault_url, settings.vault_service_token.get_secret_value())
        closers.append(remote.aclose)
        return remote
    if not (
        settings.database_url
        and settings.db_vault_password
        and settings.encryption_keys
        and settings.encryption_active_key
    ):
        return None
    from app.crypto import Envelope, LocalKeyProvider
    from app.db.pool import open_pool
    from app.integrations.providers import providers

    pool = await open_pool(settings.component_dsn("vault"), "vault", min_size=0, max_size=4)
    closers.append(pool.close)
    keys = LocalKeyProvider.from_config(
        settings.encryption_keys.get_secret_value(), settings.encryption_active_key
    )
    local = LocalVault(pool, Envelope(keys), providers(settings))
    closers.append(local.aclose)
    return local
