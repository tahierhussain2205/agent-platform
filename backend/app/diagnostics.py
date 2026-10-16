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


def _check_repository() -> bytes:
    """A gzipped tar of a tiny repository (one module, one test) under a top folder, like GitHub's."""
    import io
    import tarfile

    files = {
        "calc.py": b"def add(a, b):\n    return a + b\n",
        "tests/test_calc.py": b"from calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n",
    }
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w:gz") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(f"check-repo-0abc/{name}")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return out.getvalue()


async def check_sandbox(settings: Settings) -> str:
    """One sandbox end to end with the configured provider: upload a tiny repository, commit a
    baseline, run its tests, list, read, search, change a file and diff it; a path may not leave
    the workspace and the internet is out of reach. The sandbox is always destroyed."""
    import time

    import httpx

    from app.agent.recipes import PYTEST
    from app.sandbox import Limits, SandboxError, provider_from_settings
    from app.sandbox.workspaces import BASELINE

    async with httpx.AsyncClient(timeout=60) as http:
        provider = provider_from_settings(settings, http)
        if provider is None:
            raise RuntimeError(
                "no sandbox provider is configured (SANDBOX_API_KEY, SANDBOX_URL or SANDBOX_PROVIDER)"
            )
        started = time.monotonic()
        sandbox = await provider.create(Limits(ttl_seconds=600), {"purpose": "check"})
        try:
            if await provider.upload(sandbox, _check_repository(), "check", strip=1) != 2:
                raise RuntimeError("the archive did not unpack to its two files")
            if not (ran := await provider.exec(sandbox, BASELINE, cwd="check", seconds=60)).ok:
                raise RuntimeError(f"the baseline commit failed: {ran.stderr[-300:]}")
            tests = await provider.exec(sandbox, PYTEST, cwd="check", seconds=120)
            if not tests.ok:
                raise RuntimeError(f"the tests did not pass: {(tests.stdout + tests.stderr)[-300:]}")
            listed = {e["path"] for e in await provider.list_files(sandbox, "check", recursive=True)}
            if not {"check/calc.py", "check/tests/test_calc.py"} <= listed:
                raise RuntimeError("the listing misses the repository's files")
            if "return a + b" not in (await provider.read(sandbox, "check/calc.py"))["content"]:
                raise RuntimeError("reading a file gave something else")
            found = await provider.search(sandbox, "def add", path="check")
            if [m["path"] for m in found["matches"]] != ["check/calc.py"]:
                raise RuntimeError(f"the search found {found['matches']!r}")
            await provider.write(sandbox, "check/calc.py", b"def add(a, b):\n    return b + a\n")
            change = await provider.diff(sandbox, "check")
            if [(f.path, f.status) for f in change.files] != [
                ("calc.py", "M")
            ] or "+    return b + a" not in change.patch:
                raise RuntimeError("the diff does not show the change")
            try:
                await provider.exec(sandbox, "true", cwd="..")
            except SandboxError:
                pass
            else:
                raise RuntimeError("a command ran outside the workspace")
            # A firewall may accept a connection and close it: only an answer means the internet was reached.
            probe = (
                "python3 -c \"import socket; s = socket.create_connection(('1.1.1.1', 80), timeout=5); "
                "s.settimeout(5); s.sendall(b'HEAD / HTTP/1.0\\r\\n\\r\\n'); "
                "print('answered' if s.recv(16) else 'closed')\""
            )
            reached = await provider.exec(sandbox, probe, cwd="check", seconds=30)
            if reached.ok and "answered" in reached.stdout:
                raise RuntimeError("the sandbox reached the internet")
        finally:
            await provider.destroy(sandbox)
    return (
        f"ok: {settings.sandbox_provider} sandbox in {time.monotonic() - started:.1f}s: unpacked, committed, "
        f"tests passed in {tests.seconds:.1f}s, listed, read, searched, diffed; no way out of the "
        "workspace or to the internet; destroyed"
    )


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
