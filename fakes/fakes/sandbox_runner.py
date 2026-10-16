"""The sandbox runner (B-008): runs the commands of the sandbox stand-in's sandboxes.

It runs in its own container with no network at all, a read only root, no capabilities, CPU,
memory and process limits, as a non root user. The provider API (``fakes.sandbox``) writes a job
next to a sandbox's workspace (``<root>/<sandbox>/jobs/<job>.json``); this loop claims it (an
atomic rename), runs it in the workspace with a minimal environment, a time limit and capped
output, and writes ``<job>.result.json``. A heartbeat file tells the API the runner is there.

Standard library only: the runner image carries nothing but Python, git and pytest.
"""

import json
import os
import resource
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

ROOT = Path(os.environ.get("SANDBOX_ROOT", "/sandboxes"))
HEARTBEAT = ".runner-heartbeat"
MAX_OUTPUT = 64 * 1024
MAX_TIMEOUT = 600
BASE_ENV = {
    "PATH": "/usr/local/bin:/usr/bin:/bin",
    "LANG": "C.UTF-8",
    "PYTHONDONTWRITEBYTECODE": "1",
    "PYTHONUNBUFFERED": "1",
    "GIT_CONFIG_NOSYSTEM": "1",
}


def _limits(timeout: int) -> None:
    """In the child: CPU seconds, file size and open files bounded (memory and processes are the
    container's limits)."""
    resource.setrlimit(resource.RLIMIT_CPU, (timeout + 5, timeout + 10))
    resource.setrlimit(resource.RLIMIT_FSIZE, (200 * 1024 * 1024, 200 * 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_NOFILE, (512, 512))
    os.setsid()


def _tail(data: bytes) -> str:
    text = data.decode("utf-8", errors="replace")
    return text if len(text) <= MAX_OUTPUT else "…(truncated)\n" + text[-MAX_OUTPUT:]


def run(sandbox: Path, job: dict[str, Any]) -> dict[str, Any]:
    workspace = (sandbox / "workspace").resolve()
    cwd = (workspace / str(job.get("cwd") or "").lstrip("/")).resolve()
    if cwd != workspace and workspace not in cwd.parents:
        return {"exit_code": 126, "stdout": "", "stderr": "cwd outside the workspace", "timed_out": False, "seconds": 0}
    timeout = max(1, min(int(job.get("timeout") or 120), MAX_TIMEOUT))
    home = sandbox / "home"
    home.mkdir(exist_ok=True)
    env = {**BASE_ENV, "HOME": str(home), "TMPDIR": str(home)}
    env.update({str(k): str(v) for k, v in dict(job.get("env") or {}).items() if str(k).isidentifier()})
    started = time.monotonic()
    process = subprocess.Popen(
        ["/bin/sh", "-c", str(job["command"])], cwd=cwd, env=env, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, preexec_fn=lambda: _limits(timeout),
    )  # fmt: skip
    try:
        out, err = process.communicate(timeout=timeout)
        timed_out = False
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        out, err = process.communicate()
        timed_out = True
    return {"exit_code": process.returncode, "stdout": _tail(out), "stderr": _tail(err),
            "timed_out": timed_out, "seconds": round(time.monotonic() - started, 3)}  # fmt: skip


def loop(root: Path = ROOT, stop: threading.Event | None = None) -> None:
    while stop is None or not stop.is_set():
        root.mkdir(parents=True, exist_ok=True)
        (root / HEARTBEAT).write_text(str(time.time()))
        for path in sorted(root.glob("*/jobs/*.json")):
            if path.name.endswith((".result.json", ".running.json")):
                continue
            claimed = path.with_name(path.stem + ".running.json")
            try:
                path.rename(claimed)
            except OSError:
                continue  # taken (or removed) meanwhile
            sandbox = path.parent.parent
            try:
                result = run(sandbox, json.loads(claimed.read_text()))
            except Exception as exc:  # a broken job is reported, never fatal
                result = {"exit_code": 125, "stdout": "", "stderr": f"runner error: {type(exc).__name__}",
                          "timed_out": False, "seconds": 0}  # fmt: skip
            tmp = path.with_name(path.stem + ".result.tmp")
            tmp.write_text(json.dumps(result))
            tmp.rename(path.with_name(path.stem + ".result.json"))
            claimed.unlink(missing_ok=True)
        time.sleep(0.05)


if __name__ == "__main__":
    print(f"sandbox runner on {ROOT}", file=sys.stderr, flush=True)
    loop()
