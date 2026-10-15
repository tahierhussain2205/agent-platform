# Slack Agent Platform: Implementation Plan

| | |
|---|---|
| **Status** | Draft v1 |
| **Date** | October 16, 2026 |
| **Inputs** | `slack_agent_platform_spec.md` (requirements and design), `schema.sql` (tested initial migration) |
| **Deployment target** | Render |

This plan breaks the project into phases. Each phase states its goal, what to build, the direction to take, and when it is done. It is intentionally high level: detailed planning happens per phase, in Claude Code, against the spec and the code as it exists at that point.

---

## 1. How to use this plan with Claude Code

For each phase, in order:

1. **Read** `CLAUDE.md`, the relevant sections of `docs/spec.md`, and this phase below.
2. **Plan** the phase in detail and write it to `docs/plans/phase_NN_<name>.md`: tasks, files, schema changes, tests, risks. Get the plan reviewed before writing code.
3. **Implement** in small, reviewable commits on a phase branch.
4. **Test**: unit and integration tests pass locally (Docker Compose) and in CI.
5. **Deploy** to the Render preview or staging environment and run the phase's checks there.
6. **Record**: update `CLAUDE.md` (new commands, conventions, gotchas), append to `docs/progress.md`, and add an ADR in `docs/decisions/` for any deviation from the spec.
7. **Close** the phase only when every item in its "Done when" list is true.

A phase never weakens a security invariant from section 4 to make something work. If an invariant blocks progress, stop and raise it.

---

## 2. Target shape

### 2.1 Repository layout

```
/
  CLAUDE.md                  project memory for Claude Code
  render.yaml                Render Blueprint (all services, one file)
  compose.yaml               local environment
  .env.example
  Makefile                   common commands (dev, test, lint, migrate, seed)
  docs/
    spec.md                  the requirements and technical spec
    implementation_plan.md   this file
    plans/                   one detailed plan per phase
    decisions/               ADRs for deviations and new decisions
    progress.md              what each phase delivered
    runbooks/                operational procedures (added later)
  backend/
    Dockerfile
    pyproject.toml, uv.lock
    app/                     FastAPI app, worker, agent, adapters, policy
    migrations/              SQL migrations, starting with 001 from schema.sql
    tests/
  web/
    package.json             React + TypeScript console (Vite)
    src/
  .github/workflows/ci.yml
```

### 2.2 Runtime shape on Render

One backend Docker image, started with different commands per service. The console is a static site.

| Render resource | Type | Runs |
|---|---|---|
| `api` | Web service (Docker) | FastAPI: Slack ingress (`/slack/*`), console API (`/api/*`), health (`/healthz`). Migrations run in `preDeployCommand` |
| `worker` | Background worker (Docker, same image) | Temporal workers: agent tasks, schedules, approvals |
| `maintenance` | Cron job (Docker, same image) | Partition creation, retention pruning, Slack membership reconciliation |
| `web` | Static site | React console, built by Render, SPA rewrite to `index.html` |
| `db` | Render Postgres | Postgres 16 or later, with the built in PgBouncer pool enabled |
| `kv` | Render Key Value | Redis compatible: dedupe keys, locks, rate counters, membership cache |

External services, configured through Render environment groups:

| Service | Purpose | Local equivalent |
|---|---|---|
| Temporal Cloud | Durable workflows and schedules | Temporal dev server in Compose |
| Clerk | Console auth and orgs | Clerk development instance |
| Model provider | LLM calls through our gateway | Same, with a dev key |
| Managed sandbox provider | Coding tasks | Same, with a dev account |
| S3 compatible object storage (AWS S3 or Cloudflare R2) | Large artifacts and diffs | MinIO in Compose |
| Tracing backend | OpenTelemetry traces, LLM traces | Jaeger or Langfuse in Compose |

### 2.3 Environments

| Environment | Where | Purpose |
|---|---|---|
| Local | Docker Compose | Day to day development and integration tests |
| CI | GitHub Actions with a Postgres service | Lint, type check, tests on every push |
| Preview | Render preview environments (per pull request) | Review a change running on Render |
| Staging | Render, its own project | End to end checks with a test Slack workspace |
| Production | Render, its own project | Customers |

---

## 3. Render rules that shape the code

These hold from the first deploy onward; Claude Code should treat them as constraints.

1. **Listen on `$PORT`** and bind `0.0.0.0`. Never hardcode a port.
2. **Configuration from environment variables only.** One typed settings object; secrets come from Render environment groups or `sync: false` variables, never from files in the image.
3. **One image, many commands.** `api`, `worker`, `maintenance` and `migrate` are subcommands of the same entry point, selected with `dockerCommand` in `render.yaml`.
4. **Migrations run in `preDeployCommand`** on the `api` service, so a failed migration blocks the deploy instead of breaking a running one. Migrations must be safe to run against the previous code version during rollout.
5. **Graceful shutdown on SIGTERM.** Render waits `maxShutdownDelaySeconds` (default 30, maximum 300) before killing a service. Web requests drain; workers stop polling, finish or checkpoint the current activity, then exit. Set a longer delay for `worker`.
6. **Stateless services.** No persistent disks on application services (they also block autoscaling). State lives in Postgres, Key Value, object storage and Temporal.
7. **Use internal URLs** for Postgres and Key Value (same region), and the PgBouncer `connectionPoolString` for application traffic. With PgBouncer in transaction mode: set the org context with `set_config('app.org_id', ..., true)` inside every transaction, and disable server side prepared statements in the database driver.
8. **No privileged containers on Render**, so sandboxes always run at the managed sandbox provider, never inside our services.
9. **Health checks**: `/healthz` (process up) for Render, plus a readiness check that confirms Postgres and Key Value connectivity.
10. **Autoscaling** needs a Pro workspace or higher and is disabled in preview environments; size preview environments small with service level `previews` settings.

---

## 4. Invariants every phase must keep

These come from the spec and belong in `CLAUDE.md` verbatim.

1. Tenant data is only read or written inside a unit of work bound to one org (`app.org_id` set in the same transaction). No tenant query runs without it.
2. Request paths connect as `app_user` only. The identity sync, maintenance and vault roles are used only by their own components.
3. The policy module is called before every tool call and every output. Enforcement lives in code and the database, never in a prompt.
4. The agent acts within the requester's own access: channel allowlist plus membership, credential mode per operation, audience check on output.
5. Secrets never appear in model context, logs, traces or error messages.
6. The sandbox never holds push credentials; only the diff leaves it.
7. Raw Slack messages are never stored; only derived artifacts and permalinks.
8. Every external write has an idempotency key checked before acting.
9. The transaction commits before the HTTP response starts.
10. Irreversible actions always need human approval.

---

## 5. Phases

### Phase 0: Repository foundation and CLAUDE.md

**Goal:** a repository Claude Code can work in productively from day one.

**Scope:**
- Monorepo layout from 2.1. Move the spec to `docs/spec.md`, this plan to `docs/implementation_plan.md`, and `schema.sql` to `backend/migrations/001_initial_schema.sql`.
- Backend tooling: Python 3.12 or later, `uv` for dependencies, `ruff` for lint and format, a type checker (`mypy` or `pyright`), `pytest`.
- Web tooling: Vite, React, TypeScript, `pnpm`, ESLint, Prettier.
- CI: lint, type check and tests for both apps on every push.
- `Makefile` with the common commands; `.env.example` documenting every variable.
- Commit hooks for format and lint.
- **`CLAUDE.md`**, containing:
  - what the product is, in a few lines, with a pointer to `docs/spec.md`;
  - architecture summary (components, the one image many commands model, Render layout);
  - the invariants from section 4, verbatim;
  - commands: run locally, test, lint, migrate, seed, deploy;
  - code conventions: module layout, naming, typing, error handling, logging, how to add a migration, how to add a tool adapter, how to add a playbook;
  - testing rules: real Postgres for anything touching the database, no mocks of row level security, isolation tests mandatory for new tenant tables;
  - the per phase workflow from section 1;
  - a "gotchas" section that grows each phase.

**Direction:** keep `CLAUDE.md` short and factual; link to docs instead of copying them. It is a living file and every phase updates it.

**Done when:** CI is green on the skeleton, `make test` and `make lint` work, and `CLAUDE.md` is reviewed.

---

### Phase 1: Dockerization and local environment

**Goal:** the whole system runs locally with one command.

**Scope:**
- `backend/Dockerfile`: multistage build, slim Python base, dependencies installed from the lock file, runs as a user other than root, single entry point with subcommands (`api`, `worker`, `maintenance`, `migrate`), honors `$PORT`, handles SIGTERM.
- `compose.yaml`: Postgres 16, Redis or Valkey, Temporal dev server with its UI, MinIO, a tracing backend, `api`, `worker`, and the web dev server with hot reload.
- Seed script: two orgs, admins and users, a fake workspace, channels; used by local runs and tests.
- `make dev`, `make down`, `make reset` (drop and recreate local data).

**Direction:** the image used locally must be the image deployed to Render. Avoid Compose only behavior the Render setup cannot reproduce.

**Done when:** `make dev` brings everything up from a clean checkout, `api` answers `/healthz`, the worker connects to Temporal, and the web app loads.

---

### Phase 2: First Render deployment (walking skeleton)

**Goal:** prove the deploy path end to end before there is real functionality.

**Scope:**
- `render.yaml` with every resource from 2.2: `api`, `worker`, `maintenance`, `web`, `db`, `kv`, environment groups for shared and per service settings.
- `api` with `healthCheckPath`, `preDeployCommand` running migrations, and `maxShutdownDelaySeconds`.
- `web` static site with the SPA rewrite and its build environment variables.
- Preview environments enabled (manual generation to start), with small plans and `previewValue` overrides where needed.
- Separate Render projects for staging and production; production stays empty until phase 13.
- A short deploy runbook in `docs/runbooks/deploy.md`.

**Direction:** everything about Render lives in `render.yaml`; no manual dashboard configuration that the Blueprint does not capture, except secrets entered with `sync: false`.

**Done when:** a push to the main branch deploys all services to staging, health checks pass, the console loads, and a pull request can spin up a preview environment.

---

### Phase 3: Database and migrations

**Goal:** the tested schema running on Render Postgres, with isolation guarantees enforced in CI.

**Scope:**
- A SQL first migration runner (Alembic executing raw SQL files is fine) with a migrations table; `001` is the existing schema.
- **Render Postgres compatibility:** run migrations as Render's default user. Check early whether that user can create roles and grant `BYPASSRLS`. If not, apply the fallback already described in the spec: drop `BYPASSRLS`, and route identity sync and maintenance work through security definer functions owned by `app_owner`. Record the outcome as an ADR.
- Login users per component (API, worker, vault, identity sync, maintenance), created by migration or a bootstrap command, with credentials in Render environment groups.
- Port the 28 schema tests to `pytest` and run them in CI against Postgres. Any new tenant table must come with an isolation test.
- Partition maintenance in the `maintenance` cron job.

**Direction:** the database is a security boundary, not just storage. Schema changes always go through migrations, never through manual SQL in production.

**Done when:** migrations apply cleanly on local, CI and Render staging; the 28 tests pass in CI; the cron job creates partitions on staging.

---

### Phase 4: Backend core

**Goal:** the service layer every later phase builds on.

**Scope:**
- FastAPI app factory, typed settings, structured JSON logging, OpenTelemetry tracing with tenant ID on every span.
- Database engine using the PgBouncer pool; the **unit of work middleware** from spec section 14.1 (bind one org per request, commit before the response starts, roll back on error).
- Database error mapping to HTTP responses.
- Key Value client; Temporal client and worker skeleton with graceful shutdown.
- Envelope encryption service with a pluggable key provider. Start with a master key from a Render secret and key versions recorded in `kms_key_id`; a cloud KMS provider can be added later behind the same interface.
- Object storage client for the S3 compatible store.
- Readiness endpoint.

**Direction:** keep the core small and boring. The middleware and context handling get the most tests: commit before response, no context leak across pooled connections, concurrent requests from different orgs, rollback on error.

**Done when:** the middleware tests pass against Postgres through PgBouncer locally, and staging shows traces and logs per request.

---

### Phase 5: Identity and admin console foundations

**Goal:** an admin can sign up, set up an org, and manage people and roles.

**Scope:**
- Clerk in the web app (sign up, sign in, org switcher) and in the API (token verification as in spec section 14.3: JWKS cache, `azp` check, reject pending sessions, active org from `o.id`).
- Clerk webhook endpoint (signature verified) syncing orgs, users and memberships through the identity sync role, including invites before signup.
- Migration `002` with the console identity resolver; privilege comes from our database, not the token.
- Console pages: org overview, users and invites (promote, demote, remove), custom roles with primary role assignment, org settings and limits, basic audit view.
- Removal flow: revoke links, pause schedules, close sessions (database triggers already exist; add task cancellation when the agent exists).

**Direction:** Clerk is the source of truth for identity; our database mirrors it. The console never writes memberships directly.

**Done when:** on staging, a new signup gets an org and admin role; invites, promotions, removal and the last admin rule work from the console; roles with a primary role can be assigned.

---

### Phase 6: Slack app and ingress

**Goal:** the agent can be installed in Slack and reliably receives every event.

**Scope:**
- Slack app manifest in the repository (minimal scopes, events over HTTP, interactivity, App Home).
- OAuth install flow per org; bot token stored through the encryption service; uninstall and token revocation handling.
- Ingress endpoints from spec section 14.2: signature and timestamp checks, URL verification, dedupe with claim and release, workspace resolution, Temporal workflow start keyed by event ID, acknowledgment within 3 seconds.
- Channel sync and the allowlist page in the console, including bot membership status.
- Membership sync from Slack events, with reconciliation in the `maintenance` cron job.
- Slack user to org user matching by email; access requests for uninvited users with a one click invite for admins.
- Channel adapter: agent intents (reply, progress update, approval request, report) rendered as Block Kit, sent through a rate limited outbound queue per workspace.
- App Home with basic user settings.
- Start preparing Slack Marketplace submission (scopes justification, privacy policy, support details) and check the current entry requirements.

**Direction:** ingress never does agent work. For now, a stub workflow replies "received" through the channel adapter so the full path is testable.

**Done when:** installing into a test workspace works from the console; a mention in an allowlisted channel produces a reply in the thread; duplicates, bad signatures and unknown workspaces are handled; an uninvited user gets the access request flow.

---

### Phase 7: Agent runtime core

**Goal:** a real agent answering questions over Slack within policy.

**Scope:**
- Task workflow in Temporal: create task, status message, run the loop (understand, gather, plan, act, verify, report), checkpoints per step, cancellation.
- Orchestrator with playbook loading and versioning; the brief builder with sourced items.
- **Policy module** (spec 9.2): tool enablement, channel rule, action tiers and effective mode, credential mode resolution, audience check on output; every decision audited.
- **LLM gateway**: one provider family with admin selected tiers, per tenant limits, budgets checked before start and between steps, prompt caching, usage events, retries and fallback.
- Approval flow: Block Kit buttons, decision through the API, signal to the waiting workflow, expiry.
- Role tailoring injected as a bounded prompt section.
- First playbook: questions and summaries over allowed Slack channels and threads, with sources.
- **Eval harness** skeleton: replay cases, scoring, a command to run them, wired into CI as a non blocking job for now.

**Direction:** the orchestrator stays thin and owns state; heavy retrieval goes to subagents with isolated context. Build the tool adapter interface here (Slack only), so phase 8 extends it rather than inventing it.

**Done when:** on staging, "summarize today's discussion in #bugs" returns a correct, sourced answer; a user who is not a member of the channel is refused; budgets stop a task cleanly; the eval harness runs.

**Decision needed before this phase:** tracing backend for LLM traces. Decision T15 chose self hosted Langfuse, but its current self hosted setup needs ClickHouse, Redis, blob storage and separate web and worker services, which is heavy to run on Render. Options: self host it anyway (on Render private services or elsewhere), use Langfuse Cloud until customer data flows, or another OpenTelemetry compatible backend. Instrumentation is OpenTelemetry either way, so the backend stays swappable.

---

### Phase 8: Integrations framework

**Goal:** a uniform, safe way to add tools.

**Scope:**
- Tool adapter interface with operation metadata in code (read or write, reversible, default sensitivity, default credential mode).
- Connections: org connections by admins and user links, OAuth flows for Jira, GitHub and Google, token refresh and revocation, health status in the console.
- Token vault component (its own database role) issuing short lived credentials to the worker.
- Credential mode overrides per org (`operation_policies`).
- Native adapters: Jira (read first), GitHub (read: repos, files, CODEOWNERS). A generic vendor MCP adapter for the long tail, with output caps and redaction; customer supplied MCP servers behind admin approval.
- Console: integrations page with enable, connect, link account, overrides and health.

**Direction:** adding a tool means writing an adapter and its metadata, nothing else. The policy module and audit apply automatically.

**Done when:** an admin connects Jira and GitHub, a user links their own accounts, and the agent can answer Jira and GitHub questions with the right credential per operation.

---

### Phase 9: Scheduled reports (Example 3)

**Goal:** recurring reports that are exact and permission safe.

**Scope:**
- Schedules created from Slack in natural language (converted to cron with time zone and confirmed with the user), from App Home, and from the console.
- Temporal Schedules with staggered start times; each run creates a task under the owner's identity.
- Report playbooks: channel summary and Jira progress. All numbers computed by code; the model writes the narrative only.
- Delivery to a channel or DM; pause rules (owner removed, access lost, budget, admin) with admin notification; per org limits.
- Console: all schedules with status and controls.

**Direction:** treat report numbers as data, not generated text. Add tests comparing report figures with direct queries.

**Done when:** a QA user schedules a daily Jira report on staging and receives exact numbers on time; removing the owner pauses it and notifies admins.

---

### Phase 10: Jira actions and service catalog (Example 1)

**Goal:** turn conversations into correctly assigned tickets.

**Scope:**
- Service catalog: the agent proposes services from repositories, CODEOWNERS and Jira components; admins confirm and edit in the console.
- Playbook: find issues in a channel, group and deduplicate them against existing tickets, propose tickets with assignee suggestions, create them after approval.
- Jira write operations with the user's linked account, idempotency keys and audit.
- Ask and Auto behavior per the action tiers, including the admin ceiling.

**Direction:** assignee suggestions must explain their reasoning (owner of service X, recent commits) so humans can trust or correct them.

**Done when:** on staging, Example 1 works end to end in both modes, writes are never duplicated on retry, and the tickets appear under the requesting user's name.

---

### Phase 11: Investigation and planning (Example 2, part 1)

**Goal:** the agent understands a ticket and produces a sourced plan.

**Scope:**
- Observability interface (search events, fetch trace, list error groups) with a first adapter for Google Cloud Logging; filtering pushed to the backend; results grouped by error signature.
- Investigation subagents for logs, code (read only repository access through the sandbox provider) and recent changes (commits and deploys around the incident window).
- Plan template per playbook with evidence, hypotheses, steps, risks and verification; brainstorming in the thread; replanning rules.
- Eval cases from real resolved bugs (thread, logs, merged fix).

**Direction:** measure retrieval quality from the start: right service and time window, right error signature, recall of files the real fix touched.

**Done when:** for a set of replayed historical bugs, the agent produces plans whose root cause matches the actual fix at an agreed rate, with every claim sourced.

---

### Phase 12: Implementation and pull requests (Example 2, part 2)

**Goal:** the agent implements an approved plan and raises a PR safely.

**Scope:**
- Sandbox interface with the managed provider: create, clone with a read only token, set up from a saved recipe, run commands, export the diff, destroy.
- Claude Agent SDK running inside the sandbox, with model calls through our proxy so no raw key enters the sandbox.
- Trusted pusher outside the sandbox: diff policy checks (size, protected paths, secret scan), branch prefix rule, PR creation through a GitHub App without permission to modify workflow files.
- CI feedback loop and PR review comments through webhooks (`task_links`), with revisions in a rebuilt sandbox.
- Setup recipe learning per repository; snapshot reuse where the provider supports it.

**Direction:** follow the sandbox rules in spec section 8.2 exactly. When tests cannot run, say so in the PR and the thread.

**Done when:** on staging, Example 2 works end to end on a test repository; a red team test confirms the sandbox cannot read push credentials or reach the control plane.

---

### Phase 13: Production readiness on Render

**Goal:** ready for the first external customers.

**Scope:**
- Production project from the same `render.yaml`; autoscaling for `api` and `worker`; plan sizing.
- Postgres backups and point in time recovery verified with a restore drill; read replica if needed.
- Alerts on error rates, queue depth, ingress latency, budget overruns and failed schedules.
- Retention jobs (artifacts, traces) and the offboarding job; data deletion verified.
- Security review: dependency scanning, secret scanning, isolation tests, sandbox red team, penetration test on ingress and console.
- Load test of ingress (acknowledgment under 1 second at peak) and of concurrent tasks per tenant.
- Runbooks: deploy, rollback, incident, key rotation, tenant offboarding.
- Slack Marketplace submission; usage dashboards and plan limits in the console; shadow mode for new customers.

**Direction:** nothing ships to production that has not run on staging. Treat the non functional targets in spec section 16 as acceptance tests.

**Done when:** a design partner org runs on production with alerts, backups and runbooks in place, and the Marketplace submission is in review.

---

## 6. Open items to resolve along the way

| Item | Needed by |
|---|---|
| Tracing backend for LLM traces (see phase 7) | Phase 7 |
| Single `api` service for ingress and console versus two services. Recommended: one service at first; split later by deploying the same image twice | Phase 2 |
| Object storage: AWS S3 or Cloudflare R2 | Phase 4 |
| Route to the model provider: direct API, Bedrock or Vertex AI | Phase 7 |
| Managed sandbox provider | Phase 11 |
| How assignees are chosen (catalog owners, Jira components, history) | Phase 10 |
| Render region, and per region projects for data residency later | Phase 2 |
| Slack Marketplace entry requirements and timeline; until approved, thread reads by commercially distributed apps are heavily rate limited | Phase 6 |

---

## 7. Sources

- [Render Blueprint specification](https://render.com/docs/blueprint-spec)
- [Render Postgres: creating and connecting](https://render.com/docs/postgresql-creating-connecting)
- [Render Postgres: credentials](https://render.com/docs/postgresql-credentials)
- [Render community: privileged containers](https://community.render.com/t/run-docker-container-in-privileged-mode/1814)
- [Langfuse self hosting](https://langfuse.com/self-hosting)
