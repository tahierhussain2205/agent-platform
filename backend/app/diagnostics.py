"""Connectivity checks (``python -m app check <target>``), used by smoke tests and operators."""

import uuid
from datetime import timedelta

from app.settings import Settings
from app.temporal import connect_temporal
from app.workflows.ping import PingWorkflow


async def check_temporal(settings: Settings, timeout_seconds: float = 20.0) -> str:
    """Run PingWorkflow end to end; succeeds only if a worker is polling the task queue."""
    client = await connect_temporal(settings, attempts=3)
    result: str = await client.execute_workflow(
        PingWorkflow.run,
        "smoke",
        id=f"ping-{uuid.uuid4()}",
        task_queue=settings.temporal_task_queue,
        execution_timeout=timedelta(seconds=timeout_seconds),
    )
    return result


async def check_vault(settings: Settings) -> str:
    """Call the vault service with the service token; it answers once it reaches its database."""
    from app.vault_service.client import RemoteVault

    if not (settings.vault_url and settings.vault_service_token):
        raise RuntimeError("VAULT_URL and VAULT_SERVICE_TOKEN are not set (the vault runs in process)")
    vault = RemoteVault(settings.vault_url, settings.vault_service_token.get_secret_value())
    try:
        return f"vault: {await vault.ready()}"
    finally:
        await vault.aclose()
