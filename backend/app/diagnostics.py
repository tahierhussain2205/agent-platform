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
