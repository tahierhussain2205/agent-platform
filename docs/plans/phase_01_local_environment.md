# Phase 1: Dockerization and local environment

**Goal:** the whole system runs locally with one command.

**Done when:** `make dev` brings everything up from a clean checkout, `api` answers `/healthz`, the worker connects to Temporal, and the web app loads.

## Design points

- **Same image locally and on Render.** Compose builds `backend/Dockerfile` and runs it with the same subcommands Render will use. No Compose only entrypoints or init scripts for application behavior.
- **Migrations before the API**, like Render's `preDeployCommand`: a one shot `migrate` service runs to completion before `api` and `worker` start.
- **Seeding needs a schema**, so a minimal migration runner is pulled forward from phase 3 (apply files in order, record them, refuse edited migrations, advisory lock). Phase 3 adds Render compatibility, login users per component and the ported schema tests.

## Tasks

### 1. `backend/Dockerfile`

- Multistage: a `builder` stage with `uv` copied from the official image installs dependencies from `uv.lock` (`--frozen --no-dev`), then the project; the `runtime` stage is `python:3.12-slim` with only the virtualenv, `app/` and `migrations/`.
- Runs as a non root user (uid 10001), `PYTHONUNBUFFERED=1`, bytecode compiled at build time.
- `ENTRYPOINT ["python", "-m", "app"]`, default `CMD ["api"]`. Exec form so Python is PID 1 and receives SIGTERM directly; every long running command installs its own SIGTERM handling.
- `.dockerignore` keeps `.venv`, caches and tests out of the build context.

### 2. Worker skeleton (`python -m app worker`)

- Connects to Temporal (`TEMPORAL_ADDRESS`, `TEMPORAL_NAMESPACE`), polls `TEMPORAL_TASK_QUEUE`.
- Registers a `PingWorkflow` and `ping` activity used by smoke tests to prove a worker is connected and polling.
- Retries the initial connection with backoff (Temporal may start after the worker in Compose).
- SIGTERM: stop polling, let in-flight activities finish (graceful timeout from settings), exit 0.

### 3. Minimal migration runner (`python -m app migrate`)

- Reads `backend/migrations/NNN_*.sql` in order; connects with `MIGRATION_DATABASE_URL` (a direct connection, never the pooler, because it holds a session advisory lock).
- Bookkeeping table `public.schema_migrations (version, name, checksum, applied_at)`.
- Each pending file runs inside one transaction together with its bookkeeping row. Migration files therefore must not contain top level `begin`/`commit`; `001_initial_schema.sql` has those two lines removed (the rest stays verbatim).
- A changed checksum for an applied migration aborts with an error.
- `pg_advisory_lock` serializes concurrent runs (two deploys at once).
- Database driver: `psycopg` 3 (async, `psycopg_pool`). Chosen now because the same driver serves the app; it supports multi statement scripts and `prepare_threshold=None` for PgBouncer transaction mode later.

### 4. Seed (`python -m app seed`)

- One fixture file, `backend/app/seed/fixture.json`, so later fakes (Clerk, Slack) can load the same people and channels.
- Contents: two orgs (Acme, Globex) each with an admin and users, an invited user who has not signed up, one person in both orgs, roles with a primary role, one Slack workspace per org, channels (allowlisted public, allowlisted private, not allowlisted, archived), platform identities and channel memberships.
- Runs as `app_maintenance` (`set local role`) over the migration connection, so it exercises the maintenance grants rather than superuser powers. Idempotent: upserts by natural keys (`clerk_org_id`, email, workspace external id, channel external id).

### 5. `compose.yaml`

| Service | Image | Host port |
|---|---|---|
| `postgres` | `postgres:16` | 55432 |
| `valkey` | `valkey/valkey:8-alpine` | 56379 |
| `temporal` | `temporalio/temporal` (`server start-dev`, UI included) | 7233, UI 8233 |
| `minio` + `minio-init` | MinIO + `mc` creates the artifacts bucket | 9000, console 9001 |
| `jaeger` | `jaegertracing/jaeger` (OTLP in, UI) | 16686, 4317, 4318 |
| `migrate` | backend image, `migrate` (one shot) | |
| `seed` | backend image, `seed` (one shot, after `migrate`) | |
| `api` | backend image, `api` | 8000 |
| `worker` | backend image, `worker` | |
| `web` | `node:22-slim`, `pnpm dev` with the source mounted (hot reload) | 5173 |

Health checks on every long running service; dependencies use `service_healthy` / `service_completed_successfully`. Host ports are offset to avoid clashing with services already on developer machines.

### 6. Make targets

- `make dev`: build and start everything, wait until healthy.
- `make infra`: start only the infrastructure (for running `api`/`worker` on the host with `uv run`).
- `make down`: stop containers, keep data. `make reset`: stop and delete volumes, then `make dev`.
- `make seed`, `make migrate`: run the one shot commands against the running stack.
- `make logs`, `make ps`, `make smoke`.

### 7. Smoke check (`make smoke`, `scripts/smoke.sh`)

- `/healthz` on the API returns ok.
- `PingWorkflow` executes through Temporal and a worker returns `pong` (proves the worker is connected and polling).
- The web dev server serves the console HTML.
- MinIO has the artifacts bucket; Jaeger and Temporal UIs respond.
- Postgres has the migration recorded and the seeded orgs.

## Tests

| Test | Kind | Checks |
|---|---|---|
| Migration runner | integration (real Postgres, throwaway database per test session) | applies 001 on an empty database; second run is a no op; a changed checksum aborts; a failing migration rolls back and is not recorded; files containing top level `begin`/`commit` are rejected; concurrent runs serialize |
| Seed | integration | runs twice without duplicates; counts match the fixture; the invited user is `invited` with no `clerk_user_id`; the person in both orgs has two memberships; every role holder has exactly one primary role; RLS still isolates the seeded orgs when read as `app_user` |
| Worker | integration (Temporal dev server) | `PingWorkflow` returns `pong`; SIGTERM stops the worker with exit code 0 |
| Docker image | live | runs as non root; `--help` lists commands; `api` honors `PORT`; SIGTERM stops `api` and `worker` cleanly inside the container |
| Stack | live | `make reset` from nothing, then `make smoke` passes; the console loads in a browser (Playwright) |

Integration tests are skipped with a clear reason when their service is not reachable, and `make test` brings up what they need first.

## Risks

- **Image availability**: MinIO changed how it distributes community images in 2025. If `minio/minio` cannot be pulled, use another S3 compatible server.
- **Disk space** on this machine is limited (about 20 GB free); keep images slim and prune dangling layers.
