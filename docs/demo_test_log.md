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
| 15 | 2 | bug | Only one ticket was made: the model saw KAN-7 (Done) for the discount bug and proposed none, and the answer said "no new ticket was needed". A closed ticket does not cover a problem reported again. (Run 2's data made this likely: run 1's tickets had been moved to Done.) | fixed (checked in run 3) |
| 16 | 2 | bug | Our citation refs leak into Jira: the ticket description ends with "[S1]". | fixed (checked in run 3) |
| 17 | 2 | bug | "KAN has no component cart-service" is still shown: the request names the repository `tahier-agent-lab/cart-service`, which my check took as asking for that component. | fixed (checked in run 3) |
| 18 | 2 | UX | The short ticket answer still ends with "Not verified: No existing Jira issue found for … in project KAN" (a finding without a source). | fixed (checked in run 3) |
| 19 | 2 | test | The first click on Approve, right as the request appeared, never reached Slack (no interaction arrived); the second did. The harness now waits and checks that the status changed. Not a platform issue. | won't fix (harness) |
| 20 | 4 | UX | The change's report lists under "Not verified" what was verified ("Confirmed all three new tests fail before the fix … pass after it") and "Did not create or update any Jira ticket (e.g. KAN-8)" right below "KAN-8 moved to In Review". | fixed (checked in run 3) |
| 21 | 3,4 | UX | The plan echoes my prompt: "(tickets are not to be created by this plan)", and the pull request's plan section repeats it. | fixed (checked in run 3) |
| 22 | 4 | UX | The push request says "Then KAN-8 moves to In Review, each with a link to the pull request" for one ticket. | fixed (checked in run 3) |
| 23 | 5 | UX | The Jira comment reads "The agent opened a pull request for this issue: <link> via the agent · agent-…". | fixed (checked in run 3) |
| 24 | 5 | note | In Ask mode the tickets go from To Do straight to In Review when the push is approved (In Progress is skipped: moving it then would need an approval of its own, D-198). In Auto they show In Progress as the work starts. | by design (choose the mode for the demo) |
| 25 | 5 | gap | Auto was impossible on staging: without an autonomy ceiling every task runs in Ask, and the console had no way to set one (spec 4.5: admins set the ceiling per org, channel or repository). | fixed: Settings has an Autonomy card (Ask before every change / Let people choose Auto), saved at once and audited; people switch in the agent's Home tab in Slack |
| 26 | 3 | note | Run 3's plan has a step "Reconcile with the Jira history: since KAN-6/KAN-7/KAN-8 were marked Done …": the earlier test runs' tickets for the same problems. A fresh Jira project has none. | won't fix (test data) |
| 27 | 3 | bug | A plan came back empty and was posted that way: "Plan for KAN-11, KAN-12: · confidence low" with no goal, evidence or steps (the model's call carried none of the plan's fields; asked again, it gave a full plan). | fixed: an empty plan is asked for once more, then the agent says it could not write one instead of posting an empty plan; the schema repair also unwraps fields put under a key of the model's own; the shape is logged (names and types only) |
| 28 | 3 | UX | "Not covered" lines from the investigators carry our refs ("… [L1]") and tool names ("confirmed via catalog_find_owners lookup"). | fixed: refs are stripped from gaps, and the investigators are told points and gaps are read by people |
| 29 | CI | test | The Slack stand-in's `test_rate_limits` failed on main once: one call a second, and its two calls straddled a second. | fixed: three later calls, at least one limited |

## Runs

### Run 1 (2026-10-17, staging at 7e29262)

- Step 1: posted the two problems in #agent-test (Slack turns "1." into a list item).
- Step 2: "@agent create Jira tickets for these issues" in its thread: done in about 60 s with no tickets: both dropped for want of a project (#4); the answer was long (#6, #7). Fixed the catalog (#5) and asked again.
- Step 2 again: two approval requests in about 60 s ("Waiting for 2 approvals"), approved; KAN-6 and KAN-7 created as Tasks, To Do, assigned to Tahier Hussain, with the Slack link (#9, #10, #11). An `agent-…` label marks each ticket: it is what makes a retried write find the ticket instead of creating another, so it stays.
- Step 3: "@agent come up with a detailed plan to address these tickets": a plan in 69 s ("Understanding", "Investigating logs, code and recent changes", "Writing the plan"). It found both bugs in `src/pricing/cart.py` from the code and history, with tests to write first, fixes, verification and rollback; but it does not know KAN-6 and KAN-7 (#2, #3, #12, #13).
- Step 4: "@agent implement the changes, commit and push them to a new branch, and raise a PR": "Nothing was changed. No sandbox is configured" within 9 s (#14). Stopped here.
- Step 5: KAN-6 and KAN-7 stayed To Do (#1).

### Run 2 (2026-10-17, staging at 56543ae, Ask mode)

- Step 1: posted the two bugs in #agent-test.
- Step 2: an approval in 39 s, for one ticket only (#15, #16, #17); approved (the harness's first click was lost, #19); KAN-8 created; the answer is two sentences now (#6 fixed), with a stray "Not verified" (#18).
- Step 3: "Plan for KAN-8: Count every cart line and refuse discounts over 100%" in 67 s (#2, #3, #12 fixed): it found the thread's ticket by itself and covers both bugs (#21).
- Step 4: Preparing, Implementing, Reviewing; the push request in 54 s: 2 files (+25 −4), "Verified: All tests pass", "Then KAN-8 moves to In Review" (#22). Approved: pull request tahier-agent-lab/cart-service#1 "KAN-8: Count every cart line and refuse discounts over 100%" in 13 s, on `agent/kan-8-count-every-cart-line-and-refuse-423278`; the new tests fail without the fix and pass with it; the reviewer approved (#20).
- Step 5: KAN-8 moved to In Review with a comment linking the pull request (#1 fixed; #23, #24).

### Run 3 (2026-10-17, staging at d901097, Ask mode)

- Step 1: posted the two bugs in #agent-test.
- Step 2: "Waiting for 2 approvals" in 45 s: both tickets (#15 fixed), assignees from the catalog (#10), no component note (#17); approved both; KAN-9 and KAN-10 created, descriptions clean (#16), the answer short with no "Not verified" (#18). Each says the problem was tracked before (KAN-6/7/8, Done) and is reported again.
- Step 3: "Plan for KAN-9, KAN-10: Count every cart line and refuse discounts over 100%" in 76 s, covering both tickets (#2); no echo of the instructions (#21).
- Step 4: Understanding, Preparing, Implementing, Running the tests, Reviewing, Checking; the push request in 48 s ("Then KAN-9, KAN-10 move to In Review, each with a link to the pull request"); approved; pull request tahier-agent-lab/cart-service#2 "KAN-9, KAN-10: Count every cart line and refuse discounts over 100%" 19 s later; the report says what changed, the tests (the new test fails without the fix), the review, and the tickets, with nothing contradictory (#20).
- Step 5: KAN-9 and KAN-10 In Review, each with "Pull request opened: <link>" (#23).

### Run 4 (2026-10-17, staging at d901097, Auto mode)

Settings, Autonomy, "Let people choose Auto" in the console (#25: saved and kept after a reload); the agent's Home tab then said "Your admins allow up to auto", and "Auto: act within my access" switched the mode.

- Step 1: posted the two bugs in #agent-test.
- Step 2: done in 58 s with no approval: KAN-11 and KAN-12 created.
- Step 3: the plan came back empty (#27, #28); asked again: "Plan for KAN-11, KAN-12: Count every cart line and refuse discounts over 100%" in 71 s.
- Step 4: "KAN-11, KAN-12 moved to In Progress." in the thread at 18 s (Jira said In Progress then); Implementing, Reviewing, Checking; no approval; pull request tahier-agent-lab/cart-service#3 at 67 s.
- Step 5: KAN-11 and KAN-12 In Review, each with "Pull request opened: <link>". To Do, In Progress, In Review, as the work happened.

