"""Temporal worker process (``python -m app worker``)."""

import asyncio
import logging
import signal
from datetime import timedelta

from temporalio.client import Client
from temporalio.worker import Worker

from app.settings import Settings
from app.temporal import connect_temporal
from app.workflows.ping import PingWorkflow, ping

log = logging.getLogger(__name__)


def build_worker(client: Client, settings: Settings) -> Worker:
    return Worker(
        client,
        task_queue=settings.temporal_task_queue,
        workflows=[PingWorkflow],
        activities=[ping],
        graceful_shutdown_timeout=timedelta(seconds=settings.worker_graceful_shutdown_seconds),
    )


async def run_worker(settings: Settings) -> int:
    from app.telemetry import configure_tracing, shutdown_tracing

    configure_tracing("agent-worker", settings.otel_exporter_otlp_endpoint, settings.env)
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
    shutdown_tracing()
    log.info("worker stopped")
    return 0
