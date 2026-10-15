# Decisions and Blockers

Running log kept while implementing `implementation_plan.md`. Every decision made without asking is recorded here with its reason, and every blocker with the workaround in place and what is needed to resolve it properly.

- **Decisions** are numbered `D-NNN` and grouped by phase.
- **Blockers** are numbered `B-NNN`. Each lists: what is blocked, the workaround in use, and what is needed to resolve it.

---

## Blockers (summary)

| # | Blocker | Phases affected | Workaround in place | Needed to resolve |
|---|---|---|---|---|
| B-001 | No Render account or API key | 2, 3, 4, 5, 6, 7, 13 (all "on staging" checks) | `render.yaml` validated against Render's schema plus semantic tests; `make render-sim` executes the Blueprint locally with Docker (D-031). Unverified on Render: previews with projects, `checksPass`, how `dockerCommand`/`preDeployCommand` treat the image, `corepack pnpm` in Render's static build image | A Render workspace (Pro or higher for autoscaling), a connected GitHub repo, and the secrets listed in `.env.example` entered as `sync: false` values |
| B-002 | No GitHub remote, so GitHub Actions cannot run | 0 onward | The CI workflow is written; `make ci` runs the same steps locally against real Postgres | A GitHub repository; push, and check the first Actions run |
| B-003 | No Clerk instance | 5 onward | Local Clerk stand-in in the `fakes` service (see D-004) | A Clerk application (publishable and secret keys, webhook signing secret, allowed origins) |
| B-004 | No Slack app or test workspace | 6 onward | Local Slack stand-in with a small Slack-like UI in the `fakes` service | A Slack app created from `slack/manifest.yaml`, a test workspace, client ID/secret and signing secret; Marketplace submission |
| B-005 | No model provider API key | 7 onward | Local Anthropic Messages API stand-in (deterministic, heuristic) in the `fakes` service | An Anthropic API key (or Bedrock/Vertex AI route), then run the eval suite against the real model |
| B-006 | No Temporal Cloud namespace | 1 onward | Temporal dev server in Compose (the plan's local equivalent) | A Temporal Cloud namespace, mTLS cert or API key |
| B-007 | No Jira, GitHub or Google accounts | 8 onward | Local stand-ins for the subset of each API we use | OAuth apps / a GitHub App for each provider, plus test accounts |
| B-008 | No managed sandbox provider account | 11, 12 | To be decided in phase 11 | A provider account (open question 18.2.3) |
| B-009 | MinIO no longer publishes container images, so the plan's local object store cannot be pulled | 1 onward (local only) | SeaweedFS (`chrislusf/seaweedfs`, `mini` mode) as the local S3 compatible store (D-015) | Nothing for production (S3 or R2 per the plan). Revisit only if SeaweedFS behaves differently from S3 for an operation we use |

---

## Cross-cutting decisions

| # | Decision | Reason |
|---|---|---|
| D-001 | Keep `spec/` as the home of the spec, the plan and `schema.sql`; `docs/` holds per phase plans, progress and runbooks. The plan's "move the spec to `docs/spec.md`" step is skipped. | `spec/` is the input folder handed over for this work; moving it would break the expected location of this file. `CLAUDE.md` links to `spec/`. |
| D-002 | This file replaces separate ADR files in `docs/decisions/`. | Explicit instruction to record every decision and blocker here. |
| D-003 | A local git repository with one branch per phase, merged into `main` when the phase closes. Nothing is pushed. | The plan asks for reviewable commits per phase; there is no remote (B-002). |
| D-004 | External SaaS is replaced locally by one `fakes` service that speaks the same HTTP shapes as the real APIs for the subset we use (Clerk, Slack, Jira, GitHub, Google, Anthropic). Our code only sees base URLs from config, so switching to the real service is configuration, not code. | Blockers B-003 to B-008. Faking at the HTTP boundary keeps the real client code paths (signatures, OAuth, pagination, rate limit handling) under test. The fakes are dev and test infrastructure, never deployed. |
| D-005 | Phase plans are written to `docs/plans/phase_NN_<name>.md` before code changes; progress is appended to `docs/progress.md`. | Follows section 1 of the plan. |

---

## Phase 0: Repository foundation

| # | Decision | Reason |
|---|---|---|
| D-006 | `mypy --strict` as the Python type checker (not pyright). | The plan allows either; mypy has the pydantic plugin and is the more common choice with FastAPI. |
| D-007 | ESLint + Prettier for the web app, replacing the oxlint setup the current Vite template ships. TypeScript `strict` and `noUncheckedIndexedAccess` turned on (the template leaves `strict` off). | The plan names ESLint and Prettier; strict typing catches more bugs early. |
| D-008 | Commit hooks use the `pre-commit` framework with local hooks only, running the project's own pinned ruff, prettier and eslint. | No third party hook repos to drift from the versions CI uses. |
| D-009 | CLI subcommands that are not built yet (`worker`, `maintenance`, `migrate`, `seed`) exit with status 2 and a message naming the phase that adds them. | Nobody mistakes a placeholder for a working command, and a deploy using one fails loudly. |
| D-010 | `GET /healthz` is the only route in phase 0; the OpenAPI docs routes are disabled. | The API is not public documentation; fewer exposed routes. Docs can be enabled for local use later if wanted. |
| D-011 | After SIGTERM the API exits with status 143 (uvicorn drains requests, then re-raises the signal). Accepted as is. | Standard behavior for a process stopped by SIGTERM; Render and Docker treat it as a normal stop. |

## Phase 1: Dockerization and local environment

| # | Decision | Reason |
|---|---|---|
| D-012 | A minimal migration runner (`python -m app migrate`) is built in phase 1 instead of phase 3. It applies `NNN_*.sql` files in order, each inside one transaction with its bookkeeping row in `public.schema_migrations`, refuses edited migrations (checksum), rejects files with top level `begin`/`commit`, and takes a session advisory lock. Phase 3 adds Render compatibility, login users and the ported schema tests. | The seed script needs the schema; Compose needs the same "migrate before start" step Render uses. |
| D-013 | `001_initial_schema.sql` is `spec/schema.sql` with only its top level `begin;` and `commit;` lines replaced by comments. A test fails if anything else drifts. | The runner owns the transaction so the bookkeeping row commits atomically with the schema. |
| D-014 | `psycopg` 3 (with `psycopg_pool`) is the database driver; SQL is written by hand (no ORM). | The schema is SQL first with RLS, triggers and security definer functions; raw SQL keeps that visible. psycopg 3 runs multi statement scripts and supports `prepare_threshold=None` for PgBouncer transaction mode. |
| D-015 | SeaweedFS replaces MinIO as the local S3 compatible store (B-009). It creates the `artifacts` bucket at startup. | MinIO images are no longer pullable; SeaweedFS is mature, Apache licensed and S3 compatible. |
| D-016 | Jaeger (v2, OTLP) is the local tracing backend for now. The LLM trace backend decision stays with phase 7. | The plan lists Jaeger or Langfuse locally; Jaeger is one light container and the instrumentation is OpenTelemetry either way. |
| D-017 | Seed data lives in one JSON fixture (`backend/app/seed/fixture.json`) that later Clerk and Slack stand-ins load too. The seed runs as `app_maintenance` over the migration connection, upserts by natural keys, and refuses to run when `ENV` is staging or production. | One source for people, orgs and channels keeps the fakes and the database consistent; running as the maintenance role exercises its grants rather than superuser powers. |
| D-018 | Compose host ports are offset (Postgres 55432, Valkey 56379) to avoid clashing with databases already on developer machines. | This machine already runs Postgres 14 and MySQL locally. |
| D-019 | Backend hot reload is not set up in Compose; `make dev` rebuilds the image (about 15 seconds), and `make infra` + `uv run` on the host is the fast loop. The web container does hot reload. | Keeps the containers identical to Render; no Compose only entrypoints. |
| D-020 | `python -m app check temporal` runs `PingWorkflow` end to end and is the smoke check that a worker is connected and polling. | A worker can be "up" without polling the right queue; only a real round trip proves it. |
| D-021 | Browser tests live in a separate `e2e/` package (Playwright) because they exercise the whole stack, not just the console. | Later phases add the Slack stand-in UI to the same suite. |

## Phase 2: First Render deployment

| # | Decision | Reason |
|---|---|---|
| D-022 | One `api` service serves Slack ingress and the console API. | Plan recommendation; can split later by deploying the same image twice. |
| D-023 | Region `oregon` for staging. | Render's default US region; matches the `us` default on `orgs.region`. Per region projects come with residency work. |
| D-024 | `render.yaml` declares two projects: `agent-platform-staging` (environment `staging`, every resource) and `agent-platform-production` (environment `production`, empty, protection on). Resource names carry the environment suffix (`api-staging`). | The plan wants separate staging and production projects from the same file; Blueprint names must be unique within the file, so suffixes avoid a rename when production is added in phase 13. |
| D-025 | The image has no `ENTRYPOINT`; `CMD ["app", "api"]` with an `app` console script, and every Render command is a full command (`app worker`). | Render documents only that `dockerCommand` replaces `CMD`; without an `ENTRYPOINT` the command works whichever way Render applies it. |
| D-026 | Staging deploys use `autoDeployTrigger: checksPass` on `main`, with build filters (`backend/**` + `render.yaml` for Docker services, `web/**` for the static site). | Deploy only after CI passes; avoid rebuilding services whose code did not change. |
| D-027 | Key Value uses `maxmemoryPolicy: noeviction` and an empty IP allow list. | Dedupe keys and locks must never disappear silently; memory pressure should surface as errors. Private network only. |
| D-028 | Temporal Cloud settings are `sync: false` secrets; the worker gains API key + TLS support (`TEMPORAL_API_KEY`). Empty optional secrets are treated as unset. | Ready for Temporal Cloud without code changes; an unset dashboard value must not switch on TLS against the dev server. |
| D-029 | The static site's `VITE_API_URL` is a `sync: false` build variable. | Blueprints cannot reference another web service's public URL. Entered once after `api-staging` exists. |
| D-030 | The cron job (`app maintenance`) is in the Blueprint now even though the command arrives in phase 3; it exits 2 until then. | The walking skeleton should include every resource; a failing cron is visible rather than silently missing. |
| D-031 | Render's JSON schema is vendored in `dev/schemas/render.schema.json` and the Blueprint is validated in the backend tests, plus a local Render simulation (`make render-sim`). | Workaround for B-001: proves the Blueprint is valid and coherent with the image and code. Refresh the schema with `curl -fsSL https://render.com/schema/render.yaml.json`. |
