"""The API's Temporal client, connected on first use and kept on the app's resources."""

import logging

from fastapi import Request
from temporalio.client import Client

from app.auth.console import resources
from app.settings import get_settings
from app.temporal import connect_temporal

log = logging.getLogger(__name__)


async def temporal_client(request: Request) -> Client | None:
    """None when Temporal cannot be reached: callers that start best effort follow ups (schedule
    syncs, a removal's follow up) carry on; the maintenance job catches up."""
    res = resources(request)
    if res.temporal is None:
        try:
            res.temporal = await connect_temporal(get_settings(), attempts=2, delay=0.2)
        except Exception:
            log.warning("Temporal is not reachable", exc_info=True)
            return None
    client: Client = res.temporal
    return client
