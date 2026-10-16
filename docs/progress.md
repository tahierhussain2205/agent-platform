# Progress

What each phase delivered. Decisions and blockers are in [spec/decisions_and_blockers.md](../spec/decisions_and_blockers.md).

## Phase 0: Repository foundation (done)

- Monorepo layout: `backend/` (FastAPI skeleton, single CLI entry point), `web/` (Vite + React + TypeScript), `docs/`, `.github/workflows/ci.yml`.
- Tooling: uv, ruff, mypy (strict), pytest; pnpm, ESLint, Prettier, Vitest; pre-commit hooks running the project's pinned tools.
- `make lint | fmt | typecheck | test | ci | hooks`.
- `CLAUDE.md` with architecture, invariants, commands, conventions, testing rules and workflow.
- `backend/migrations/001_initial_schema.sql` copied from `spec/schema.sql` (runner arrives in phase 3).
- Verified: `make ci` passes (14 backend tests, 1 web test, production build); `actionlint` passes on the workflow; the API binds `0.0.0.0:$PORT` and shuts down gracefully on SIGTERM; commit hooks pass on all files.
- Not verified: a GitHub Actions run (no remote, B-002).

## Phase 1: Dockerization and local environment (done)

- `backend/Dockerfile`: multistage (uv builder, slim runtime), non root uid 10001, exec form entry point `python -m app`, honors `$PORT`.
- Worker skeleton: connects to Temporal with retries, `PingWorkflow` for smoke checks, graceful SIGTERM (stops polling, drains activities, exits 0).
- Minimal migration runner and idempotent seed (pulled forward from phase 3, see D-012).
- `compose.yaml`: Postgres 16, Valkey, Temporal dev server + UI, SeaweedFS (S3), Jaeger, one shot `migrate` and `seed`, `api`, `worker`, `web` (Vite hot reload).
- `make dev | infra | down | reset | migrate | seed | smoke | test-image | e2e | test-stack`.
- CI: Temporal dev server for the backend job; a `stack` job that boots Compose and runs smoke, image and Playwright checks.
- Verified: from a fresh clone, `make dev` brings every service up healthy; a failing `migrate` keeps `api` from starting (as `preDeployCommand` will on Render); `make smoke` (8 checks), `scripts/test_image.sh` (7 checks), Playwright console test, 44 backend tests (migration runner, seed idempotency and RLS isolation, worker round trip and SIGTERM) and `make ci` all pass.

## Phase 2: First Render deployment (done locally; deploy blocked by B-001)

- `render.yaml`: staging project with `api-staging`, `worker-staging`, `maintenance-staging`, `web-staging`, `db-staging`, `kv-staging`, a shared env group, manual previews; empty production project.
- Image entry point changed to an `app` console script with no `ENTRYPOINT` (D-025); Temporal Cloud API key/TLS support.
- `tests/test_render_blueprint.py` (36 tests): Render JSON schema validation and semantic checks (references, commands vs CLI, health path, env vars vs settings, secrets never literal, shutdown delays, data stores, static site).
- `make render-sim`: executes the Blueprint locally (stand-in stores, `preDeployCommand`, PORT 10000 health, worker round trip, SIGTERM within limits, cron, clean checkout static build with SPA rewrite and headers). Passing.
- `docs/runbooks/deploy.md`.
- Not verified: an actual Render deploy, preview environment, or `checksPass` trigger (B-001, B-002).

## Phase 3: Database and migrations (done locally; staging blocked by B-001)

- Migration 001 runs as a non superuser with CREATEROLE (assumed Render default user, B-010); BYPASSRLS changes are skipped with a notice and isolation fails closed. Compose's Postgres mirrors this with `agent_admin`.
- Login users per component created by `app migrate` (SCRAM verifiers, attributes verified); `Settings.component_dsn()`.
- `app maintenance` (partitions job) as `maintenance_service`; `make maintenance`.
- Schema test suite: generic isolation for every tenant table (231 tests), spec 13.4 guarantees, login users, Render-like migrations, maintenance. Mutation check: 10 of 10 weakened guarantees caught.
- `render.yaml`: pooled `DATABASE_URL`, generated password groups per component; `make render-sim` now migrates as a Render-like user and runs the cron.
- Verified: 400 backend tests, mutation check, `make reset` + smoke, `make render-sim` all pass.

## Phase 4: Backend core (done locally; staging blocked by B-001)

- Unit of work middleware: one org per request, commit before the response starts (deferred constraint failures replace the response), rollback on errors and exceptions, closed after the response starts.
- Database error mapping (403/409/422), JSON 500s with request ids, PgBouncer friendly pools.
- JSON logging with request, org and trace ids, access log, redaction backstop; OpenTelemetry with tenant id on every span after binding, query strings and SQL parameters kept out of spans.
- Key Value helpers, shared Temporal client (tracing interceptor), envelope encryption with per tenant keys and rotation, tenant scoped object storage, `/readyz`.
- Compose: PgBouncer (transaction mode, `auth_query`); API and worker connect through it. CI: PgBouncer, Valkey and SeaweedFS in the backend job.
- Verified: 535 backend tests (unit of work suite runs both direct and through PgBouncer), smoke checks including `/readyz` through PgBouncer and the request's trace in Jaeger, `make render-sim` (readiness with generated credentials).

## Phase 5: Identity and admin console foundations (done locally; staging and Clerk blocked by B-001, B-003)

- Migration 002: identity sync functions (security definer, `app_identity_sync` only) with out of order protection (`identity_event_at`), console resolvers for `app_user`.
- API: Clerk token verification, console identity (privilege from the database), Svix verified webhook sync with dedupe, Clerk Backend API client, console routes (me, orgs, org, members and invitations, roles and primary assignment, settings, audit, access requests), CORS, uniform error shape.
- `fakes` service with a Clerk stand-in (Backend API, v2 tokens, JWKS, Svix webhooks with retries, dev sign in API).
- Web console: dev and Clerk auth adapters, sign in/up, create org, invitations, overview, members, roles, settings, audit, org switcher, read only view for members.
- Tests: 120+ new backend tests (identity functions, verifier attacks, webhooks, console API incl. Clerk failure rollback and last admin), 13 stand-in tests, 19 web unit tests, 11 Playwright end to end flows (signup to admin, invite and accept, promote/demote, last admin refused, removal, revoke, roles with primary, settings validation, audit, member view, org switcher).
- Bugs found by testing and fixed: org name blanked by an upsert, late webhooks resurrecting revoked invitations, confirmations not advancing the event stamp, `httpx` missing from the runtime image, tier arrays returned as strings, the settings "saved" message lost on remount, a wasted unauthenticated `/api/me` call.

## Phase 6: Slack app and ingress (done locally; real Slack and staging blocked by B-004, B-001)

- Slack stand-in in `fakes` (B-004 workaround): Web API subset with Slack's errors and optional 429s, OAuth v2 authorize page, signed event and interaction deliveries with retries, and a Slack-like UI at http://localhost:4100/slack/ui.
- Backend: signature verification, `/slack/events` (URL verification, Key Value claim plus Temporal workflow id dedupe, workspace resolution, routing, references only), `/slack/interactions` (App Home mode, access request Invite/Dismiss), OAuth install with signed single use state, encrypted bot token through the vault, uninstall on `app_uninstalled`/`tokens_revoked` and Disconnect.
- Slack client with per workspace rate limiter, 429/5xx retries and pagination; channel adapter (Block Kit rendering within limits, progress coalescing).
- Worker: `AgentRequestWorkflow` (identify by platform identity or email, access request with admin DMs, channel allowlist check, threaded acknowledgement), `SyncWorkspaceWorkflow`, `PublishHomeWorkflow`; permanent Slack errors are not retried.
- Maintenance: hourly Slack reconciliation through `app.maintenance_active_workspaces()` (migration 003).
- Console: Slack page (connect, disconnect, sync, channels with bot membership hints and the allowlist toggle), access requests on the Members page.
- Slack app manifest and Marketplace notes (`slack/manifest.yaml`, `docs/slack/marketplace.md`); `.env.example` documents every setting (now enforced by a test); CI starts the stand-in so the Slack integration tests run there.
- Tests: about 80 new backend tests (signatures, Block Kit limits, client retries, limiter and coalescing, ingress dedupe and failure paths, OAuth state attacks and install errors, token storage failure, uninstall, channel and member events, activities, workflows against the stand-in through Temporal, console Slack routes and access request decisions, reconciliation, manifest), 13 stand-in tests, 4 web unit tests, 5 Playwright flows (connect and disconnect through OAuth, threaded reply, channel not allowed then allowed, uninvited person invited from Slack, App Home mode). Totals: 762 backend, 26 stand-in, 23 web, 16 e2e; smoke, image checks, schema mutations and the Render simulation pass.
- Bugs found by testing and fixed: the token stored before the install committed (foreign key invisible to the vault's connection), a failed token store leaving a "connected" workspace without a token, permanent Slack errors retried for hours, a notice failing the whole request when the bot was not in the channel, the allowlist checkbox flipping back while saving, reseeding leaving e2e state behind (pending access requests, extra members), tests depending on stand-in state another suite changed.

## Phase 7: Agent runtime core (done locally; real model and staging blocked by B-005, B-001)

- Anthropic Messages API stand-in in `fakes` (B-005 workaround): request and response shapes, prompt cache usage, a deterministic heuristic "model" per agent step, fault injection, scripted responses, recorded requests. The Slack stand-in gained message metadata and thread ids in interaction containers.
- LLM gateway: tier resolution within the org's allowed tiers, task, daily and monthly budgets before every call, per org rate limits by lane, retries and fallback routes, usage events and task totals, GenAI spans without content (D-086 settles the tracing backend question).
- Tool adapter interface with operation metadata and derived tiers; Slack adapter (list, read channel in a window with threads, read thread, post) with normalised, capped, redacted, ref-cited output and idempotent posting; secret redaction.
- Policy module: enablement and admin overrides, the channel rule (also for posting), tiers by Ask/Auto, credential modes, audience check with private delivery; every decision audited.
- Playbooks as versioned Markdown synced at deploy (`slack_qa` v1); the agent runner (understand, gather as an isolated subagent, plan, act with approvals, verify citations in code, report) with role tailoring and prompt caching.
- `TaskWorkflow`: FIFO per thread, the org's concurrency limit, budget checks between steps, approval waits that survive lost signals, cancellation that never interrupts a write, clean failure; approvals and Cancel from Slack buttons; intake hands allowed requests to it.
- Migration 004 and a maintenance job reconcile tasks whose workflow is gone; migration 005 fixes a race in identity sync found in the logs.
- Eval harness (`app eval`, `make eval`, six cases, non blocking CI step); console audit page shows agent actions on behalf of the requester with reasons.
- Tests: about 90 new backend tests (gateway, policy, tools and redaction, runner steps, workflows through Temporal with the stand-ins, interactions, reconciliation, evals, identity races), 9 stand-in tests, 1 web unit test, 8 Playwright flows (sourced answer, non member refused, private delivery, share approved, share rejected with permission check, cancel, budget refusal, audit log). Totals: 850 backend, 36 stand-in, 24 web, 24 e2e; eval mean 1.00 locally; smoke, image checks, 12 schema mutations and the Render simulation pass.
- Bugs found by testing and fixed: module level test tokens expiring during longer runs, the share target read as a source and findings truncated to the oldest messages (stand-in model), permalinks throttled by the default rate bucket, tasks left running blocking the org limit in tests, the smoke readiness probe sampled once, image checks reading container logs before they were flushed, CLI maintenance tests starting reconcile workflows on the dev stack's queue, select-then-insert races in identity sync.

