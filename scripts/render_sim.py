"""Local Render simulation: execute render.yaml the way Render would, with Docker.

Run with `make render-sim`. It reads the staging environment from render.yaml and:
  1. builds the backend image from dockerContext / dockerfilePath,
  2. starts throwaway stand-ins for the Blueprint's Postgres, Key Value and Temporal Cloud,
  3. runs preDeployCommand with the env vars the Blueprint wires,
  4. starts each web and worker service with Render's default PORT (10000), probes healthCheckPath,
  5. runs the cron command once,
  6. stops services with SIGTERM and checks they exit within maxShutdownDelaySeconds,
  7. builds the static site with its buildCommand from a clean copy of the repository and serves
     staticPublishPath with its routes and headers.

This is a workaround for having no Render account (B-001); it cannot prove Render-specific
behavior, only that the Blueprint is coherent with the code and the image.
"""

from __future__ import annotations

import http.server
import os
import shlex
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
PREFIX = "rendersim"
NETWORK = f"{PREFIX}-net"
IMAGE = f"{PREFIX}-backend:latest"
RENDER_PORT = 10000
# Must be a path Docker can bind mount (Docker Desktop does not share /tmp by default).
WORKDIR = Path.home() / ".cache" / "agentplatform-rendersim"

# Values an operator would type into the dashboard for `sync: false` variables.
DASHBOARD_SECRETS = {
    "TEMPORAL_ADDRESS": f"{PREFIX}-temporal:7233",
    "TEMPORAL_NAMESPACE": "default",
    "TEMPORAL_API_KEY": "",  # the local dev server needs none; Temporal Cloud would
    "VITE_API_URL": f"http://localhost:{RENDER_PORT}",
}

failures: list[str] = []


def ok(msg: str) -> None:
    print(f"  \033[32mok\033[0m   {msg}")


def bad(msg: str) -> None:
    print(f"  \033[31mFAIL\033[0m {msg}")
    failures.append(msg)


def note(msg: str) -> None:
    print(f"  \033[33mnote\033[0m {msg}")


def sh(
    *args: str, check: bool = True, capture: bool = True, timeout: float = 600
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, check=check, capture_output=capture, text=True, timeout=timeout)  # noqa: S603


def docker(*args: str, check: bool = True, timeout: float = 600) -> subprocess.CompletedProcess[str]:
    return sh("docker", *args, check=check, timeout=timeout)


def staging() -> dict[str, Any]:
    blueprint = yaml.safe_load((ROOT / "render.yaml").read_text())
    project = next(p for p in blueprint["projects"] if p["name"] == "agent-platform-staging")
    env: dict[str, Any] = next(e for e in project["environments"] if e["name"] == "staging")
    return env


def resolve_env(service: dict[str, Any], env: dict[str, Any]) -> dict[str, str]:
    """Resolve envVars like Render: groups, references to stores, dashboard secrets, literals."""
    groups = {g["name"]: g["envVars"] for g in env.get("envVarGroups", [])}
    databases = {d["name"]: d for d in env.get("databases", [])}
    out: dict[str, str] = {}

    def one(var: dict[str, Any]) -> None:
        key = var["key"]
        if "fromDatabase" in var:
            db = databases[var["fromDatabase"]["name"]]
            prop = var["fromDatabase"]["property"]
            url = f"postgresql://postgres:postgres@{PREFIX}-db:5432/{db.get('databaseName', 'postgres')}"
            out[key] = {"connectionString": url, "connectionPoolString": url}[prop]
        elif "fromService" in var:
            ref = var["fromService"]
            if ref["type"] == "keyvalue" and ref.get("property") == "connectionString":
                out[key] = f"redis://{PREFIX}-kv:6379"
            else:
                raise SystemExit(f"render_sim: unsupported reference {ref}")
        elif var.get("sync") is False:
            out[key] = DASHBOARD_SECRETS[key]
        else:
            out[key] = str(var["value"])

    for var in service.get("envVars", []):
        if "fromGroup" in var:
            for gvar in groups[var["fromGroup"]]:
                one(gvar)
        else:
            one(var)
    if service["type"] == "web":
        out["PORT"] = str(RENDER_PORT)  # Render injects PORT for web services
    return out


def env_flags(env: dict[str, str]) -> list[str]:
    flags: list[str] = []
    for key, value in env.items():
        flags += ["-e", f"{key}={value}"]
    return flags


def cleanup() -> None:
    names = docker("ps", "-aq", "--filter", f"name=^{PREFIX}-", check=False).stdout.split()
    if names:
        docker("rm", "-f", *names, check=False)
    docker("network", "rm", NETWORK, check=False)
    shutil.rmtree(WORKDIR, ignore_errors=True)


def wait_for(predicate: Any, timeout: float, interval: float = 0.5) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def http_ok(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=2) as resp:  # noqa: S310
            return bool(resp.status == 200)
    except (urllib.error.URLError, ConnectionError, TimeoutError):
        return False


def start_stores(env: dict[str, Any]) -> None:
    docker("network", "create", NETWORK)
    db = env["databases"][0]
    docker(
        "run", "-d", "--name", f"{PREFIX}-db", "--network", NETWORK,
        "-e", "POSTGRES_PASSWORD=postgres", "-e", f"POSTGRES_DB={db.get('databaseName', 'postgres')}",
        f"postgres:{db['postgresMajorVersion']}",
    )  # fmt: skip
    docker("run", "-d", "--name", f"{PREFIX}-kv", "--network", NETWORK, "valkey/valkey:8-alpine")
    docker(
        "run", "-d", "--name", f"{PREFIX}-temporal", "--network", NETWORK,
        "temporalio/temporal:latest", "server", "start-dev", "--ip=0.0.0.0",
    )  # fmt: skip
    pg_ready = wait_for(
        lambda: docker("exec", f"{PREFIX}-db", "pg_isready", "-U", "postgres", check=False).returncode == 0,
        60,
    )
    temporal_ready = wait_for(
        lambda: docker(
            "exec", f"{PREFIX}-temporal", "temporal", "operator", "cluster", "health",
            "--address", "127.0.0.1:7233", check=False,
        ).returncode == 0,
        90,
    )  # fmt: skip
    if pg_ready and temporal_ready:
        ok(f"stand-ins up: postgres {db['postgresMajorVersion']}, key value, temporal")
    else:
        raise SystemExit("render_sim: stand-in stores did not start")


def run_command(service: dict[str, Any], field: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    # Render does not wrap commands in a shell (its docs say to use `sh -c` for several commands),
    # so the command is split into argv and replaces the image CMD.
    argv = shlex.split(service[field])
    return docker("run", "--rm", "--network", NETWORK, *env_flags(env), IMAGE, *argv, check=False)


def simulate_backend(env: dict[str, Any]) -> None:
    services = {s["name"]: s for s in env["services"]}
    docker_services = [s for s in env["services"] if s.get("runtime") == "docker"]
    contexts = {(s["dockerContext"], s["dockerfilePath"]) for s in docker_services}
    if len(contexts) != 1:
        bad(f"backend services use different images: {contexts}")
        return
    ((context, dockerfile),) = contexts
    docker("build", "-q", "-f", str(ROOT / dockerfile), "-t", IMAGE, str(ROOT / context), timeout=900)
    ok(f"image built from {dockerfile}")

    api = services["api-staging"]
    api_env = resolve_env(api, env)

    result = run_command(api, "preDeployCommand", api_env)
    if result.returncode == 0:
        ok(f"preDeployCommand `{api['preDeployCommand']}`: {result.stdout.strip()}")
    else:
        bad(f"preDeployCommand failed: {result.stderr.strip()[-500:]}")
        return
    again = run_command(api, "preDeployCommand", api_env)
    if again.returncode == 0 and "none pending" in again.stdout:
        ok("preDeployCommand is idempotent on the next deploy")
    else:
        bad(f"second preDeployCommand: {again.stdout} {again.stderr[-300:]}")

    docker(
        "run", "-d", "--name", f"{PREFIX}-api", "--network", NETWORK, "-p", f"{RENDER_PORT}:{RENDER_PORT}",
        *env_flags(api_env), IMAGE, *shlex.split(api["dockerCommand"]),
    )  # fmt: skip
    health = f"http://localhost:{RENDER_PORT}{api['healthCheckPath']}"
    if wait_for(lambda: http_ok(health), 60):
        ok(f"api-staging healthy on PORT={RENDER_PORT} at {api['healthCheckPath']}")
    else:
        bad(f"api-staging health check failed: {docker('logs', f'{PREFIX}-api', check=False).stderr[-500:]}")

    worker = services["worker-staging"]
    worker_env = resolve_env(worker, env)
    docker(
        "run", "-d", "--name", f"{PREFIX}-worker", "--network", NETWORK, *env_flags(worker_env),
        IMAGE, *shlex.split(worker["dockerCommand"]),
    )  # fmt: skip
    polling = wait_for(lambda: "worker polling" in docker("logs", f"{PREFIX}-worker", check=False).stderr, 60)
    pong = docker("exec", f"{PREFIX}-worker", "app", "check", "temporal", check=False)
    if polling and pong.stdout.strip() == "pong: smoke":
        ok("worker-staging connected to Temporal and answers PingWorkflow")
    else:
        logs = docker("logs", f"{PREFIX}-worker", check=False).stderr[-300:]
        bad(f"worker-staging: {pong.stderr[-300:]} {logs}")

    cron = services["maintenance-staging"]
    result = run_command(cron, "dockerCommand", resolve_env(cron, env))
    if result.returncode == 0:
        ok(f"maintenance-staging ran: {result.stdout.strip()[-200:]}")
    elif result.returncode == 2 and "not implemented yet" in result.stderr:
        note(f"maintenance-staging: {result.stderr.strip()}")
    else:
        bad(f"maintenance-staging exited {result.returncode}: {result.stderr[-300:]}")

    for name, container in (("api-staging", f"{PREFIX}-api"), ("worker-staging", f"{PREFIX}-worker")):
        delay = services[name].get("maxShutdownDelaySeconds", 30)
        started = time.monotonic()
        docker("stop", "-t", str(delay), container, timeout=delay + 30)
        took = time.monotonic() - started
        code = docker("inspect", "-f", "{{.State.ExitCode}}", container).stdout.strip()
        if code == "0" and took < delay:
            ok(f"{name} stopped on SIGTERM in {took:.1f}s (limit {delay}s), exit 0")
        else:
            bad(f"{name} stop: exit {code} after {took:.1f}s (limit {delay}s)")


# ------------------------------------------------------------------ static site


def clean_checkout() -> Path:
    """Copy what would be committed (tracked plus untracked, not ignored) to a fresh directory."""
    files = sh("git", "-C", str(ROOT), "ls-files", "-z", "--cached", "--others", "--exclude-standard").stdout
    target = WORKDIR / "repo"
    for rel in filter(None, files.split("\0")):
        src = ROOT / rel
        if src.is_file():
            dest = target / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
    return target


def make_handler(publish: Path, site: dict[str, Any]) -> type[http.server.SimpleHTTPRequestHandler]:
    routes = site.get("routes", [])
    headers = site.get("headers", [])

    class RenderStatic(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, directory=str(publish), **kwargs)

        def log_message(self, *args: Any) -> None:
            pass

        def send_head(self) -> Any:
            # Render serves an existing file first; rewrites apply only when nothing matches.
            path = self.path.split("?", 1)[0]
            if not (publish / path.lstrip("/")).is_file() or path == "/":
                for route in routes:
                    if route["type"] == "rewrite" and fnmatch(path, route["source"]):
                        self.path = route["destination"]
                        break
            return super().send_head()

        def end_headers(self) -> None:
            path = self.path.split("?", 1)[0]
            for header in headers:
                if fnmatch(path, header["path"]):
                    self.send_header(header["name"], header["value"])
            super().end_headers()

    return RenderStatic


@contextmanager
def serve(publish: Path, site: dict[str, Any]) -> Iterator[str]:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), make_handler(publish, site))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()


def simulate_static(env: dict[str, Any]) -> None:
    site = next(s for s in env["services"] if s.get("runtime") == "static")
    repo = clean_checkout()
    site_env = {k: v for k, v in resolve_env(site, env).items() if k != "PORT"}
    # Build as root like Render's builder, then hand the files back to the host user so cleanup works
    # on native Docker as well as Docker Desktop.
    script = f"{site['buildCommand']}; rc=$?; chown -R {os.getuid()}:{os.getgid()} /repo; exit $rc"
    build = docker(
        "run", "--rm", "-v", f"{repo}:/repo", "-w", "/repo", *env_flags(site_env),
        "node:22-slim", "sh", "-c", script, check=False, timeout=900,
    )  # fmt: skip
    publish = repo / site["staticPublishPath"]
    if build.returncode != 0 or not (publish / "index.html").is_file():
        bad(f"static build failed: {build.stderr[-800:]}")
        return
    ok(f"web-staging built from a clean checkout with `{site['buildCommand']}`")
    asset = next((p for p in (publish / "assets").glob("*.js")), None)

    with serve(publish, site) as base:

        def get(path: str) -> tuple[int, str, dict[str, str]]:
            with urllib.request.urlopen(base + path, timeout=5) as resp:  # noqa: S310
                return resp.status, resp.read().decode(errors="replace"), dict(resp.headers)

        status, body, hdrs = get("/")
        if status == 200 and '<div id="root">' in body and hdrs.get("X-Frame-Options") == "DENY":
            ok("static site serves / with security headers")
        else:
            bad(f"static / -> {status} {hdrs}")
        status, body, _ = get("/settings/members?tab=invites")
        if status == 200 and '<div id="root">' in body:
            ok("deep links rewrite to index.html (SPA)")
        else:
            bad(f"deep link -> {status}")
        if asset is not None:
            status, body, hdrs = get(f"/assets/{asset.name}")
            if (
                status == 200
                and "immutable" in hdrs.get("Cache-Control", "")
                and '<div id="root">' not in body
            ):
                ok("hashed assets served as files with immutable caching")
            else:
                bad(f"asset -> {status} {hdrs.get('Cache-Control')}")


def main() -> int:
    env = staging()
    print("Render simulation of render.yaml (staging)")
    cleanup()
    try:
        start_stores(env)
        simulate_backend(env)
        simulate_static(env)
    finally:
        cleanup()
    if failures:
        print(f"{len(failures)} check(s) failed")
        return 1
    print("Render simulation passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
