"""Temporal worker process (``app worker``)."""

import asyncio
import logging
import signal
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any

from temporalio.client import Client
from temporalio.worker import Worker

from app import kv
from app.agent.runner import AgentDeps, AgentRunner
from app.crypto import Envelope, LocalKeyProvider
from app.db.pool import open_pool
from app.llm.gateway import gateway_from_settings
from app.settings import Settings
from app.slack.client import SlackClient
from app.slack.limiter import SlackRateLimiter
from app.temporal import connect_temporal
from app.vault import Vault
from app.workflows.ping import PingWorkflow, ping
from app.workflows.slack import AgentRequestWorkflow, PublishHomeWorkflow, SyncWorkspaceWorkflow
from app.workflows.slack_activities import SlackActivities, SlackDeps
from app.workflows.task import TaskWorkflow
from app.workflows.task_activities import TaskActivities

log = logging.getLogger(__name__)


async def build_slack_activities(
    settings: Settings, closers: list[Callable[[], Awaitable[None]]]
) -> SlackActivities | None:
    """Slack activities need the worker's and the vault's database users and the encryption keys."""
    if not (
        settings.database_url
        and settings.db_worker_password
        and settings.db_vault_password
        and settings.encryption_keys
        and settings.encryption_active_key
    ):
        log.warning("database, vault or encryption not configured: Slack activities disabled")
        return None
    pool = await open_pool(
        settings.component_dsn("worker"), "worker", min_size=1, max_size=settings.db_pool_max_size
    )
    vault_pool = await open_pool(settings.component_dsn("vault"), "vault", min_size=0, max_size=4)
    closers += [pool.close, vault_pool.close]
    kv_client: Any = kv.make_client(settings.redis_url.get_secret_value()) if settings.redis_url else None
    if kv_client is not None:
        closers.append(kv_client.aclose)
    slack = SlackClient(
        settings.slack_api_url, limiter=SlackRateLimiter(kv_client) if kv_client is not None else None
    )
    closers.append(slack.aclose)
    envelope = Envelope(
        LocalKeyProvider.from_config(
            settings.encryption_keys.get_secret_value(), settings.encryption_active_key
        )
    )
    vault = Vault(vault_pool, envelope)
    gateway = gateway_from_settings(settings, pool, kv_client)
    tasks = None
    if gateway is None:
        log.warning("no model provider key (ANTHROPIC_API_KEY): the agent only acknowledges requests")
    else:
        closers.append(gateway.aclose)
        tasks = TaskActivities(
            AgentRunner(
                AgentDeps(
                    pool=pool,
                    vault=vault,
                    slack=slack,
                    kv=kv_client,
                    gateway=gateway,
                    console_url=settings.console_url,
                    approval_ttl_hours=settings.approval_ttl_hours,
                )
            )
        )
    return SlackActivities(
        SlackDeps(
            pool=pool,
            vault=vault,
            slack=slack,
            kv=kv_client,
            console_url=settings.console_url,
            agent_enabled=tasks is not None,
            task_token_budget=settings.task_token_budget,
        ),
        tasks,
    )


def build_worker(client: Client, settings: Settings, slack: SlackActivities | None = None) -> Worker:
    workflows: list[type] = [PingWorkflow]
    activities: list[Callable[..., Any]] = [ping]
    if slack is not None:
        workflows += [AgentRequestWorkflow, SyncWorkspaceWorkflow, PublishHomeWorkflow]
        activities += [
            slack.identify_requester,
            slack.request_access,
            slack.check_channel,
            slack.explain_refusal,
            slack.acknowledge,
            slack.create_task,
            slack.sync_workspace,
            slack.publish_home,
        ]
        if slack.tasks is not None:
            workflows.append(TaskWorkflow)
            activities += slack.tasks.all()
    return Worker(
        client,
        task_queue=settings.temporal_task_queue,
        workflows=workflows,
        activities=activities,
        graceful_shutdown_timeout=timedelta(seconds=settings.worker_graceful_shutdown_seconds),
    )


async def run_worker(settings: Settings) -> int:
    from app.telemetry import configure_tracing, shutdown_tracing

    configure_tracing("agent-worker", settings.otel_exporter_otlp_endpoint, settings.env)
    client = await connect_temporal(settings)
    closers: list[Callable[[], Awaitable[None]]] = []
    worker = build_worker(client, settings, await build_slack_activities(settings, closers))

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    try:
        async with worker:  # leaving the block stops polling and waits for in-flight activities
            log.info(
                "worker polling task queue %r on %s (namespace %s)",
                settings.temporal_task_queue,
                settings.temporal_address,
                settings.temporal_namespace,
            )
            await stop.wait()
            log.info("shutdown requested; draining in-flight activities")
    finally:
        for close in reversed(closers):
            await close()
    shutdown_tracing()
    log.info("worker stopped")
    return 0
