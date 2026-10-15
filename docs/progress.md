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
