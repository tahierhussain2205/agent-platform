"""Temporal worker process (``python -m app worker``)."""

import asyncio
import logging
import signal
from datetime import timedelta

from temporalio.client import Client
from temporalio.worker import Worker

from app.settings import Settings
from app.workflows.ping import PingWorkflow, ping

log = logging.getLogger(__name__)


async def connect_temporal(settings: Settings, attempts: int = 30, delay: float = 1.0) -> Client:
    """Connect with retries: in Compose (and on restarts) Temporal may come up after the worker."""
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            api_key = settings.temporal_api_key.get_secret_value() if settings.temporal_api_key else None
            return await Client.connect(
                settings.temporal_address,
                namespace=settings.temporal_namespace,
                api_key=api_key,
                tls=api_key is not None,
            )
        except Exception as exc:  # the SDK raises RuntimeError subclasses for connection failures
            last_error = exc
            log.warning("temporal not reachable (attempt %d/%d): %s", attempt, attempts, exc)
            await asyncio.sleep(min(delay * attempt, 5.0))
    raise RuntimeError(f"could not connect to Temporal at {settings.temporal_address}") from last_error


def build_worker(client: Client, settings: Settings) -> Worker:
    return Worker(
        client,
        task_queue=settings.temporal_task_queue,
        workflows=[PingWorkflow],
        activities=[ping],
        graceful_shutdown_timeout=timedelta(seconds=settings.worker_graceful_shutdown_seconds),
    )


async def run_worker(settings: Settings) -> int:
    client = await connect_temporal(settings)
    worker = build_worker(client, settings)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    async with worker:  # leaving the block stops polling and waits for in-flight activities
        log.info(
            "worker polling task queue %r on %s (namespace %s)",
            settings.temporal_task_queue,
            settings.temporal_address,
            settings.temporal_namespace,
        )
        await stop.wait()
        log.info("shutdown requested; draining in-flight activities")
    log.info("worker stopped")
    return 0
