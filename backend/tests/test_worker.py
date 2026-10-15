import asyncio
import json
import os
import signal
import socket
import subprocess
import sys
import time
import uuid

import pytest
from temporalio.client import WorkflowFailureError
from temporalio.exceptions import TimeoutError

from app.diagnostics import check_temporal
from app.settings import Settings
from app.temporal import connect_temporal
from app.worker import build_worker

TEMPORAL = os.environ.get("TEST_TEMPORAL_ADDRESS", "localhost:7233")


def _reachable(address: str) -> bool:
    host, _, port = address.rpartition(":")
    try:
        with socket.create_connection((host, int(port)), timeout=1):
            return True
    except OSError:
        return False


@pytest.fixture(scope="module")
def temporal_settings() -> Settings:
    if not _reachable(TEMPORAL):
        pytest.skip(f"Temporal not reachable at {TEMPORAL} (run `make infra`)")
    return Settings(temporal_address=TEMPORAL, temporal_task_queue=f"test-{uuid.uuid4().hex[:8]}")


async def test_ping_workflow_round_trip(temporal_settings: Settings) -> None:
    client = await connect_temporal(temporal_settings)
    async with build_worker(client, temporal_settings):
        assert await check_temporal(temporal_settings) == "pong: smoke"


async def test_check_fails_when_no_worker_polls(temporal_settings: Settings) -> None:
    lonely = temporal_settings.model_copy(update={"temporal_task_queue": f"nobody-{uuid.uuid4().hex[:8]}"})
    with pytest.raises(WorkflowFailureError) as info:
        await check_temporal(lonely, timeout_seconds=2)
    assert isinstance(info.value.cause, TimeoutError)


async def test_connect_gives_up_after_retries() -> None:
    settings = Settings(temporal_address="127.0.0.1:1")
    started = time.monotonic()
    with pytest.raises(RuntimeError, match="could not connect"):
        await connect_temporal(settings, attempts=2, delay=0.1)
    assert time.monotonic() - started < 10


def test_worker_process_stops_cleanly_on_sigterm(temporal_settings: Settings) -> None:
    env = {
        **os.environ,
        "TEMPORAL_ADDRESS": temporal_settings.temporal_address,
        "TEMPORAL_TASK_QUEUE": temporal_settings.temporal_task_queue,
        "WORKER_GRACEFUL_SHUTDOWN_SECONDS": "5",
    }
    # Logs are JSON lines on stdout; stderr is merged so nothing is missed.
    proc = subprocess.Popen(
        [sys.executable, "-m", "app", "worker"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        assert proc.stdout is not None
        deadline = time.monotonic() + 30
        lines: list[str] = []
        while time.monotonic() < deadline:
            line = proc.stdout.readline()
            lines.append(line)
            if "worker polling" in line:
                break
        else:
            pytest.fail(f"worker did not start: {''.join(lines)}")
        # The worker answers a ping while running.
        assert asyncio.run(check_temporal(temporal_settings)) == "pong: smoke"
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=20) == 0
        rest = proc.stdout.read()
        assert "worker stopped" in rest
        assert all(
            json.loads(line)["service"] == "agent-worker" for line in rest.splitlines() if line.strip()
        )
    finally:
        if proc.poll() is None:
            proc.kill()
