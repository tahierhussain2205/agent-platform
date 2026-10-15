# CLAUDE.md

## What this is

A multi tenant AI agent that works as a teammate inside Slack. Each customer is an **org**; admins configure it in a web console (channels it may read, tools, models, limits, custom roles). The agent reads and summarizes conversations, works with Jira and GitHub, investigates issues, raises PRs and delivers scheduled reports, always within the requesting person's own access.

- Requirements and design: [spec/slack_agent_platform_spec.md](spec/slack_agent_platform_spec.md)
- Phased plan: [spec/implementation_plan.md](spec/implementation_plan.md)
- Initial schema (source of migration 001): [spec/schema.sql](spec/schema.sql)
- Decisions and blockers log: [spec/decisions_and_blockers.md](spec/decisions_and_blockers.md)
- Per phase plans: [docs/plans/](docs/plans/), delivery log: [docs/progress.md](docs/progress.md)

## Architecture

| Component | Where |
|---|---|
| Console API, Slack ingress, health | `backend/app/api/` (FastAPI) |
| Temporal workers (agent tasks, schedules, approvals) | `backend/app/` `worker` command |
| Maintenance jobs (partitions, retention, reconciliation) | `backend/app/` `maintenance` command |
| SQL migrations | `backend/migrations/NNN_*.sql` |
| Admin console | `web/` (React, TypeScript, Vite) |

**One image, many commands.** `backend/Dockerfile` builds one image; `app <api|worker|maintenance|migrate|seed|check>` (console script, same as `python -m app`) selects the process. The image has no `ENTRYPOINT`; `CMD` is `app api` and every caller passes a full command. On Render ([render.yaml](render.yaml), runbook [docs/runbooks/deploy.md](docs/runbooks/deploy.md)): `api-staging` web service (migrations in `preDeployCommand`), `worker-staging` background worker, `maintenance-staging` cron job, `web-staging` static site, `db-staging` Render Postgres (PgBouncer pool), `kv-staging` Render Key Value. Production is an empty project until phase 13.

## Invariants every phase must keep

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

A change never weakens one of these to make something work. If an invariant blocks progress, stop and raise it.

## Commands

| Command | Does |
|---|---|
| `make install` | Install backend (uv) and web (pnpm) dependencies |
| `make lint` / `make fmt` | Lint and format check / format, both apps |
| `make typecheck` | mypy (strict) and tsc |
| `make test` | All backend and web tests |
| `make ci` | Every CI step locally |
| `make hooks` | Install commit hooks |
| `make dev` | Build and start the whole local stack (Compose), wait until healthy |
| `make infra` | Start only Postgres, Valkey, Temporal, object store, Jaeger |
| `make down` / `make reset` | Stop (keep data) / wipe all local data and start again |
| `make migrate` / `make seed` | Apply migrations / load the dev fixture into the running stack |
| `make smoke` / `make test-image` / `make e2e` | Stack smoke checks / image checks / Playwright |
| `make test-stack` | `dev` + every stack level check |
| `make render-sim` | Execute `render.yaml` locally with Docker the way Render would |
| `make maintenance` | Run the maintenance jobs once (partitions; later reconciliation, retention) |
| `make test-mutations` | Weaken each DB guarantee in turn; the schema suite must catch every one |

Backend only: `cd backend && uv run pytest -k name`, `uv run python -m app api`.
Web only: `pnpm --dir web dev`.

Local URLs: API http://localhost:8000 (`/healthz`, `/readyz`), console http://localhost:5173, Temporal UI http://localhost:8233,
Jaeger http://localhost:16686, Postgres `localhost:55432` (postgres/postgres, db `agent`), PgBouncer `localhost:56432`,
Valkey `localhost:56379`, S3 (SeaweedFS) http://localhost:8333.

## Code conventions

- **Python 3.12**, `uv` for dependencies (commit `uv.lock`), `ruff` (lint and format, line length 110), `mypy --strict` on `app/`.
- **Layout** (grows per phase): `app/api/` HTTP layer only (`main.py` factory, `middleware.py`), `app/settings.py` the one typed settings object (environment only, never files in the image), `app/db/` pools, unit of work, error mapping, roles and the migration runner, `app/crypto/` envelope encryption, `app/storage.py` object storage, `app/kv.py` Key Value, `app/temporal.py` Temporal client, `app/logging.py` and `app/telemetry.py` observability, `app/workflows/` Temporal workflows and activities, `app/worker.py` the worker process, `app/maintenance.py` cron jobs, `app/seed/` the dev fixture.
- **Naming**: modules and functions `snake_case`, classes `PascalCase`, SQL identifiers `snake_case`, tables plural.
- **Typing**: full annotations; no `Any` in public signatures unless wrapping untyped JSON.
- **Errors**: raise domain exceptions in services; the API layer maps them to HTTP responses. Never put secrets or tokens in exception messages.
- **Logging**: structured JSON logs (phase 4). Never log tokens, secrets or raw Slack message text.
- **Migrations**: add `backend/migrations/NNN_description.sql`; never edit an applied migration (the runner checks checksums). Files must not contain top level `begin`/`commit`: the runner wraps each file and its bookkeeping row in one transaction. Start with `set local role app_owner;` so objects are owned by `app_owner`. Migrations run as a non superuser (Render-like `agent_admin` locally): never rely on BYPASSRLS; cross tenant work goes through `security definer` functions owned by `app_owner` with `execute` granted only to the role that needs it.
- **Database access by component**: each component connects as its own login user via `Settings.component_dsn("api" | "worker" | "vault" | "identity" | "maintenance")`; never use `MIGRATION_DATABASE_URL` outside `app migrate` / `app seed`.
- **Tenant data in request handlers**: take `uow: UnitOfWork = Depends(get_uow)` and `conn = await uow.bind(org_id)`. Never take a pool connection directly in a request path (only `/readyz` does). The middleware commits before the response starts; raise to roll back. Do not use the unit of work from background tasks or streaming bodies.
- **Errors**: let database errors propagate; `app/db/errors.py` maps them (403/409/422) with generic messages. Unhandled exceptions become a JSON 500 with the request id.
- **Logging**: `log = logging.getLogger(__name__)`, structured fields via `extra={...}`. Request id, org id and trace ids are added automatically from context. Never log tokens, secrets, raw Slack text or raw database errors to clients.
- **Tracing**: binding a unit of work tags spans with `tenant.id` automatically; spans never include SQL parameters or query strings.
- **Secrets at rest**: `Envelope.seal(org_id, plaintext, context="<table>:<row id>")`; store `ciphertext` and `kms_key_id`. Never put plaintext secrets in the database, logs or model context.
- **Object storage**: `ObjectStore.put(org_id, kind, data)` returns the `storage_ref`; reads and deletes need the same org id.
- **Key Value**: build tenant keys with `kv.tenant_key(org_id, ...)`; `kv.global_key` only for data that is not tenant scoped. New tables need explicit grants and, if tenant scoped, the `tenant_isolation` policy and composite `(org_id, id)` foreign keys. Migrations must be safe to run against the previous code version.
- **Adding a tool adapter**: arrives in phase 8.
- **Adding a playbook**: arrives in phase 7.
- **Web**: TypeScript strict, ESLint + Prettier, components in `PascalCase.tsx`, tests next to code as `*.test.tsx`.

## Testing rules

- Real Postgres for anything touching the database. Never mock row level security.
- Every new tenant table gets an isolation test (org A cannot read, insert, update, link or delete org B's rows; no context means no rows): add it to `TENANT_TABLES` in `tests/schema/conftest.py` and give the `world` fixture a row for it; the generic suite in `tests/schema/test_isolation.py` then covers it (a meta test fails until you do).
- New database guarantees get a test in `tests/schema/test_guarantees.py` and, ideally, a mutation in `scripts/schema_mutations.sh`.
- Tests for security properties assert the denial, not just the happy path.
- Browser flows are tested end to end with Playwright.

## Per phase workflow

1. Read this file, the relevant spec sections and the phase in the plan.
2. Write the detailed plan to `docs/plans/phase_NN_<name>.md` before changing code.
3. Implement on a `phase-NN-<name>` branch in small commits; merge to `main` when the phase closes.
4. Test deeply: unit, integration, end to end, failure paths and tenant isolation. Fix and retest until clean.
5. Record: update this file (commands, conventions, gotchas), append to `docs/progress.md`, and log decisions and blockers in `spec/decisions_and_blockers.md`.
6. Close the phase only when every "Done when" item is true (or blocked, with a workaround logged).

## Gotchas

- A stray `VIRTUAL_ENV` in the shell makes `uv` warn; the Makefile unexports it. Run uv commands from `backend/`.
- Starlette's test client wants `httpx2` installed (it is a dev dependency).
- The Vite template now ships oxlint; this repo uses ESLint + Prettier per the plan.
- MinIO no longer publishes images; the local S3 stand-in is SeaweedFS (`objectstore` service, bucket `artifacts`).
- Backend Compose services share one image built by the `migrate` service (`pull_policy: never` on the rest).
- Integration tests skip (with a reason) when Postgres or Temporal are not reachable; `make test-backend` starts them.
- DB tests create a throwaway database per test or module (`tests/conftest.py`); roles are cluster wide and shared.
- `test_initial_migration_matches_spec_schema...` fails if `001_initial_schema.sql` drifts from `spec/schema.sql`.
- Every env var `render.yaml` sets for a backend service must be a `Settings` field (`tests/test_render_blueprint.py`); add the field first.
- Empty optional secrets (`KEY=`) are treated as unset by `Settings`.
- Docker Desktop does not share `/tmp` with its VM; scripts that bind mount use `~/.cache/...`. Files a container writes there may be owned by a remapped uid: delete them from a container.
- Roles are cluster wide. Tests create login users with unique names and drop them; never touch the real `*_service` users from tests (the running dev stack uses them).
- After changing `dev/postgres/init/` or editing migration 001, run `make reset` (init scripts run only on an empty volume; edited migrations fail the checksum).
- A CREATEROLE non superuser cannot mention `SUPERUSER`/`BYPASSRLS` in `ALTER ROLE`; `app/db/roles.py` verifies attributes instead.
- Logs go to stdout as JSON (one line each), not stderr; tests and scripts that wait for log lines must read stdout.
- Tracing is configured once per process (`configure_tracing` is idempotent); tests configure it first in `tests/conftest.py` with an in-memory exporter (`SPANS`).
- Use `httpx.ASGITransport(app=..., raise_app_exceptions=False)` in API tests, or unhandled exceptions are re-raised instead of returning the 500 the server would send.
- Jaeger v2 dropped the legacy `/api/traces` query API; use `/api/v3/traces/<trace id>`.
