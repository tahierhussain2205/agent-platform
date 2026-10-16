"""Checks in code on a change before it is pushed, setup recipes, branch names, the pull request's
description and the GitHub webhook's parsing (no stand-ins needed)."""

import hashlib
import hmac
from typing import Any

from app.agent.changes import check_change, protected, secrets_in, unsafe
from app.agent.implementation import branch_name, pr_body
from app.agent.recipes import PYTEST, detect, outcome
from app.agent.types import ChangeCase, Exported, Implemented, ReviewResult, Verification
from app.api.webhooks import github_notices, github_signature_ok
from app.sandbox import Ran


def change(*files: tuple[str, str, bytes | None], patch: str = "") -> tuple[list[dict[str, Any]], str]:
    out = [{"path": p, "mode": m, "content": c, "status": "A" if c is not None else "D"} for p, m, c in files]
    if not patch:
        patch = "".join(
            f"--- a/{p}\n+++ b/{p}\n"
            + "".join(f"+{line}\n" for line in (c or b"").decode(errors="replace").splitlines())
            for p, _, c in files
        )
    return out, patch


def test_a_clean_change_passes() -> None:
    files, patch = change(("src/cart.py", "100644", b"def total():\n    return 1\n"),
                          ("tests/test_cart.py", "100644", b"def test():\n    pass\n"))  # fmt: skip
    checks = check_change(files, patch, [])
    assert (checks.blocked, checks.ask) == ([], [])
    assert (checks.files, checks.added, checks.removed) == (2, 4, 0)
    assert checks.tests == ["tests/test_cart.py"]


def test_secrets_workflows_unsafe_paths_links_and_binaries_block() -> None:
    cases = {
        "adds what looks like a secret (github token)": change(
            ("src/config.py", "100644", b'TOKEN = "ghp_' + b"a" * 36 + b'"\n')
        ),
        "adds what looks like a secret (aws access key)": change(
            ("a.py", "100644", b"key = 'AKIAABCDEFGHIJKLMNOP'\n")
        ),
        "adds what looks like a secret (secret)": change(
            ("a.py", "100644", b'db_password = "Zx9vQ2mL7pR4tK1w"\n')
        ),
        "is a workflow file": change((".github/workflows/ci.yml", "100644", b"on: push\n")),
        "looks like a secrets file": change(("deploy/.env", "100644", b"A=1\n")),
        "Unsafe path": change(("../outside.py", "100644", b"x = 1\n")),
        "is a symbolic link": change(("link", "120000", b"/etc/passwd")),
        "is a submodule": change(("vendor/lib", "160000", b"")),
        "is a binary file": change(("logo.png", "100644", b"\x89PNG\r\n\x1a\n\0\0\0")),
        "There is no change": ([], ""),
    }
    for reason, (files, patch) in cases.items():
        checks = check_change(files, patch, [])
        assert any(reason in b for b in checks.blocked), (reason, checks.blocked)
    for path in ("a/../b", ".git/config", "src/.git/hooks/x", "/abs", "a//b", "a\\b"):
        assert unsafe(path), path
    # Placeholders and ordinary code are not secrets.
    for line in ('password = "change-me-please"', "token = get_token(request)", 'api_key = os.environ["API_KEY"]',
                 'SECRET = "${SECRET_FROM_ENV}"'):  # fmt: skip
        assert secrets_in(line) == [], line


def test_size_protected_and_generated_paths_ask_for_approval() -> None:
    many = [(f"src/f{i}.py", "100644", b"x = 1\n") for i in range(25)]
    checks = check_change(*change(*many), [])
    assert checks.blocked == []
    assert any("The change is large: 25 files" in a for a in checks.ask)
    files, patch = change(
        ("migrations/010_x.sql", "100644", b"select 1;\n"), ("dist/app.min.js", "100644", b"x\n")
    )
    checks = check_change(files, patch, ["migrations/", "*.tf"])
    assert "migrations/010_x.sql is a protected path." in checks.ask
    assert "dist/app.min.js looks generated or vendored." in checks.ask
    assert protected("infra/main.tf", ["*.tf"])
    assert protected("billing/x.py", ["billing"])
    assert not protected("src/billing.py", ["billing/"])
    # The pusher runs only the blocking checks again.
    assert check_change(files, patch, ["migrations/"], blocking_only=True).ask == []


def test_recipes_are_detected_and_runs_classified() -> None:
    assert detect(["src/pricing/cart.py", "tests/test_cart.py"]) == PYTEST
    assert detect(["package.json", "src/index.ts"], {"scripts": {"test": "vitest"}}) == "npm test --silent"
    assert detect(["package.json"], {"scripts": {}}) is None
    assert detect(["go.mod", "main.go"]) == "go test ./..."
    assert detect(["README.md", "search/index.ts"]) is None
    assert outcome(Ran(0, "3 passed", "", False, 1.0)) == "works"
    assert outcome(Ran(1, "1 failed, 2 passed", "", False, 1.0)) == "failed"
    assert outcome(Ran(127, "", "sh: 1: npm: not found", False, 0.1)) == "unavailable"
    assert outcome(Ran(5, "no tests ran in 0.01s", "", False, 0.1)) == "unavailable"
    assert outcome(Ran(-9, "", "", True, 600)) == "failed"  # a timeout is a failure, not a pass


def test_branches_are_under_agent_and_unique_per_task() -> None:
    task = "0199f0c2-5b0e-7aa1-9c3d-2f6b1e4a9c07"
    assert branch_name("PAY-101", "Checkout returns 500 for carts with 20 or more items", task) == (
        "agent/pay-101-checkout-returns-500-for-carts-with-20-4a9c07"
    )
    assert branch_name(None, "!!!", task) == "agent/change-4a9c07"


def test_the_pull_request_says_what_was_and_was_not_verified() -> None:
    case = ChangeCase("acme/cart-service", "a" * 40, "agent/x", "Fix", {
        "goal": "Fix checkout", "summary": "Carts overflow", "steps": [{"kind": "reproduce", "text": "Test 25 items"}],
        "verification": "The errors stop in the logs"}, "PAY-101")  # fmt: skip
    exported = Exported(
        "id", [{"path": "src/cart.py", "status": "M"}], 3, 4, ask=["The change adds or edits no test."]
    )
    body = pr_body(case, exported, Verification(True, True, "python -m pytest -q", "All tests pass.", reproduced=True),
                   ReviewResult(True, "ok"), Implemented("Sum every item"), "https://slack.test/archives/C1/p1")  # fmt: skip
    assert body.startswith("## Fix checkout")
    assert "### What changed\nSum every item" in body
    assert "The new test fails without the fix and passes with it." in body
    assert "- The change adds or edits no test." in body
    assert "- After merging: The errors stop in the logs" in body
    assert "[Slack thread](https://slack.test/archives/C1/p1) · Issue PAY-101" in body


def test_github_webhooks_are_parsed_and_signatures_checked() -> None:
    body = b'{"x": 1}'
    good = "sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
    assert github_signature_ok("s3cret", body, good)
    assert not github_signature_ok("s3cret", body + b" ", good)
    assert not github_signature_ok("other", body, good)
    assert not github_signature_ok("s3cret", body, None)
    repo = {"full_name": "acme/cart-service"}
    review = {"action": "submitted", "repository": repo, "pull_request": {"number": 3},
              "review": {"id": 9, "state": "CHANGES_REQUESTED", "user": {"login": "bob-gh"}}}  # fmt: skip
    assert github_notices("pull_request_review", review) == [{"kind": "review", "repo": "acme/cart-service", "number": 3,
                                                              "review_id": 9, "state": "CHANGES_REQUESTED",
                                                              "sender": "bob-gh"}]  # fmt: skip
    suite = {"action": "completed", "repository": repo, "check_suite": {"conclusion": "failure",
                                                                         "pull_requests": [{"number": 3}]}}  # fmt: skip
    assert [n["kind"] for n in github_notices("check_suite", suite)] == ["checks_failed"]
    suite["check_suite"]["conclusion"] = "success"  # type: ignore[index]
    assert github_notices("check_suite", suite) == []  # only failures are posted
    merged = {"action": "closed", "repository": repo, "pull_request": {"number": 3, "merged": True}}
    assert github_notices("pull_request", merged)[0]["state"] == "merged"
    assert github_notices("pull_request_review_comment", {"action": "created", "repository": repo}) == []
    assert github_notices("push", {"repository": repo}) == []
