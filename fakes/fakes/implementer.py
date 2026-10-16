"""The Anthropic stand-in's implementer and reviewer (heuristics, not a model).

The implementer is recognised by ``submit_implementation`` among the tools. It works from the
conversation alone (the platform sends ``<plan>``, ``<repository>`` and, for revisions or retries,
``<review>``, ``<pull_request_feedback>`` and ``<verification>``), one step per turn, like a model:

1. read the file the plan names;
2. reproduce first: write a test for the fixture bug, run the tests and see them fail;
3. apply the fix it knows for that bug, run the tests again, and submit.

It knows the fixture bugs (the cart's 19 preallocated price slots, the EU search replica). For a file
it has no fix for, it writes a reproduction test it cannot make pass (when the repository has tests)
and says so. Review comments asking for a docstring are addressed.

The reviewer (forced ``submit_review``) approves a diff that adds a test and a change, and asks for
changes when the diff has no test, adds a failing placeholder, or is empty.
"""

import json
import re
from typing import Any

CART_FIX_OLD = """    prices = [0.0] * MAX_LINES  # preallocated for speed
    for n, item in enumerate(items):
        prices[n] = item.price
    return sum(prices)
"""
CART_FIX_NEW = """    return sum(item.price for item in items)
"""
CART_TEST = """from dataclasses import dataclass

from src.pricing.cart import total


@dataclass
class Item:
    price: float


def test_total_of_twenty_or_more_items():
    assert total([Item(1.5)] * 25) == 37.5
"""

FIXES: list[dict[str, Any]] = [
    {
        "detect": "prices = [0.0] * MAX_LINES",
        "edits": [("MAX_LINES = 19  # BUG: assumes at most 19 items\n\n\n", ""), (CART_FIX_OLD, CART_FIX_NEW)],
        "test": ("tests/test_cart_many_items.py", CART_TEST),
        "summary": "total() no longer fills 19 preallocated price slots: it sums the price of every item, so carts "
        "of 20 or more items are priced. Added a test with 25 items that failed before the fix.",
    },
    {
        "detect": 'const REGIONS = ["us-central1"] // TODO',
        "edits": [
            (
                'const REGIONS = ["us-central1"] // TODO: add the europe-west1 replica for products_v2',
                'const REGIONS = ["us-central1", "europe-west1"]',
            )
        ],
        "test": None,
        "summary": "EU searches use the europe-west1 replica of products_v2 again instead of us-central1.",
    },
]
LINE = re.compile(r"^(\d+): ?(.*)$")
DEF = re.compile(r"^(\s*)def (\w+)\(.*\):\s*$")


def use(name: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {"type": "tool_use", "name": name, "input": payload}


def _tag(text: str, name: str) -> Any:
    m = re.search(rf"<{name}>(.*?)</{name}>", text, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except ValueError:
        return m.group(1).strip()


def _calls(messages: list[dict[str, Any]]) -> list[tuple[str, dict[str, Any], str]]:
    """(tool, input, result text) for every call made so far, in order."""
    made: list[tuple[str, dict[str, Any]]] = []
    ids: dict[str, int] = {}
    results: dict[int, str] = {}
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if block.get("type") == "tool_use":
                ids[str(block.get("id"))] = len(made)
                made.append((str(block.get("name")), dict(block.get("input") or {})))
            elif block.get("type") == "tool_result":
                body = block.get("content")
                text = body if isinstance(body, str) else "\n".join(b.get("text", "") for b in body or [])
                n = ids.get(str(block.get("tool_use_id")))
                if n is not None:
                    results[n] = text
    return [(name, args, results.get(n, "")) for n, (name, args) in enumerate(made)]


def _content(calls: list[tuple[str, dict[str, Any], str]], path: str) -> str | None:
    """The file as the last read of it showed it (the read's numbered lines)."""
    for name, args, result in reversed(calls):
        if name == "github_read_code" and args.get("path") == path and result:
            lines = [m.group(2) for line in result.splitlines()[1:] if (m := LINE.match(line))]
            return "\n".join(lines) + "\n"
    return None


def implement(messages: list[dict[str, Any]], first_user: str, forced: bool) -> list[dict[str, Any]]:
    plan = _tag(first_user, "plan") or {}
    repository = _tag(first_user, "repository") or {}
    repo, command = str(repository.get("repo") or ""), repository.get("test_command")
    feedback = _tag(first_user, "pull_request_feedback") or {}
    comments = [*(_tag(first_user, "review") or []), *(feedback.get("comments") or [])]
    comments += [{"text": r.get("body"), "path": None} for r in feedback.get("reviews") or [] if r.get("body")]
    calls = _calls(messages)
    paths = [str(f["path"]) for f in plan.get("files") or [] if f.get("path") and f.get("repo") in (repo, None)]
    for c in comments:
        if c.get("path") and c["path"] not in paths:
            paths.insert(0, str(c["path"]))
    if forced or not repo or not paths:
        return [use("submit_implementation", _summary(calls, comments, paths))]
    unread = [p for p in paths[:2] if _content(calls, p) is None and not _tried(calls, "github_read_code", p)]
    if unread:
        return [{"type": "text", "text": "Reading the code first."},
                *(use("github_read_code", {"repo": repo, "path": p, "start": 1, "end": 400}) for p in unread)]  # fmt: skip
    steps = _review_steps(calls, comments, paths, repo) if comments else _fix_steps(calls, paths, repo, command)
    if steps:
        return [{"type": "text", "text": "Working on it."}, *steps]
    if command and not _ran_after_last_edit(calls):
        return [use("sandbox_run_command", {"repo": repo, "command": command})]
    return [use("submit_implementation", _summary(calls, comments, paths))]


def _tried(calls: list[tuple[str, dict[str, Any], str]], tool: str, path: str) -> bool:
    return any(name == tool and args.get("path") == path for name, args, _ in calls)


def _ran_after_last_edit(calls: list[tuple[str, dict[str, Any], str]]) -> bool:
    last_edit = max((n for n, (name, _, _) in enumerate(calls) if name in ("sandbox_edit_file", "sandbox_write_file")),
                    default=-1)  # fmt: skip
    return any(name == "sandbox_run_command" for name, _, _ in calls[last_edit + 1 :])


def _fix_steps(
    calls: list[tuple[str, dict[str, Any], str]], paths: list[str], repo: str, command: Any
) -> list[dict[str, Any]]:
    for path in paths:
        content = _content(calls, path) or ""
        fix = next((f for f in FIXES if f["detect"] in content), None)
        if fix is None:
            continue
        if fix["test"] and not _tried(calls, "sandbox_write_file", fix["test"][0]):
            test_path, test = fix["test"]
            return [use("sandbox_write_file", {"repo": repo, "path": test_path, "content": test})]
        edits = [e for e in fix["edits"] if not any(name == "sandbox_edit_file" and args.get("old") == e[0]
                                                    for name, args, _ in calls)]  # fmt: skip
        if edits:
            if fix["test"] and command and not any(name == "sandbox_run_command" for name, _, _ in calls):
                return [use("sandbox_run_command", {"repo": repo, "command": command})]  # see it fail first
            return [
                use("sandbox_edit_file", {"repo": repo, "path": path, "old": old, "new": new}) for old, new in edits
            ]
        return []
    # No fix known: reproduce it when the repository has tests, and say it could not be fixed.
    if command:
        name = re.sub(r"[^a-z0-9]+", "_", paths[0].rsplit("/", 1)[-1].split(".")[0].lower())
        test_path = f"tests/test_reproduce_{name}.py"
        if not _tried(calls, "sandbox_write_file", test_path):
            body = f'def test_reproduces_the_problem():\n    assert False, "{paths[0]}: not fixed yet"\n'
            return [use("sandbox_write_file", {"repo": repo, "path": test_path, "content": body})]
    return []


def _review_steps(
    calls: list[tuple[str, dict[str, Any], str]], comments: list[dict[str, Any]], paths: list[str], repo: str
) -> list[dict[str, Any]]:
    steps = []
    for c in comments:
        text = str(c.get("text") or c.get("body") or "")
        path = str(c.get("path") or paths[0])
        if "docstring" not in text.lower():
            continue
        content = _content(calls, path) or ""
        for line in content.splitlines():
            m = DEF.match(line)
            if m:
                indent, name = m.group(1) + "    ", m.group(2)
                doc = "Return the sum of the items' prices." if name == "total" else f"Document {name}."
                old, new = line + "\n", f'{line}\n{indent}"""{doc}"""\n'
                if not any(n == "sandbox_edit_file" and a.get("old") == old for n, a, _ in calls):
                    steps.append(use("sandbox_edit_file", {"repo": repo, "path": path, "old": old, "new": new}))
                break
    return steps


def _summary(
    calls: list[tuple[str, dict[str, Any], str]], comments: list[dict[str, Any]], paths: list[str]
) -> dict[str, Any]:
    edited = [a.get("path") for n, a, r in calls if n == "sandbox_edit_file" and r.startswith("Edited")]
    written = [str(a.get("path")) for n, a, r in calls if n == "sandbox_write_file" and r.startswith("Wrote")]
    placeholder = any("test_reproduce_" in p for p in written)
    if comments and edited:
        return {"summary": "Addressed the review: added a docstring to total() as asked.", "tests": [], "notes": []}
    fix = next((f for f in FIXES if any(old == a.get("old") for n, a, _ in calls if n == "sandbox_edit_file"
                                        for old, _ in f["edits"])), None)  # fmt: skip
    if fix and not placeholder:
        return {"summary": fix["summary"], "tests": [fix["test"][0]] if fix["test"] else [], "notes": []}
    return {"summary": "I wrote a test that reproduces the problem but could not find a fix." if placeholder
            else "I could not find a change that carries out the plan.",
            "tests": written, "notes": [f"No fix known for {', '.join(paths[:2]) or 'the plan'}"]}  # fmt: skip


def review(user: str) -> dict[str, Any]:
    diff = (re.search(r"<diff>(.*?)</diff>", user, re.DOTALL) or [None, ""])[1] or ""
    files = re.findall(r"^\+\+\+ b/(\S+)", diff, re.MULTILINE)
    if not files:
        return {"verdict": "changes", "summary": "There is no change to review.", "comments": []}
    tests = [f for f in files if re.search(r"(^|/)tests?/|test_[^/]+$", f)]
    if "assert False" in diff:
        return {"verdict": "changes", "summary": "The new test can never pass.",
                "comments": [{"path": tests[0] if tests else files[0], "text": "This test always fails; fix the code."}]}  # fmt: skip
    changed = [line[1:].strip() for line in diff.splitlines()
               if line[:1] in "+-" and not line.startswith(("+++", "---")) and line[1:].strip()]  # fmt: skip
    if changed and all(line.startswith(('"""', "#", "//")) for line in changed):
        return {"verdict": "approve", "summary": "Documentation only; nothing else changes.", "comments": []}
    if not tests:
        return {"verdict": "changes", "summary": "The change has no test.",
                "comments": [{"path": files[0], "text": "Add a test that reproduces the problem."}]}  # fmt: skip
    return {"verdict": "approve", "summary": f"A test reproduces the problem and {files[0]} fixes it; nothing unrelated.",
            "comments": []}  # fmt: skip
