"""Connectivity checks (``python -m app check <target>``), used by smoke tests and operators."""

import uuid
from datetime import timedelta
from typing import Any, cast

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


# Objects written by `app check storage` live under this made up tenant, never a real org's prefix.
CHECK_TENANT = uuid.UUID("00000000-0000-0000-0000-00000000c0de")


async def check_storage(settings: Settings) -> str:
    """Write, read and delete one small object in the configured bucket with this process's own
    credentials (under a tenant prefix no org has)."""
    from app.storage import ObjectNotFoundError, store_from_settings

    store = store_from_settings(settings)
    if store is None:
        raise RuntimeError("no object storage configured (S3_BUCKET is not set)")
    body = f"check {uuid.uuid4()}".encode()
    ref = await store.put(CHECK_TENANT, "check", body)
    if await store.get(CHECK_TENANT, ref) != body:
        raise RuntimeError("read back something other than what was written")
    await store.delete(CHECK_TENANT, ref)
    try:
        await store.get(CHECK_TENANT, ref)
    except ObjectNotFoundError:
        return f"ok: wrote, read and deleted an object in {settings.s3_bucket}"
    raise RuntimeError("the object was still there after deleting it")


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


async def check_model(settings: Settings) -> str:
    """One small request per tier, shaped like the agent's steps (a cached system prompt and a forced
    ``submit_*`` tool): the key works and each tier's model answers the way the agent needs. No
    budgets or metering: this is an operator check, not tenant work."""
    from opentelemetry import trace

    from app.llm.gateway import LLMError, gateway_from_settings

    gateway = gateway_from_settings(settings, cast(Any, None), None)
    if gateway is None:
        raise RuntimeError("ANTHROPIC_API_KEY is not set")
    tool = {
        "name": "submit_check",
        "description": "Submit the answer.",
        "input_schema": {
            "type": "object",
            "properties": {"answer": {"type": "string"}},
            "required": ["answer"],
        },
    }
    lines = [f"model provider: {settings.anthropic_api_url}"]
    failed = 0
    try:
        for tier, model in gateway.tier_models.items():
            system = "You are a connectivity check. Answer with the submit_check tool."
            messages = [{"role": "user", "content": "Say ok."}]
            forced = {"type": "tool", "name": "submit_check"}
            body = gateway._body(model, system, messages, [tool], forced, 200, True)
            try:
                payload = await gateway._send(body, trace.get_current_span())
            except LLMError as exc:
                failed += 1
                lines.append(f"{tier} ({model}): FAILED {exc}")
                continue
            called = any(
                b.get("type") == "tool_use" and b.get("name") == "submit_check"
                for b in payload.get("content") or []
            )
            usage = payload.get("usage") or {}
            if called:
                tokens = f"{usage.get('input_tokens')} in, {usage.get('output_tokens')} out"
                lines.append(f"{tier} ({model}): ok, answered as {payload.get('model')} ({tokens})")
            else:
                failed += 1
                stop = payload.get("stop_reason")
                lines.append(f"{tier} ({model}): FAILED no submit_check call (stop_reason {stop})")
    finally:
        await gateway.aclose()
    report = "\n".join(lines)
    if failed:
        raise RuntimeError(f"{failed} of {len(gateway.tier_models)} tiers failed\n{report}")
    return report
