"""Temporal client factory shared by the API (starting and signalling workflows) and the worker."""

import asyncio
import logging

from temporalio.client import Client
from temporalio.contrib.opentelemetry import TracingInterceptor

from app.settings import Settings

log = logging.getLogger(__name__)


async def connect_temporal(settings: Settings, attempts: int = 30, delay: float = 1.0) -> Client:
    """Connect with retries: in Compose (and on restarts) Temporal may come up after us.
    An API key (Temporal Cloud) switches the connection to TLS."""
    api_key = settings.temporal_api_key.get_secret_value() if settings.temporal_api_key else None
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return await Client.connect(
                settings.temporal_address,
                namespace=settings.temporal_namespace,
                api_key=api_key,
                tls=api_key is not None,
                interceptors=[TracingInterceptor()],
            )
        except Exception as exc:  # the SDK raises RuntimeError subclasses for connection failures
            last_error = exc
            log.warning("temporal not reachable (attempt %d/%d): %s", attempt, attempts, exc)
            await asyncio.sleep(min(delay * attempt, 5.0))
    raise RuntimeError(f"could not connect to Temporal at {settings.temporal_address}") from last_error
