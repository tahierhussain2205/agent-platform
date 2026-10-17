# Demo flow test log

The demo, end to end on staging (Slack workspace "Agent Platform", channel #agent-test; Jira project
KAN on tahierhussain2205.atlassian.net; repository `tahier-agent-lab/cart-service`; sandboxes at E2B):

1. A Slack message describes a couple of issues.
2. `@agent` is asked to create Jira tickets from that message.
3. `@agent` is asked for a detailed plan to address those tickets.
4. `@agent` is asked to implement the changes, commit and push them to a new branch, and raise a PR.
5. The tickets' statuses follow the work as it happens.

Each run is recorded below with what was seen; every gap, bug, improvement and UI/UX issue gets an
entry in the issue list, which is kept up to date (status **open**, **fixed** with the commit, or
**won't fix** with the reason).

Demo data: the two issues are real bugs in `cart-service`:
`src/pricing/cart.py` keeps only the first 19 lines (`MAX_LINES = 19`), so large carts are
undercharged; `total()` takes any discount, so a promotion above 100% makes a total negative.

## Issues

| # | Step | Kind | Issue | Status |
|---|---|---|---|---|
| 1 | 5 | gap | The agent cannot change a Jira ticket's status at all: there is no transition operation, so statuses never follow the work. | fixed (run 2 checks it): `jira.transition_issue` (internal, a reversible write with the requester's account); the change moves its tickets to In Progress when it starts (Auto: the policy allows it without asking; Ask skips it) and to In Review with a comment linking the pull request once the push succeeds (approved with the push, whose request lists them) |
| 2 | 3 | gap | A plan covers one ticket: orientation takes the first issue key that resolves, and the demo creates two. | fixed: the case holds up to three issues; investigators and the plan see them all; the plan stores them and implementation carries every key (branch from the first, PR title and body name all) |
| 3 | 3 | gap | "Plan for these tickets" names no keys: the tickets the agent created earlier in the thread are not found (the agent skips its own messages and orientation only reads keys from the request or a previous plan). | fixed: with no key in the request and no earlier plan, orientation takes the tickets earlier tasks of the thread created (`external_actions`) |
| 4 | 2 | bug | With no project named in the request and none on the matched catalog service, every proposed ticket is dropped ("No Jira project for …: name the project and ask again") although the requester can browse only a couple of projects and the service's repository is known. The message does not say which projects exist. | fixed: the requester's only browsable project is used; otherwise the message names the projects |
| 5 | 2 | config | The demo's catalog service `cart-service` had no Jira project (catalog proposals from GitHub CODEOWNERS never set one). Set to KAN with #agent-test as its channel in the console (Catalog, Edit). | fixed (staging data) |
| 6 | 2 | UX | The answer to "create Jira tickets" is a long findings report (ownership aside about a "payments team lead", a "Not covered" paragraph) when the person wants to know which tickets will be made; the ticket notes come last. | fixed: ticket requests have their own short answer prompt (two or three sentences; the tickets follow, listed by code) |
| 7 | 2,3 | UX | "Not verified" repeats what the answer already said (the model's own list plus ours), and lists doubts like "No existing open Jira issues were confirmed via a project-scoped search". | fixed: code lists the gaps once ("Not covered: …"), the prompt says not to repeat them, model doubts that repeat a gap are dropped; a ticket answer keeps none |
| 8 | CI | test | `test_ask_requests_approvals_together_and_creates_what_is_approved` timed out waiting for its two approvals once in CI (phase 13 run of d2a4e0c) and passes locally and on main: flaky under CI load. | fixed: the wait is 180 s |
| 9 | 2 | UX | Approval requests say "KAN has no component cart-service, so the ticket has none": the model put the service's name in the component field and the approver reads about a field nobody asked for. | fixed: a missing component is mentioned only when the request named it |
| 10 | 2 | bug | The second ticket's assignee evidence was "named in the request": the requester was named as the reporter, not as an assignee. The catalog match missed that ticket because its summary shares no word with `cart-service`; the repository named in the request was not passed to `catalog_find_owners`. | fixed: the repository named in the request goes to `catalog_find_owners`; the requester is never "named in the request" as an assignee |
| 11 | 2 | UX | The ticket answer calls the tickets "candidates for new tickets" right above "Created KAN-6 / KAN-7" (the rule against claiming changes makes the model hedge); the outcome comes last. | fixed with #6 |
| 12 | 3 | UX | The plan's title is the request echoed: "Plan: Create a detailed plan to address two checkout issues…". | fixed: "Plan for KAN-6, KAN-7: <what the change achieves>" (linked keys; the prompt asks for the outcome, not the request) |
| 13 | 3 | bug | The plan's step 6 is "Create the two Jira tickets … capture the resulting Jira keys": they exist already (KAN-6, KAN-7), see #3. | fixed: the plan sees the tickets and is told they exist |
| 14 | 4 | bug | "No sandbox is configured, so I can't change or test code safely" on staging: `build_registry` offers the sandbox tools only when `SANDBOX_URL` is set, and E2B has no URL. `app check sandbox` passed because it uses the provider directly. Blocks step 4. | fixed: `sandbox_configured` decides (E2B needs only its key); `app check sandbox` now fails when the agent would not offer the tools |

## Runs

### Run 1 (2026-10-17, staging at 7e29262)

- Step 1: posted the two problems in #agent-test (Slack turns "1." into a list item).
- Step 2: "@agent create Jira tickets for these issues" in its thread: done in about 60 s with no tickets: both dropped for want of a project (#4); the answer was long (#6, #7). Fixed the catalog (#5) and asked again.
- Step 2 again: two approval requests in about 60 s ("Waiting for 2 approvals"), approved; KAN-6 and KAN-7 created as Tasks, To Do, assigned to Tahier Hussain, with the Slack link (#9, #10, #11). An `agent-…` label marks each ticket: it is what makes a retried write find the ticket instead of creating another, so it stays.
- Step 3: "@agent come up with a detailed plan to address these tickets": a plan in 69 s ("Understanding", "Investigating logs, code and recent changes", "Writing the plan"). It found both bugs in `src/pricing/cart.py` from the code and history, with tests to write first, fixes, verification and rollback; but it does not know KAN-6 and KAN-7 (#2, #3, #12, #13).
- Step 4: "@agent implement the changes, commit and push them to a new branch, and raise a PR": "Nothing was changed. No sandbox is configured" within 9 s (#14). Stopped here.
- Step 5: KAN-6 and KAN-7 stayed To Do (#1).
