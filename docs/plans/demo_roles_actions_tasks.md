# Demo features: role profiles, role limits, answer buttons, tasks page

Four additions for the demo (D-201 to D-205). Each one lands on its own, with its tests, and goes to
staging as soon as it passes (`git push origin HEAD:main HEAD:phase-13-production`).

Order: Tasks page first (it shows what the other three do), then role profiles and preview, then
role limits and approval routing, then the buttons on answers.

## 1. Tasks page (D-201)

What admins and members see: every task the agent ran, and for one task a timeline of what it did.

**Data.** Migration `012_roles_and_tasks.sql` adds `usage_events.purpose text` (nullable, at most
64 characters). The gateway writes the call's purpose (`classify`, `brief`, `plan`, `compose`,
`implement`...). Nothing else is new: the timeline is read from what tasks already write.

**API** (`app/api/console/tasks.py`, any active member; admins see every task, members only theirs):

- `GET /api/tasks?status=&limit=&before=`: id, created_at, started_at, finished_at, status,
  trigger, effective_mode, playbook (key, title), title (the brief's `goal`, a derived artifact,
  never message text), requester (email), channel name, tokens_used, cost.
- `GET /api/tasks/{id}`: the task, its links (tickets, pull requests from `external_actions` and
  `task_links`), a Slack link to the thread (`app.slack.com/client/<team>/<channel>/thread/...`),
  and `events`, merged and ordered by time:
  - steps (`task_steps`: gather, plan, act, verify, report with their summaries),
  - policy decisions (`audit_log` `policy.tool` / `policy.output`: target, decision, reason),
  - approvals (requested, who may decide, decided by whom, or expired),
  - external writes (`external_actions`: operation, status, target),
  - model calls (`usage_events`: purpose, model, tokens, cost),
  - artifacts (brief, plan, summary: kind and a one line summary).
  Another org's task, or someone else's for a member, is a 404.

**Console.** `Tasks` in the nav for everyone. `/tasks`: a table (status badge, title, playbook,
requester, mode, started, duration, tokens), status filter. `/tasks/:id`: header (title, status,
playbook, mode, tokens and cost, links to the Slack thread, tickets and the pull request), then the
timeline with an icon and tone per event kind. Both themes, phone widths (`ui.spec.ts` PAGES).

**Tests.** API: admin sees all, member sees own only, other org 404, detail has each event kind
from a seeded task, purpose recorded by the gateway. Web: list and detail render, filter, empty
state. e2e: the page in `ui.spec.ts`.

## 2. Structured role profiles and "Preview as role" (D-202)

Roles today are a title and free text. A profile makes them concrete and predictable.

**Data** (same migration): `custom_roles.profile jsonb not null default '{}'` (an object, at most
8 KB). Fields, validated in code (`app/agent/roles.py`):

- `detail`: `brief` | `balanced` | `thorough`
- `style`: `plain` | `technical` | `business`
- `format`: `bullets` | `paragraphs` | `summary_first`
- `focus`: any of `risks`, `next_steps`, `owners`, `dates`, `code`, `customer_impact`, `metrics`
- the existing `description` stays as "anything else".

**Prompt.** `prompts.role_section` renders each role as fixed sentences built from the enums
("Keep answers brief: at most five bullets.", "Lead with risks and next steps.") plus the cleaned
description, still bounded, primary first, and still "only tone, depth and format". Enums mean the
prompt text is ours; only the description is free text (cleaned and capped as today).

**Preview.** In the role editor, "Preview" runs one sample request against a fixed sample thread
(code owned: a checkout incident with two bugs) twice, without any role and with the role being
edited (unsaved values), and shows the answers side by side.

- `POST /api/roles/preview` (admins) with the profile and a request (one of three samples or
  typed, at most 300 characters) starts `RolePreviewWorkflow` on the worker (the API has no model
  key) with id `role-preview-<org>-<random>` and returns the id. `GET /api/roles/preview/{id}`
  answers `running` or the two answers. The id must carry the caller's org.
- The activity calls `LLMGateway.complete` (fast tier, purpose `role_preview`, metered to the org,
  budgets apply) with a forced `submit_preview` tool. Nothing is stored; an audit row records that a
  preview ran.
- Stand-in: `fakes/fakes/anthropic.py` answers `submit_preview` from the role section (brief makes
  it short, focus adds lines), so tests can see the difference.

**Console.** The role dialog becomes a two column editor: profile controls (segmented choices and
focus chips) and the description; the preview panel under it. Role cards show the profile as badges.

**Tests.** Unit: role section text per profile, bounds, cleaning. API: validation (unknown values
422), profile saved and listed, preview start/result and org check (another org's id is 404),
members cannot preview. Workflow test with the stand-in. Web: editor and preview.

## 3. Role limits and approval routing (D-203, D-204)

Roles can narrow what the agent does for people who hold them, never widen it.

**Data** (same migration), on `custom_roles`:

- `allowed_playbooks text[]` (null: all),
- `blocked_operations text[] not null default '{}'` (`adapter.operation`, reads or writes),
- `ask_operations text[] not null default '{}'` (writes that always ask, even in Auto),
- `max_mode app.autonomy_mode` (null: no cap),
- `approves_operations text[] not null default '{}'` (holders of this role decide approval
  requests for these operations).

With several roles the most restrictive wins: playbooks intersect, blocked and ask lists add up,
any `ask` cap makes the cap `ask`.

**Enforcement, in code and the database:**

- Mode: `app.effective_mode` is replaced to also cap by the requester's active roles (one place,
  so tasks and App Home agree).
- Playbooks: `AgentRunner.understand` refuses a playbook the roles do not allow, with a reply that
  names the role ("Your role (Support) doesn't include changing code. An admin can change that in
  the console."). The answer buttons are filtered the same way.
- Operations: `Policy._decide` loads the requester's role limits in the same transaction:
  blocked → `denied` (`role_blocked`, names the role); ask listed write in Auto →
  `needs_approval` (`role_ask`). Audited like every decision.
- Routing: when a decision needs approval and an active role approves that operation, the
  decision carries `approver_rule = 'role_or_admin'` and the role. `approvals` gains
  `approver_role_id` (composite foreign key to `custom_roles`), the rule's check allows
  `role_or_admin` (and requires the role id with it), and `app.check_approval_decision` lets an
  active holder of that role, or an active admin, decide. The approval message says who decides
  ("a Tech Lead or an admin decides"); a refused click says the same.
- Console API validates the lists against the operation catalog (`operations()`) and the
  playbook keys; ask and approves lists take writes only.

**Console.** A "Limits" section in the role editor: playbooks (checkboxes, "all" by default),
operations to block, to always ask for, and to approve (grouped by integration), and the autonomy
cap. Role cards show the limits as badges. Copy changes from "Roles never grant or restrict access"
to "Roles can narrow what the agent does for their holders; they never grant access."

**Tests.** Schema guarantees: role id required with `role_or_admin`, role holder decides, a
non-holder member is refused, an admin decides, another org's role cannot be linked; effective mode
capped by a role. Policy: blocked denied, ask in Auto needs approval, routing picks the role.
Runner: a disallowed playbook is refused with the role named. API: validation. Web: limits editor.

## 4. Buttons on answers (D-205)

The demo's next step, one click away, run as the person who clicks.

- `Reply` gains optional buttons; `render` adds an actions block to the last batch.
- `AgentRunner.deliver` adds, when the requester's roles allow the playbook:
  - after `jira_tickets` created tickets in the thread: **Plan a fix** (`investigate`),
  - after an `investigate_issue` plan: **Implement this plan** (`implement`),
  - after `implement_change` opened a pull request: **View pull request** (a link button).
- A click (`quick_action`, value `intent`) in `app/api/slack.py`: one claim per message and
  intent in Key Value (a second click is told it already started), then `AgentRequestWorkflow` with
  the clicker as requester, the thread as the conversation and `intent` on the request. The
  answer's buttons are replaced by "▶️ <@U> chose *Plan a fix*" (the message is updated).
- `SlackRequest` and `TaskRef` carry `intent: str | None = None` (a key, never text). The runner
  maps it to a code owned request text and playbook: the classification is skipped, the brief reads
  the thread with that request. The usual checks all run (identity, channel, role limits, policy).

**Tests.** Adapter render with buttons. Interaction: click starts the workflow with the intent and
the clicker, second click refused, unknown member refused, message updated. Runner: intent skips
classification and pins the playbook; buttons offered per playbook and filtered by role. Live agent
test: tickets, then the button's intent produces a plan.

## Rollout and checks

1. Each feature: `make lint`, `make typecheck`, affected backend tests, web tests; rebuild the
   stand-ins when `fakes/` changes; e2e `ui.spec.ts` for new pages.
2. Push to both branches; Render deploys api, worker, web (and runs migration 012 in the API's
   pre-deploy). Confirm each service is live on the commit.
3. On staging: open Tasks and a demo task's timeline; create a "Tech Lead" role (approves
   `github.push_changes`) and a "Support" role (brief, no `implement_change`), preview them; run the
   demo flow in Slack with the buttons; check the approval says who decides; check the timeline.
4. Record: CLAUDE.md conventions, D-201 to D-205, `docs/progress.md`, the demo log.
