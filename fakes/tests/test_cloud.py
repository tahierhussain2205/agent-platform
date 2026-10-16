"""The Google Cloud stand-in (service account tokens, Logging, Error Reporting, Trace), the GitHub
history it matches (commits, archives, deployments) and the sandbox provider stand-in."""

import io
import json
import tarfile
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from fakes.config import Config
from fakes.gcp import matches, signature_of
from fakes.main import create_app
from tests.test_providers import PUBLIC, app_jwt, install

SA_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
SA_PUBLIC = (
    SA_KEY.public_key()
    .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    .decode()
)
SA = "agent-reader@acme-prod.iam.gserviceaccount.com"
READ = "https://www.googleapis.com/auth/cloud-platform.read-only"
API_KEY = {"authorization": "Bearer sbx-local-key"}


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    config = Config(
        state_dir=tmp_path, seed_fixture=tmp_path / "none.json", github_app_public_key=PUBLIC,
        gcp_sa_public_key=SA_PUBLIC,
    )  # fmt: skip
    with TestClient(create_app(config, persist=False), follow_redirects=False) as c:
        yield c


def assertion(iss: str = SA, scope: str = READ, key: Any = SA_KEY, life: int = 3600, aud: str = "x/gcp/token") -> str:
    now = int(time.time())
    return jwt.encode({"iss": iss, "scope": scope, "aud": aud, "iat": now, "exp": now + life}, key, algorithm="RS256")


def gcp_token(client: TestClient, **claims: Any) -> Any:
    return client.post(
        "/gcp/token",
        data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": assertion(**claims)},
    )


def bearer(client: TestClient) -> dict[str, str]:
    return {"authorization": f"Bearer {gcp_token(client).json()['access_token']}"}


def iso(hours_ago: float) -> str:
    return (datetime.now(UTC) - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


# ------------------------------------------------------------------ Google Cloud


def test_service_account_tokens(client: TestClient) -> None:
    ok = gcp_token(client)
    assert ok.status_code == 200
    assert ok.json()["token_type"] == "Bearer"
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    for bad in ({"key": other}, {"iss": "someone@else.iam.gserviceaccount.com"}, {"life": 7200},
                {"scope": "https://www.googleapis.com/auth/drive"}, {"aud": "https://evil.example/collect"}):  # fmt: skip
        refused = gcp_token(client, **bad)
        assert refused.status_code == 400, bad
    assert client.post("/gcp/token", data={"grant_type": "password"}).status_code == 400


def test_entries_are_filtered_at_the_source(client: TestClient) -> None:
    auth = bearer(client)
    flt = (
        'resource.type="cloud_run_revision" AND resource.labels.service_name="cart-service" AND severity>=ERROR '
        f'AND timestamp>="{iso(8)}" AND timestamp<="{iso(0)}" AND (jsonPayload.message:"checkout" OR '
        'jsonPayload.exception:"checkout")'
    )
    body = {"resourceNames": ["projects/acme-prod"], "filter": flt, "orderBy": "timestamp desc", "pageSize": 10}
    first = client.post("/gcp/logging/v2/entries:list", json=body, headers=auth).json()
    assert len(first["entries"]) == 10
    assert all(e["severity"] == "ERROR" for e in first["entries"])
    assert first["entries"][0]["timestamp"] > first["entries"][-1]["timestamp"]  # newest first
    rest = client.post("/gcp/logging/v2/entries:list", json={**body, "pageToken": first["nextPageToken"],
                                                             "pageSize": 100}, headers=auth).json()  # fmt: skip
    assert len(first["entries"]) + len(rest["entries"]) == 37
    assert "IndexError" in rest["entries"][0]["jsonPayload"]["exception"]
    assert "nextPageToken" not in rest
    # Unknown syntax is an error, not everything.
    bad = client.post("/gcp/logging/v2/entries:list", json={**body, "filter": "service ~ cart"}, headers=auth)
    assert bad.status_code == 400
    assert bad.json()["error"]["status"] == "INVALID_ARGUMENT"


def test_other_projects_and_bad_tokens_are_refused(client: TestClient) -> None:
    auth = bearer(client)
    other = client.post("/gcp/logging/v2/entries:list", json={"resourceNames": ["projects/globex-prod"]}, headers=auth)
    assert other.status_code == 403
    assert "Globex" not in other.text
    assert client.post("/gcp/logging/v2/entries:list", json={"resourceNames": ["projects/acme-prod"]},
                       headers={"authorization": "Bearer nope"}).status_code == 401  # fmt: skip
    assert client.get("/gcp/trace/v1/projects/globex-prod/traces/x", headers=auth).status_code == 403
    client.post("/_admin/gcp/expire-tokens")
    assert client.post("/gcp/logging/v2/entries:list", json={"resourceNames": ["projects/acme-prod"]},
                       headers=auth).status_code == 401  # fmt: skip


def test_error_groups_and_traces(client: TestClient) -> None:
    auth = bearer(client)
    day = client.get(
        "/gcp/errorreporting/v1beta1/projects/acme-prod/groupStats",
        params={"timeRange.period": "PERIOD_1_DAY", "serviceFilter.service": "cart-service"},
        headers=auth,
    ).json()["errorGroupStats"]
    assert [(g["count"], g["representative"]["message"].splitlines()[-1]) for g in day] == [
        ("37", "IndexError: list assignment index out of range")
    ]
    week = client.get("/gcp/errorreporting/v1beta1/projects/acme-prod/groupStats",
                      params={"timeRange.period": "PERIOD_1_WEEK"}, headers=auth).json()["errorGroupStats"]  # fmt: skip
    assert sorted(int(g["count"]) for g in week) == [26, 37]  # the webhook incident two days ago too
    entries = client.post("/gcp/logging/v2/entries:list", json={"resourceNames": ["projects/acme-prod"],
                          "filter": 'severity>=ERROR AND resource.labels.service_name="cart-service"',
                          "orderBy": "timestamp desc", "pageSize": 1}, headers=auth).json()["entries"]  # fmt: skip
    trace_id = entries[0]["trace"].rsplit("/", 1)[1]
    trace = client.get(f"/gcp/trace/v1/projects/acme-prod/traces/{trace_id}", headers=auth).json()
    assert [s["name"] for s in trace["spans"]] == ["POST /checkout", "pricing.total", "GET tax-service /rates"]
    assert client.get("/gcp/trace/v1/projects/acme-prod/traces/0000", headers=auth).status_code == 404


def test_filter_language_subset() -> None:
    entry = {"severity": "WARNING", "timestamp": "2026-10-16T08:00:00.000000Z", "resource": {"labels": {"service_name": "web-app"}},
             "jsonPayload": {"message": "slow search request: 4800ms", "region": "europe-west1"}}  # fmt: skip
    assert matches(entry, 'severity>=WARNING AND resource.labels.service_name="web-app"')
    assert not matches(entry, "severity>=ERROR")
    assert matches(entry, '(jsonPayload.message:"SLOW" OR jsonPayload.exception:"slow")')
    assert matches(entry, 'timestamp>="2026-10-16T07:00:00Z" AND timestamp<="2026-10-16T09:00:00Z"')
    assert not matches(entry, 'timestamp<"2026-10-16T07:00:00Z"')
    assert matches(entry, 'jsonPayload.region="europe-west1"')
    assert signature_of({"jsonPayload": {"exception": "Traceback\nSignatureError: mismatch for event evt_7001"}}) == (
        "SignatureError: mismatch for event evt_N"
    )


# ------------------------------------------------------------------ GitHub history


def installation_token(client: TestClient) -> dict[str, str]:
    setup = install(client)
    token = client.post(f"/github/api/app/installations/{setup['installation_id']}/access_tokens",
                        headers={"authorization": f"Bearer {app_jwt()}"}).json()["token"]  # fmt: skip
    return {"authorization": f"Bearer {token}"}


def test_commits_deployments_and_archives(client: TestClient) -> None:
    auth = installation_token(client)
    commits = client.get("/github/api/repos/acme/cart-service/commits", headers=auth).json()
    assert [c["commit"]["message"] for c in commits] == [
        "Preallocate price slots for faster totals", "Reload the webhook signing key on rotation",
        "Verify webhook signatures with the signing key", "Create cart-service",
    ]  # fmt: skip
    recent = client.get("/github/api/repos/acme/cart-service/commits",
                        params={"since": iso(12), "path": "src/pricing"}, headers=auth).json()  # fmt: skip
    assert [c["commit"]["message"] for c in recent] == ["Preallocate price slots for faster totals"]
    head = client.get(f"/github/api/repos/acme/cart-service/commits/{commits[0]['sha'][:7]}", headers=auth).json()
    (changed,) = head["files"]
    assert (changed["filename"], changed["status"]) == ("src/pricing/cart.py", "modified")
    assert "+    prices = [0.0] * MAX_LINES" in changed["patch"]
    deploys = client.get("/github/api/repos/acme/cart-service/deployments", params={"environment": "production"},
                         headers=auth).json()  # fmt: skip
    assert deploys[0]["sha"] == commits[0]["sha"]
    # The archive at an older commit has the code as it was then.
    old = client.get(f"/github/api/repos/acme/cart-service/tarball/{commits[2]['sha']}", headers=auth)
    with tarfile.open(fileobj=io.BytesIO(old.content), mode="r:gz") as tar:
        names = tar.getnames()
        top = names[0].split("/")[0]
        assert top == f"acme-cart-service-{commits[2]['sha'][:7]}"
        cart = tar.extractfile(f"{top}/src/pricing/cart.py")
        assert cart is not None
        assert "MAX_LINES" not in cart.read().decode()
    at_ref = client.get("/github/api/repos/acme/cart-service/contents/src/pricing/cart.py",
                        params={"ref": commits[3]["sha"]}, headers=auth).json()  # fmt: skip
    assert at_ref["type"] == "file"
    assert client.get("/github/api/repos/globex/payments/commits", headers=auth).status_code == 404
    assert client.get("/github/api/repos/acme/cart-service/commits/zzz", headers=auth).status_code == 422


# ------------------------------------------------------------------ sandbox provider


def archive(files: dict[str, bytes], top: str = "repo-abc1234") -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(f"{top}/{name}" if top else name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def sandbox(client: TestClient, **spec: Any) -> str:
    made = client.post("/sandbox/v1/sandboxes", json={"ttl_seconds": 600, **spec}, headers=API_KEY)
    assert made.status_code == 201, made.text
    return str(made.json()["id"])


def test_sandboxes_hold_files_and_answer_reads(client: TestClient) -> None:
    assert client.post("/sandbox/v1/sandboxes", json={}, headers={"authorization": "Bearer x"}).status_code == 401
    sid = sandbox(client, metadata={"task": "t1"})
    files = {"src/app.py": b"def main():\n    raise IndexError('boom')\n", "README.md": b"# hi\n", "img.bin": b"\0\1\2"}
    up = client.put(f"/sandbox/v1/sandboxes/{sid}/archive", params={"path": "repo", "strip": 1},
                    content=archive(files), headers=API_KEY)  # fmt: skip
    assert up.json()["files"] == 3
    listing = client.get(f"/sandbox/v1/sandboxes/{sid}/files", params={"path": "repo", "recursive": "true"},
                         headers=API_KEY).json()  # fmt: skip
    assert {e["path"] for e in listing["entries"] if e["type"] == "file"} == {
        "repo/src/app.py",
        "repo/README.md",
        "repo/img.bin",
    }
    read = client.get(f"/sandbox/v1/sandboxes/{sid}/files/content", params={"path": "repo/src/app.py", "start": 2},
                      headers=API_KEY).json()  # fmt: skip
    assert (read["start"], read["end"], read["total_lines"]) == (2, 2, 2)
    assert "IndexError" in read["content"]
    assert client.get(f"/sandbox/v1/sandboxes/{sid}/files/content", params={"path": "repo/img.bin"},
                      headers=API_KEY).status_code == 422  # fmt: skip
    found = client.post(f"/sandbox/v1/sandboxes/{sid}/search", json={"pattern": "indexerror", "path": "repo"},
                        headers=API_KEY).json()  # fmt: skip
    assert found["matches"] == [{"path": "repo/src/app.py", "line": 2, "text": "    raise IndexError('boom')"}]
    regex = client.post(f"/sandbox/v1/sandboxes/{sid}/search", json={"pattern": "def \\w+\\(", "regex": True,
                        "glob": "*.py"}, headers=API_KEY).json()  # fmt: skip
    assert [m["line"] for m in regex["matches"]] == [1]
    assert client.post(f"/sandbox/v1/sandboxes/{sid}/exec", json={"cmd": ["ls"]}, headers=API_KEY).status_code == 422
    assert client.delete(f"/sandbox/v1/sandboxes/{sid}", headers=API_KEY).status_code == 204
    assert client.get(f"/sandbox/v1/sandboxes/{sid}", headers=API_KEY).status_code == 404
    destroyed = client.get("/_admin/sandbox/sandboxes").json()["destroyed"]
    assert {"id": sid, "reason": "deleted"}.items() <= destroyed[-1].items()


def test_sandboxes_refuse_unsafe_archives_and_paths(client: TestClient) -> None:
    sid = sandbox(client, limits={"disk_mb": 1})
    for evil in ({"../escape.txt": b"x"}, {"/etc/passwd": b"x"}):
        bad = client.put(f"/sandbox/v1/sandboxes/{sid}/archive", content=archive(evil, top=""), headers=API_KEY)
        assert bad.status_code == 422, evil
    link = io.BytesIO()
    with tarfile.open(fileobj=link, mode="w:gz") as tar:
        info = tarfile.TarInfo("secret")
        info.type, info.linkname = tarfile.SYMTYPE, "/etc/shadow"
        tar.addfile(info)
    assert client.put(f"/sandbox/v1/sandboxes/{sid}/archive", content=link.getvalue(),
                      headers=API_KEY).status_code == 422  # fmt: skip
    big = archive({"big.txt": b"x" * (2 * 1024 * 1024)})
    assert client.put(f"/sandbox/v1/sandboxes/{sid}/archive", content=big, headers=API_KEY).status_code == 507
    for path in ("../../", "/etc"):
        assert (
            client.get(f"/sandbox/v1/sandboxes/{sid}/files", params={"path": path}, headers=API_KEY).status_code == 404
        )
        assert client.get(f"/sandbox/v1/sandboxes/{sid}/files/content", params={"path": f"{path}passwd"},
                          headers=API_KEY).status_code == 404  # fmt: skip
    assert client.post("/sandbox/v1/sandboxes", json={"ttl_seconds": 99999}, headers=API_KEY).status_code == 422


def test_sandboxes_expire(client: TestClient) -> None:
    sid = sandbox(client)
    client.post(f"/_admin/sandbox/sandboxes/{sid}/expire")
    assert client.get(f"/sandbox/v1/sandboxes/{sid}", headers=API_KEY).status_code == 404
    state = client.get("/_admin/sandbox/sandboxes").json()
    assert state["live"] == []
    assert json.dumps(state["destroyed"][-1]).count("expired") == 1


# ------------------------------------------------------------------ commands (with the runner in a thread)


@pytest.fixture
def runner_client(tmp_path: Path) -> Iterator[TestClient]:
    import threading

    from fakes import sandbox_runner

    root = tmp_path / "sandboxes"
    stop = threading.Event()
    thread = threading.Thread(target=sandbox_runner.loop, args=(root, stop), daemon=True)
    thread.start()
    config = Config(state_dir=tmp_path, seed_fixture=tmp_path / "none.json", sandbox_root=root)
    with TestClient(create_app(config, persist=False)) as c:
        for _ in range(100):
            if (root / sandbox_runner.HEARTBEAT).exists():
                break
            time.sleep(0.02)
        yield c
    stop.set()
    thread.join(5)


def test_the_runner_stays_alive_through_a_long_command(runner_client: TestClient) -> None:
    from concurrent.futures import ThreadPoolExecutor

    c = runner_client
    sid, other = sandbox(c), sandbox(c)
    with ThreadPoolExecutor(1) as pool:  # one command keeps the runner busy past the 10 s liveness window
        long = pool.submit(lambda: c.post(f"/sandbox/v1/sandboxes/{sid}/exec",
                                          json={"command": "sleep 12", "timeout_seconds": 30}, headers=API_KEY))  # fmt: skip
        time.sleep(11)
        queued = c.post(f"/sandbox/v1/sandboxes/{other}/exec", json={"command": "echo still here"}, headers=API_KEY)
        assert queued.status_code == 200, queued.text  # waits its turn instead of "runner_unavailable"
        assert queued.json()["stdout"] == "still here\n"
        assert long.result().json()["exit_code"] == 0


def test_commands_run_in_the_workspace_with_limits(runner_client: TestClient) -> None:
    c = runner_client
    sid = sandbox(c)
    assert c.put(f"/sandbox/v1/sandboxes/{sid}/files/content", params={"path": "repo/app.py"},
                 content=b"print('hi')\n", headers=API_KEY).json() == {"path": "repo/app.py", "bytes": 12}  # fmt: skip
    ran = c.post(f"/sandbox/v1/sandboxes/{sid}/exec", json={"command": "python app.py && echo $HOME", "cwd": "repo"},
                 headers=API_KEY).json()  # fmt: skip
    assert (ran.get("exit_code"), ran.get("timed_out")) == (0, False), ran
    assert ran["stdout"].startswith("hi\n")
    assert ran["stdout"].strip().endswith(f"{sid}/home")  # a home of its own, not the runner's
    env = c.post(f"/sandbox/v1/sandboxes/{sid}/exec", json={"command": "env"}, headers=API_KEY).json()["stdout"]
    assert "SANDBOX" not in env and "KEY" not in env  # a minimal environment
    slow = c.post(f"/sandbox/v1/sandboxes/{sid}/exec", json={"command": "sleep 5", "timeout_seconds": 1},
                  headers=API_KEY).json()  # fmt: skip
    assert slow["timed_out"] is True
    big = c.post(f"/sandbox/v1/sandboxes/{sid}/exec", json={"command": "python -c \"print('x' * 200000)\""},
                 headers=API_KEY).json()  # fmt: skip
    assert len(big["stdout"]) < 70_000
    assert big["stdout"].startswith("…(truncated)")
    for bad in ({"command": ""}, {"command": "ls", "cwd": "../.."}):
        assert c.post(f"/sandbox/v1/sandboxes/{sid}/exec", json=bad, headers=API_KEY).status_code == 422
    assert c.put(f"/sandbox/v1/sandboxes/{sid}/files/content", params={"path": "../escape"}, content=b"x",
                 headers=API_KEY).status_code == 422  # fmt: skip


def test_the_change_of_a_working_tree_is_exported(runner_client: TestClient) -> None:
    c = runner_client
    sid = sandbox(c)
    files = {"src/cart.py": b"MAX = 19\n", "README.md": b"# r\n", "old.txt": b"bye\n"}
    c.put(f"/sandbox/v1/sandboxes/{sid}/archive", params={"path": "repo", "strip": 1}, content=archive(files),
          headers=API_KEY)  # fmt: skip
    base = "git init -q && git add -A && git -c user.name=agent -c user.email=agent@local commit -qm base"
    assert c.post(f"/sandbox/v1/sandboxes/{sid}/exec", json={"command": base, "cwd": "repo"},
                  headers=API_KEY).json()["exit_code"] == 0  # fmt: skip
    c.put(f"/sandbox/v1/sandboxes/{sid}/files/content", params={"path": "repo/src/cart.py"},
          content=b"MAX = 100\n", headers=API_KEY)  # fmt: skip
    c.put(f"/sandbox/v1/sandboxes/{sid}/files/content", params={"path": "repo/tests/test_cart.py"},
          content=b"def test(): pass\n", headers=API_KEY)  # fmt: skip
    c.post(f"/sandbox/v1/sandboxes/{sid}/exec", json={"command": "rm old.txt && chmod +x README.md", "cwd": "repo"},
           headers=API_KEY)  # fmt: skip
    diff = c.post(f"/sandbox/v1/sandboxes/{sid}/diff", json={"path": "repo"}, headers=API_KEY).json()
    by_path = {f["path"]: f for f in diff["files"]}
    assert {p: f["status"] for p, f in by_path.items()} == {
        "src/cart.py": "M", "tests/test_cart.py": "A", "old.txt": "D", "README.md": "M",
    }  # fmt: skip
    assert by_path["old.txt"]["content_b64"] is None
    assert by_path["README.md"]["mode"] == "100755"
    import base64

    assert base64.b64decode(by_path["src/cart.py"]["content_b64"]) == b"MAX = 100\n"
    assert "-MAX = 19" in diff["patch"]
    assert "+MAX = 100" in diff["patch"]
    not_git = c.post(f"/sandbox/v1/sandboxes/{sid}/diff", json={"path": ""}, headers=API_KEY)
    assert not_git.status_code == 422


def test_without_a_runner_commands_are_refused(client: TestClient) -> None:
    sid = sandbox(client)
    refused = client.post(f"/sandbox/v1/sandboxes/{sid}/exec", json={"command": "ls"}, headers=API_KEY)
    assert (refused.status_code, refused.json()["error"]["code"]) == (503, "runner_unavailable")


def test_the_runner_reports_broken_jobs_and_stays_in_the_workspace(tmp_path: Path) -> None:
    import threading

    from fakes import sandbox_runner

    sandbox = tmp_path / "sbx_1"
    (sandbox / "workspace").mkdir(parents=True)
    (sandbox / "workspace" / "out").symlink_to(tmp_path)  # a link out of the workspace
    escaped = sandbox_runner.run(sandbox, {"command": "ls", "cwd": "out"})
    assert (escaped["exit_code"], escaped["stderr"]) == (126, "cwd outside the workspace")
    jobs = sandbox / "jobs"
    jobs.mkdir()
    (jobs / "j1.json").write_text("{not json")
    stop = threading.Event()
    thread = threading.Thread(target=sandbox_runner.loop, args=(tmp_path, stop), daemon=True)
    thread.start()
    result = jobs / "j1.result.json"
    for _ in range(200):
        if result.exists():
            break
        time.sleep(0.02)
    stop.set()
    thread.join(5)
    assert json.loads(result.read_text())["exit_code"] == 125
    assert not (jobs / "j1.running.json").exists()
