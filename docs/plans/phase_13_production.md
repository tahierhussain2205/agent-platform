# Phase 13: Production readiness on Render

**Goal:** ready for the first external customers (plan phase 13; spec 11.4, 12.5, 16, 15 item 4).

**Done when:** a design partner org runs on production with alerts, backups and runbooks in place, and the Slack Marketplace submission is in review. Production itself needs the user (a Render workspace able to autoscale, production accounts for Clerk, Slack and the providers, the Marketplace submission: B-001, B-003, B-004, B-007); this phase builds and tests everything that does not, so that launching is configuration and the runbooks.

## Decisions up front

- **Production is not created by this phase.** `render.yaml` auto-syncs from `main`, so adding production services to it would create paid services before anyone decided to launch. The production services (autoscaling `api` and `worker`, larger plans, production project) are written in `deploy/render-production.yaml`, checked by the same Blueprint tests, and merged into `render.yaml` in one reviewed change when the user launches (runbook).
- **Restores are drilled locally and on staging on request.** A scripted drill restores a dump into a fresh database and runs the isolation and guarantee suites against it; on Render it restores a point in time into a new database through the API (costs money: the user starts it).

## Data lifecycle

- **Retention** (spec 16: 90 days by default, 1 to 3650 per org): a maintenance job deletes, per org (its own tenant context, so row level security applies), finished tasks older than the org's retention with everything that hangs off them (steps, approvals, external actions, links, artifacts), artifacts past `retain_until` and their objects in object storage, and sessions with no task left. Usage events older than 13 months go by monthly partition (their daily rollup stays); the audit log is kept for 400 days (`AUDIT_RETENTION_DAYS`), also by partition. Deleting partitions is a security definer function for the maintenance role.
- **Offboarding** (spec 12.5): an org marked `offboarding` (Clerk deleted it, or an operator ran `app offboard`) is deleted after a grace period (7 days, `OFFBOARDING_GRACE_DAYS`) across every store: its Temporal schedules, its objects (tenant prefix), its Key Value keys (tenant prefix), the provider tokens the vault can revoke (best effort), then its rows (one security definer function deleting the org, everything else cascades). The job proves the deletion: no row in any tenant table, no object under the prefix, no key under the prefix. `app offboard --org <id> --now` runs it at once for an operator (audited, refuses an active org).

## Shadow mode (spec 15 item 4)

- `org_settings.shadow_mode`: the agent reads, plans and answers, but never acts. Writes are denied by the policy module (code `shadow_mode`) and reported in the thread as what it would have done; no approval request is offered. Orgs created by self service sign up (the Clerk webhook) start in shadow mode; admins turn it off in the console (audited). Seeded and test orgs are unchanged.

## Usage and plan limits (spec 4.7 Usage, 4.8)

- Console **Usage** page and `GET /api/usage?days=` (admins): tokens and cost per day, per person and per playbook, and today's and this month's use against the org's limits.
- **Plans** (`orgs.plan`: free, team, enterprise): ceilings in code on what admins may set (tokens per person per day, per org per month, concurrent tasks, schedules, retention). The Settings API refuses values above the plan's ceiling with the reason; operators change an org's plan with `app org plan`.

## Alerts

- A maintenance job checks, every run: task failure rate (last hour, per org and overall), queue depth (queued tasks and the oldest one's age), ingress acknowledgment latency (p95 and p99 of the last 15 minutes, recorded by the Slack ingress into Key Value by minute), budget overruns (tasks stopped by a budget, orgs at their monthly limit), failed or paused schedules. Thresholds are settings. An alert goes to `ALERT_WEBHOOK_URL` (Slack incoming webhook format) once per condition per hour (deduplicated in Key Value), and is logged; without a URL, alerts are logged only.

## Security

- CI: dependency audits (`pip-audit` on the locked backend and stand-in dependencies, `pnpm audit --prod` for the console), secret scanning (`gitleaks` on the history), both blocking on high severity.
- Security headers on the API (`X-Content-Type-Options`, `Referrer-Policy`, `X-Frame-Options: DENY`, `Strict-Transport-Security` outside local) and on the console's static site (`render.yaml` headers, including a content security policy).
- An ingress and console attack suite (`tests/security/`): forged and replayed Slack signatures, oversized bodies, forged, expired and wrong audience console tokens, another org's ids, CORS from other origins, methods and paths that must not exist, object storage references of another tenant. Most properties have tests already; this suite runs them as one checklist and adds what is missing.

## Load

- `scripts/load_ingress.py`: signed Slack events at a target rate against the API, measuring acknowledgment latency (p50, p95, p99, errors) and the time to the first status message; and a burst of tasks for one org to check the concurrency limit queues instead of overloading. Run locally and on staging; the numbers go in the progress log and spec 16's targets are the pass marks.

## Runbooks

- `docs/runbooks/`: deploy (exists), rollback (in deploy), incident (triage by alert, where to look, how to pause an org or the agent), key rotation (encryption keys with re-encryption, Slack signing secret, GitHub App key, Clerk keys, the vault service token, database passwords), tenant offboarding, restore drill, alerts, launching production.

## Tests

- Retention: rows and objects past retention go, nothing else does, other orgs untouched, partitions dropped only past their age.
- Offboarding: every store emptied for the org and only for it; grace period; refused for active orgs; schedules deleted; idempotent re-runs.
- Shadow mode: every write denied with the reason and reported; reads and answers unchanged; new self service orgs start in it; the console toggle audited.
- Usage API and page; plan ceilings refused with reasons.
- Alerts: each condition fires once per hour, not below threshold; the webhook payload; ingress latency recording.
- Security headers; the attack suite; CI scanning steps.
- Blueprint tests on `deploy/render-production.yaml`.
- Playwright: usage page, shadow mode toggle and a shadow answer in Slack.
