"""The GitHub stand-in's write side: Git Data API, branches, pulls, comments, checks, CI and webhooks."""

import base64
import hashlib
import hmac
import json
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from fakes.config import Config
from fakes.main import create_app
from tests.test_providers import PUBLIC, app_jwt, gh_user_token, install

REPO = "/github/api/repos/acme/cart-service"
SECRET = "test-webhook-secret"


class Receiver:
    def __init__(self) -> None:
        self.received: list[dict[str, Any]] = []
        self.status = 200
        receiver = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                body = self.rfile.read(int(self.headers["content-length"]))
                receiver.received.append({"headers": {k.lower(): v for k, v in self.headers.items()}, "body": body})
                self.send_response(receiver.status)
                self.end_headers()

            def log_message(self, *args: Any) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/webhooks/github"

    def events(self, name: str) -> list[dict[str, Any]]:
        return [json.loads(r["body"]) for r in self.received if r["headers"]["x-github-event"] == name]


@pytest.fixture
def receiver() -> Iterator[Receiver]:
    r = Receiver()
    yield r
    r.server.shutdown()


@pytest.fixture
def client(tmp_path: Path, receiver: Receiver) -> Iterator[TestClient]:
    config = Config(state_dir=tmp_path, seed_fixture=tmp_path / "none.json", github_app_public_key=PUBLIC,
                    github_webhook_url=receiver.url, github_webhook_secret=SECRET)  # fmt: skip
    with TestClient(create_app(config, persist=False), follow_redirects=False) as c:
        c.post("/_admin/github/ci", json={"repository": "acme/cart-service", "delay": 0.05})
        yield c


@pytest.fixture
def app(client: TestClient) -> dict[str, str]:
    setup = install(client)
    token = client.post(f"/github/api/app/installations/{setup['installation_id']}/access_tokens",
                        headers={"authorization": f"Bearer {app_jwt()}"}).json()["token"]  # fmt: skip
    return {"authorization": f"token {token}"}


def user(client: TestClient, login: str) -> dict[str, str]:
    code = install(client, f"{login}|acme")["code"]
    return {"authorization": f"Bearer {gh_user_token(client, code)['access_token']}"}


def head(client: TestClient, auth: dict[str, str], branch: str = "main") -> str:
    sha: str = client.get(f"{REPO}/git/ref/heads/{branch}", headers=auth).json()["object"]["sha"]
    return sha


def commit(client: TestClient, auth: dict[str, str], base: str, files: dict[str, str | None], message: str) -> str:
    tree = client.get(f"{REPO}/git/commits/{base}", headers=auth).json()["tree"]["sha"]
    entries: list[dict[str, Any]] = []
    for path, content in files.items():
        if content is None:
            entries.append({"path": path, "mode": "100644", "type": "blob", "sha": None})
            continue
        blob = client.post(f"{REPO}/git/blobs", headers=auth,
                           json={"content": base64.b64encode(content.encode()).decode(), "encoding": "base64"})  # fmt: skip
        assert blob.status_code == 201
        entries.append({"path": path, "mode": "100644", "type": "blob", "sha": blob.json()["sha"]})
    new_tree = client.post(f"{REPO}/git/trees", headers=auth, json={"base_tree": tree, "tree": entries})
    assert new_tree.status_code == 201, new_tree.text
    made = client.post(f"{REPO}/git/commits", headers=auth,
                       json={"message": message, "tree": new_tree.json()["sha"], "parents": [base]})  # fmt: skip
    assert made.status_code == 201, made.text
    sha: str = made.json()["sha"]
    return sha


def test_a_change_is_pushed_to_a_branch_and_opened_as_a_pull(client: TestClient, app: dict[str, str]) -> None:
    main = head(client, app)
    blob = client.post(f"{REPO}/git/blobs", headers=app, json={"content": "x\n", "encoding": "utf-8"}).json()
    assert blob["sha"] == hashlib.sha1(b"blob 2\0x\n").hexdigest()  # git's blob id
    sha = commit(client, app, main, {"src/pricing/cart.py": "MAX_LINES = None\n", "README.md": None},
                 "Fix carts with 20 items\n\nAgent-Action: k1")  # fmt: skip
    assert client.get(f"{REPO}/git/ref/heads/agent/pay-101", headers=app).status_code == 404
    created = client.post(f"{REPO}/git/refs", headers=app, json={"ref": "refs/heads/agent/pay-101", "sha": sha})
    assert created.status_code == 201
    assert head(client, app, "agent/pay-101") == sha
    assert client.post(f"{REPO}/git/refs", headers=app,
                       json={"ref": "refs/heads/agent/pay-101", "sha": sha}).status_code == 422  # fmt: skip
    # The branch serves the change; main is unchanged.
    content = client.get(f"{REPO}/contents/src/pricing/cart.py", params={"ref": "agent/pay-101"}, headers=app).json()
    assert base64.b64decode(content["content"]) == b"MAX_LINES = None\n"
    assert client.get(f"{REPO}/contents/README.md", params={"ref": "agent/pay-101"}, headers=app).status_code == 404
    assert client.get(f"{REPO}/contents/README.md", headers=app).status_code == 200
    detail = client.get(f"{REPO}/commits/{sha}", headers=app).json()
    assert {f["filename"]: f["status"] for f in detail["files"]} == {"README.md": "removed",
                                                                     "src/pricing/cart.py": "modified"}  # fmt: skip
    assert detail["author"]["login"] == "agent-platform-local[bot]"
    # The pull: opened once per head, found by head, its files are the change.
    opened = client.post(f"{REPO}/pulls", headers=app,
                         json={"title": "Fix carts", "head": "agent/pay-101", "base": "main", "body": "Plan"})  # fmt: skip
    assert opened.status_code == 201
    pr = opened.json()
    assert (pr["number"], pr["head"]["sha"], pr["user"]["login"]) == (1, sha, "agent-platform-local[bot]")
    again = client.post(f"{REPO}/pulls", headers=app, json={"title": "Fix carts", "head": "agent/pay-101"})
    assert again.status_code == 422
    assert "already exists" in again.json()["errors"][0]["message"]
    found = client.get(f"{REPO}/pulls", params={"head": "acme:agent/pay-101", "state": "all"}, headers=app).json()
    assert [p["number"] for p in found] == [1]
    assert client.get(f"{REPO}/pulls", params={"head": "acme:other"}, headers=app).json() == []
    files = client.get(f"{REPO}/pulls/1/files", headers=app).json()
    assert [f["filename"] for f in files] == ["README.md", "src/pricing/cart.py"]
    page = client.get("/github/acme/cart-service/pull/1")
    assert "Fix carts" in page.text
    assert "MAX_LINES = None" in page.text
    pushes = client.get("/_admin/github/repos/acme/cart-service").json()["pushes"]
    assert [(p["ref"], p["before"], p["forced"]) for p in pushes] == [("refs/heads/agent/pay-101", None, False)]


def test_branches_move_forward_only_and_main_is_protected(client: TestClient, app: dict[str, str]) -> None:
    main = head(client, app)
    first = commit(client, app, main, {"a.txt": "1\n"}, "one")
    client.post(f"{REPO}/git/refs", headers=app, json={"ref": "refs/heads/agent/x", "sha": first})
    second = commit(client, app, first, {"a.txt": "2\n"}, "two")
    sideways = commit(client, app, main, {"b.txt": "1\n"}, "sideways")
    assert client.patch(f"{REPO}/git/refs/heads/agent/x", headers=app, json={"sha": second}).status_code == 200
    refused = client.patch(f"{REPO}/git/refs/heads/agent/x", headers=app, json={"sha": sideways})
    assert (refused.status_code, refused.json()["message"]) == (422, "Update is not a fast forward")
    assert head(client, app, "agent/x") == second
    for attempt in (
        client.patch(f"{REPO}/git/refs/heads/main", headers=app, json={"sha": first}),
        client.patch(f"{REPO}/git/refs/heads/main", headers=app, json={"sha": first, "force": True}),
        client.delete(f"{REPO}/git/refs/heads/main", headers=app),
        client.post(f"{REPO}/git/refs", headers=app, json={"ref": "refs/heads/main", "sha": first}),
    ):
        assert attempt.status_code == 422
    assert head(client, app) == main
    # A force push is possible on other branches (our pusher never asks for one) and is recorded.
    assert client.patch(f"{REPO}/git/refs/heads/agent/x", headers=app,
                        json={"sha": sideways, "force": True}).status_code == 200  # fmt: skip
    assert client.get("/_admin/github/repos/acme/cart-service").json()["pushes"][-1]["forced"] is True


def test_the_app_cannot_change_workflows_or_write_malformed_paths(client: TestClient, app: dict[str, str]) -> None:
    main = head(client, app)
    workflow = commit(client, app, main, {".github/workflows/ci.yml": "on: push\n"}, "add a workflow")
    refused = client.post(f"{REPO}/git/refs", headers=app, json={"ref": "refs/heads/agent/ci", "sha": workflow})
    assert refused.status_code == 422
    assert "without `workflows` permission" in refused.json()["message"]
    tree = client.get(f"{REPO}/git/commits/{main}", headers=app).json()["tree"]["sha"]
    for path in ("../escape", ".git/config", "/abs", "a//b", "src/./x"):
        bad = client.post(f"{REPO}/git/trees", headers=app,
                          json={"base_tree": tree, "tree": [{"path": path, "mode": "100644", "content": "x"}]})  # fmt: skip
        assert bad.status_code == 422, path
    submodule = client.post(f"{REPO}/git/trees", headers=app, json={
        "base_tree": tree, "tree": [{"path": "vendor/x", "mode": "160000", "type": "commit", "sha": main}]})  # fmt: skip
    assert submodule.status_code == 422


def test_only_writers_write(client: TestClient, app: dict[str, str]) -> None:
    main = head(client, app)
    quinn = user(client, "quinn-gh")  # read access to cart-service
    bob = user(client, "bob-gh")  # write access
    blob = {"content": "x", "encoding": "utf-8"}
    assert client.post(f"{REPO}/git/blobs", headers=quinn, json=blob).status_code == 403
    assert client.post(f"{REPO}/git/blobs", headers=bob, json=blob).status_code == 201
    assert client.post("/github/api/repos/acme/infra/git/blobs", headers=bob, json=blob).status_code == 404
    assert client.post("/github/api/repos/globex/payments/git/blobs", headers=app, json=blob).status_code == 404
    assert client.post(f"{REPO}/git/blobs", json=blob).status_code == 401
    assert client.post(f"{REPO}/pulls", headers=quinn, json={"title": "t", "head": "main"}).status_code == 403
    assert head(client, app) == main


def test_ci_runs_on_pushes_and_reports_by_webhook(client: TestClient, app: dict[str, str], receiver: Receiver) -> None:
    main = head(client, app)
    first = commit(client, app, main, {"a.txt": "1\n"}, "one")
    client.post("/_admin/github/ci", json={"repository": "acme/cart-service", "conclusion": "failure",
                                           "failure_summary": "1 failed: test_many_items"})  # fmt: skip
    client.post(f"{REPO}/git/refs", headers=app, json={"ref": "refs/heads/agent/ci", "sha": first})
    running = client.get(f"{REPO}/commits/{first}/check-runs", headers=app).json()["check_runs"]
    assert [(r["name"], r["status"]) for r in running] == [("test", "in_progress")]
    client.post(f"{REPO}/pulls", headers=app, json={"title": "t", "head": "agent/ci"})  # opened before CI ends
    assert client.post("/_admin/github/deliveries/drain").json() == {"undelivered": 0}
    done = client.get(f"{REPO}/commits/agent/ci/check-runs", headers=app).json()["check_runs"]
    assert [(r["status"], r["conclusion"], r["output"]["summary"]) for r in done] == [
        ("completed", "failure", "1 failed: test_many_items")]  # fmt: skip
    [suite] = receiver.events("check_suite")
    assert suite["check_suite"]["conclusion"] == "failure"
    assert suite["check_suite"]["head_branch"] == "agent/ci"
    assert [p["number"] for p in suite["check_suite"]["pull_requests"]] == [1]
    assert suite["repository"]["full_name"] == "acme/cart-service"
    assert suite["installation"]["id"] > 0
    raw = receiver.received[0]
    expected = "sha256=" + hmac.new(SECRET.encode(), raw["body"], hashlib.sha256).hexdigest()
    assert raw["headers"]["x-hub-signature-256"] == expected
    assert raw["headers"]["x-github-delivery"]
    # The next push succeeds (the queue was used up); main never runs CI here.
    second = commit(client, app, first, {"a.txt": "2\n"}, "two")
    client.patch(f"{REPO}/git/refs/heads/agent/ci", headers=app, json={"sha": second})
    client.post("/_admin/github/deliveries/drain")
    assert [e["check_suite"]["conclusion"] for e in receiver.events("check_suite")] == ["failure", "success"]


def test_reviews_comments_and_redelivery(client: TestClient, app: dict[str, str], receiver: Receiver) -> None:
    main = head(client, app)
    sha = commit(client, app, main, {"a.txt": "1\n"}, "one")
    client.post(f"{REPO}/git/refs", headers=app, json={"ref": "refs/heads/agent/r", "sha": sha})
    client.post(f"{REPO}/pulls", headers=app, json={"title": "t", "head": "agent/r"})
    reviewed = client.post("/_admin/github/review", json={
        "repository": "acme/cart-service", "number": 1, "login": "bob-gh", "state": "changes_requested",
        "body": "Close, two things", "comments": [{"path": "a.txt", "line": 1, "body": "Use 2 here"}]})  # fmt: skip
    assert reviewed.status_code == 200
    [review] = client.get(f"{REPO}/pulls/1/reviews", headers=app).json()
    assert (review["state"], review["user"]["login"], review["body"]) == ("CHANGES_REQUESTED", "bob-gh",
                                                                          "Close, two things")  # fmt: skip
    [inline] = client.get(f"{REPO}/pulls/1/comments", headers=app).json()
    assert (inline["path"], inline["line"], inline["pull_request_review_id"]) == ("a.txt", 1, review["id"])
    said = client.post(f"{REPO}/issues/1/comments", headers=app, json={"body": "Pushed a revision"})
    assert said.status_code == 201
    assert [c["body"] for c in client.get(f"{REPO}/issues/1/comments", headers=app).json()] == ["Pushed a revision"]
    assert client.get(f"{REPO}/pulls/9/comments", headers=app).status_code == 404
    [submitted] = receiver.events("pull_request_review")
    assert submitted["review"]["state"] == "CHANGES_REQUESTED"
    assert submitted["pull_request"]["number"] == 1
    assert [c["comment"]["body"] for c in receiver.events("pull_request_review_comment")] == ["Use 2 here"]
    # Redelivery sends the same delivery id again; a failed delivery is recorded, not retried.
    first = client.get("/_admin/github/deliveries").json()[0]
    receiver.status = 500
    again = client.post(f"/_admin/github/deliveries/{first['id']}/redeliver").json()
    assert again["attempts"][-1] == {"status": 500}

    def ids() -> list[str]:
        return [r["headers"]["x-github-delivery"] for r in receiver.received]

    assert ids().count(first["id"]) == 2
    time.sleep(0.3)
    assert ids().count(first["id"]) == 2  # no automatic retry after the 500
