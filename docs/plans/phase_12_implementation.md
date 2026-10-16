# Phase 12: Implementation and pull requests (Example 2, part 2)

**Goal:** the agent implements an approved plan and raises a PR safely (spec 3.3 steps 4 and 5, section 8).

**Done when:** Example 2 works end to end on a test repository, and a red team test confirms the sandbox cannot read push credentials or reach the control plane. Staging, a managed sandbox provider and a real model are blocked (B-001, B-008, B-005): this runs on the local stack, with a sandbox runner that has no network at all.

## Decisions up front

- **The coding harness runs in the worker, not in the sandbox.** The implementer is a subagent in our worker whose tools act in the sandbox (read, search, write, edit, run a command, show the diff) through the provider API. No model key, proxy or credential exists inside the sandbox, so its egress can be denied entirely (spec 8.2 rules 1 and 6). The plan's direction (the Claude Agent SDK inside the sandbox, model calls through our proxy) needs egress to a proxy and a real model to be worth testing (B-005, B-008); it is recorded as a later option, not built now.
- **Local sandbox runner (B-008):** commands run in a separate `sandbox-runner` container with `network_mode: none`, a read only root, no capabilities, CPU, memory and process limits, as a non root user. The stand-in provider API hands it jobs through a shared volume. Isolation between sandboxes inside that one runner is not provided (a managed provider gives each sandbox its own machine); the red team test covers credentials and the control plane.

## Sandbox

- Provider API additions (stand-in and client): `exec` (a command with arguments, working directory, environment and timeout; output capped), `write` (a file's content) and `diff` (the change against the baseline, as a patch and as changed file contents).
- When a sandbox is built, the platform makes a baseline commit inside it (`git init`, add, commit) so the change can be exported exactly.
- **Setup recipe per repository** (`repositories.setup_recipe`): detected from the repository's files the first time (for example `python -m pytest -q` for a Python project with tests), saved with what happened when it ran. A recipe that worked is reused; one that could not run is reported. Snapshots are not supported by the stand-in provider (recorded, not built).

## Playbook `implement_change` v1

Classified from requests like "implement the plan", "go ahead and fix it", "raise a PR", in a thread whose session has a current plan; and from "address the review comments" or "fix CI" in a thread whose task opened a PR.

1. **Prepare**, in code: the current plan; the repository; the base commit (the default branch's head for a new change, the PR's head for a revision); the branch (`agent/<issue or task>-<slug>`, always under the `agent/` prefix); for a revision, the review comments and failing checks.
2. **Implement**: a sandbox at the base commit; the implementer subagent with read tools and the sandbox tools (write, edit, run, diff). Reproduce first: it is told to add a failing test before the fix. Code then runs the recipe's tests (verification). A failed verification goes back to the implementer with the output; when verification fails twice the task stops, reports what it tried, and asks for the plan to be revised (spec 7.3), without a PR.
3. **Review**: a reviewer subagent sees only the plan and the diff (never the implementer's reasoning, spec 7.4) and approves or asks for changes; one round of changes goes back to the implementer, then verification again.
4. **Export**: the diff and the changed files leave the sandbox; the diff and base commit are stored (artifact `diff`, in the object store when large); the sandbox is destroyed. The workspace can be rebuilt from them.
5. **Checks** on the diff, in code: size (files and lines), protected paths (the repository's list plus `.github/workflows/`), secret scan of added lines, binary and generated files, path safety (no `..`, `.git/`, links or submodules). Secrets, workflow files and unsafe paths block the push; size and protected paths require approval even in Auto.
6. **Push**, as an action through the policy (a reversible write: Ask asks, Auto proceeds within the ceiling): the trusted pusher outside the sandbox creates blobs, a tree and a commit on top of the base through the GitHub App, and creates or fast forwards the branch (never the default branch, never a force push); then it opens the PR (or finds it) with the plan, what was verified and what was not, and links to the thread and the issue. Idempotent: the commit carries the action's key, and the branch and PR are looked up first. The PR is linked to the task (`task_links`).
7. **Report** in the thread: the PR link, the files changed, the checks, and what was and was not verified.

## Feedback from GitHub

- `POST /webhooks/github`: the signature (`X-Hub-Signature-256`) is verified, the delivery claimed once, and the event mapped to its task through `task_links`: a check suite that failed, a review, or a review comment on a PR the agent opened is posted in the task's thread. The engineer asks for a revision there.
- New GitHub reads: a PR with its review comments and check runs.

## Stand-ins

- GitHub: the app installation has `contents: write`, `pull_requests: write`, `checks: read` and no `workflows` permission; Git Data API (blobs, trees, commits, refs) with GitHub's refusal to change workflow files without that permission and the default branch protected; pulls (create, list by head, get), review and issue comments, check runs; CI simulated per repository (an admin route sets the next conclusion) with signed webhook deliveries (check suites, reviews, review comments); an admin route posts a reviewer's comment.
- Sandbox runner container and the provider API additions.
- The stand-in model plays the implementer (reads the plan's file, adds a test, applies the fix the plan names for the fixture bugs) and the reviewer.

## Console

- Repositories: each repository's protected paths (editable by admins) and its setup recipe.

## Tests

- Stand-ins: Git Data API, refs (prefix rules belong to our pusher; the stand-in protects the default branch and workflow files), pulls, comments, check runs, webhooks and signatures, CI simulation; the runner's jobs, limits and lack of network.
- Sandbox: exec, write, diff, baseline, output caps, timeouts.
- Diff checks: each rule blocks or asks, with reasons.
- Pusher: blobs, tree and commit on the base, branch prefix, never the default branch, fast forward only, idempotent retries (a lost answer finds its commit), the requester's write access checked, the PR found or opened and linked.
- Playbook: implement, verify, review, export, checks, push in Ask (approve, reject) and Auto, verification failing twice, a revision from review comments, tests that cannot run reported.
- Webhooks: signature, replay, mapping to the task, notices in the thread, unknown PRs ignored.
- Red team: from inside the sandbox, no token in the environment or files, and no connection to the API, the vault, Postgres, Valkey, Temporal, the stand-ins or the metadata endpoint.
- Console API and web tests; Playwright: Example 2 continued: "implement the plan" in the thread, approve the push in Slack, the PR appears with the change, a review comment comes back to the thread, a revision is pushed.

## Changes during implementation

- The diff artifact is inline up to 200 KB and in the object store above that (the worker now builds the store from its settings); a change over 5 MB is refused as too large to push.
- `github.prepare_workspace` checks the requester's push access before building the working copy (with `push: true`), so a person who cannot push gets an answer before any work; the pusher checks again.
- Branch names end with the task id's last characters: two tasks for the same issue never share a branch.
- Review comment events are not posted on their own: every comment belongs to a review, whose event carries them (GitHub sends both). Merged and closed pull requests are posted too; passing checks are not.
- Notices read the pull request outside the task that opened it: the vault releases credentials to running tasks only, and that task has ended. The requester's own access is still checked.
- A revision that adds no test (for example a docstring a reviewer asked for) asks a person even in Auto, like any change without a test.
- Recipe runs are classified `works`, `failed` or `unavailable` (could not run); an unavailable detected recipe is detected again next time, an admin's is kept.
- The stand-in provider got a raw file read (`GET .../files/raw`) so exact edits never see truncated lines.
