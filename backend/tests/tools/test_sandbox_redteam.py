"""Red team (plan phase 12, "Done when"): from inside a task's sandbox, as a command the implementer
could run, there is no credential to find and nothing of the platform to reach.

The sandbox is built the real way (the GitHub App's installation token fetches the archive outside)
and commands run in the local sandbox runner (B-008): its own container with no network at all, a
read only root, no capabilities, as a non root user. Every check asserts the denial."""

import asyncio
import json
import shutil
import subprocess
from collections.abc import AsyncIterator
from uuid import UUID

import httpx
import pytest
import pytest_asyncio

from app.sandbox import HttpSandboxProvider, Ran
from app.sandbox.workspaces import Workspace, Workspaces
from tests.connect import connect_github, enable, link_github, sql
from tests.stack import FAKES, Stack, fakes_up
from tests.tools.test_integrations import Setup

pytestmark = pytest.mark.skipif(not fakes_up(), reason="stand-ins not reachable (run `make infra`)")
REPO = "acme/cart-service"
# Every service of the control plane by its Compose name, and the cloud metadata endpoint.
TARGETS = [("api", 8000), ("vault", 8100), ("postgres", 5432), ("pgbouncer", 6432), ("valkey", 6379),
           ("temporal", 7233), ("fakes", 4100), ("objectstore", 8333), ("jaeger", 4318), ("web", 5173),
           ("169.254.169.254", 80), ("metadata.google.internal", 80), ("host.docker.internal", 8000),
           ("8.8.8.8", 53), ("1.1.1.1", 443)]  # fmt: skip
PROBE = """
import json, socket, sys
out = {}
for host, port in json.loads(sys.argv[1]):
    try:
        socket.create_connection((host, port), timeout=2).close()
        out[f"{host}:{port}"] = "connected"
    except OSError as exc:
        out[f"{host}:{port}"] = type(exc).__name__
print(json.dumps(out))
"""


@pytest_asyncio.fixture(loop_scope="session")
async def sandbox(stack: Stack) -> AsyncIterator[tuple[Setup, HttpSandboxProvider, Workspace, str]]:
    acme = stack.env.world.acme
    enable(stack, acme, "github")
    await connect_github(stack, acme)
    await link_github(stack, acme, acme.user, "bob-gh")
    async with httpx.AsyncClient(timeout=60) as http:
        setup = Setup(stack, http)
        bob = await setup.executor("bob")
        prepared = await bob.run("github_prepare_workspace", {"repo": REPO, "push": True})
        assert not prepared.is_error, prepared.text
        credential = await bob.credential_for("github", "org")
        assert credential is not None
        provider = HttpSandboxProvider(f"{FAKES}/sandbox/v1", "sbx-local-key", http)
        ws = await Workspaces(stack.pool, provider).find(acme.id, UUID(str(bob.ctx.task_id)), REPO)
        assert ws is not None
        yield setup, provider, ws, credential.token
    sql(stack, "update app.tasks set status = 'completed', finished_at = now() where status = 'running'")


async def run(provider: HttpSandboxProvider, ws: Workspace, command: str) -> Ran:
    return await provider.exec(ws.sandbox_id, command, cwd=ws.root, seconds=60)


async def test_no_credential_in_the_environment_or_the_files(
    sandbox: tuple[Setup, HttpSandboxProvider, Workspace, str],
) -> None:
    _, provider, ws, token = sandbox
    env = (await run(provider, ws, "env")).stdout
    secrets = [token, "ghs_", "xoxb-", "sk-ant-", "sbx-local-key", "local-vault", "password", "SECRET", "TOKEN",
               "DATABASE_URL", "VAULT", "ANTHROPIC", "GITHUB_APP"]  # fmt: skip
    for secret in secrets:
        assert secret not in env, secret
    # Not in the working copy, its git config, the home directory or anywhere the runner can read
    # (other sandboxes included: no token ever enters one).
    for secret in (
        token,
        "xoxb-local",
        "sk-ant-local-key",
        "local-vault-service-token",
        "local-github-client-secret",
    ):
        # The pattern is put together inside the sandbox: the command (kept in a job file the grep
        # can see) never holds the whole secret.
        half = len(secret) // 2
        pattern = f"\"$(printf %s '{secret[:half]}' '{secret[half:]}')\""
        found = await run(
            provider, ws, f"grep -rIl -e {pattern} /sandboxes /tmp /runner /etc $HOME 2>/dev/null"
        )
        assert found.stdout.strip() == "", (secret[:6], found.stdout)
    # The same search finds a planted canary: an empty result above means absent, not a broken search.
    canary = f"canary-{ws.sandbox_id}-{token[-6:]}"
    await provider.write(ws.sandbox_id, ws.path("planted.txt"), canary.encode())
    half = len(canary) // 2
    found = await run(
        provider, ws, f"grep -rIl -e \"$(printf %s '{canary[:half]}' '{canary[half:]}')\" /sandboxes"
    )
    assert found.stdout.strip().endswith("planted.txt"), found.stdout
    await run(provider, ws, "rm planted.txt")
    config = await run(provider, ws, "git config --list --show-origin; git remote -v")
    assert "http" not in config.stdout  # no remote, no credential helper, no extraheader
    assert "extraheader" not in config.stdout.lower()


async def test_no_network_to_the_control_plane_or_anywhere(
    sandbox: tuple[Setup, HttpSandboxProvider, Workspace, str],
) -> None:
    _, provider, ws, _ = sandbox
    targets = [*TARGETS, *await asyncio.to_thread(container_addresses)]
    command = f"python -c {_quote(PROBE)} {_quote(json.dumps(targets))}"
    ran = await run(provider, ws, command)
    assert ran.ok, ran.stderr
    results = json.loads(ran.stdout)
    connected = [t for t, outcome in results.items() if outcome == "connected"]
    assert connected == [], connected
    assert len(results) == len(targets)
    # Only loopback is up (the kernel's default tunnel devices exist, down and without addresses), and
    # there is no route at all.
    up = await run(
        provider, ws, "for i in /sys/class/net/*; do echo $(basename $i) $(cat $i/operstate); done"
    )
    states = dict(line.split() for line in up.stdout.strip().splitlines())
    assert {name for name, state in states.items() if state != "down"} == {"lo"}, states
    routes = await run(provider, ws, "tail -n +2 /proc/net/route; grep -v ' lo$' /proc/net/ipv6_route")
    assert routes.stdout.strip() == "", routes.stdout


async def test_unprivileged_and_read_only_outside_its_workspace(
    sandbox: tuple[Setup, HttpSandboxProvider, Workspace, str],
) -> None:
    _, provider, ws, _ = sandbox
    whoami = await run(provider, ws, "id -u; grep CapEff /proc/self/status")
    uid, caps = whoami.stdout.split("\n", 1)
    assert uid.strip() != "0"
    assert caps.split()[1] == "0000000000000000"  # no capabilities
    for path in ("/etc/agent", "/usr/agent", "/runner/agent", "/agent"):
        assert not (await run(provider, ws, f"touch {path}")).ok, path
    assert (
        await run(provider, ws, "touch inside.txt && rm inside.txt")
    ).ok  # its own working copy is writable
    # The provider refuses paths outside the workspace.
    with pytest.raises(Exception, match="refused"):
        await provider.write(ws.sandbox_id, "../../escape.txt", b"x")


def container_addresses() -> list[tuple[str, int]]:
    """The Compose containers' own addresses too, not only their names (when docker is at hand)."""
    if not shutil.which("docker"):
        return []
    out = []
    for name in ("api", "vault", "postgres", "valkey", "temporal", "fakes"):
        ip = subprocess.run(["docker", "inspect", "-f", "{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}",
                             f"agentplatform-{name}-1"], capture_output=True, text=True, check=False).stdout.split()  # fmt: skip
        out += [(address, 80) for address in ip[:1]]
    return out


def _quote(text: str) -> str:
    return "'" + text.replace("'", "'\"'\"'") + "'"
