# Phase 2: First Render deployment (walking skeleton)

**Goal:** prove the deploy path end to end before there is real functionality.

**Done when:** a push to the main branch deploys all services to staging, health checks pass, the console loads, and a pull request can spin up a preview environment.

## Blocker and workaround

There is no Render account (B-001) and no GitHub remote (B-002), so nothing can actually be deployed. The phase delivers everything that does not need an account and proves it as far as possible locally:

1. `render.yaml` validated against Render's published JSON schema (`https://render.com/schema/render.yaml.json`, vendored in `dev/schemas/`).
2. Semantic checks the schema cannot express (references resolve, commands exist in the CLI, env vars match the settings the code reads, secrets are never literal values).
3. A **local Render simulation** that reads `render.yaml` and executes it the way Render does: build the image from `dockerContext`/`dockerfilePath`, run `preDeployCommand` with the env vars the Blueprint wires, start each service with `PORT=10000` (Render's default), probe `healthCheckPath`, stop with SIGTERM within `maxShutdownDelaySeconds`, run the cron command once, build the static site with its `buildCommand` from a clean checkout and serve `staticPublishPath` with its rewrite rules.

## Decisions to make here

| Topic | Choice |
|---|---|
| One `api` service vs separate ingress and console | One service (plan recommendation); split later by deploying the same image twice |
| Region | `oregon` for staging; region per project later for residency |
| Projects | Two Blueprint projects: `agent-platform-staging` (environment `staging`, all resources) and `agent-platform-production` (environment `production`, empty until phase 13). Resource names carry the environment suffix (`api-staging`) because names must be unique within a Blueprint and production will live in the same file |
| Start commands | Drop the image `ENTRYPOINT`; `CMD ["app", "api"]` with an `app` console script. Render's docs only say `dockerCommand` replaces `CMD`; without an `ENTRYPOINT` the command works whether Render replaces `CMD` alone or the whole entry point |
| Deploy trigger | `autoDeployTrigger: checksPass` on `main`, so staging deploys only after CI passes |
| Build filters | Backend services rebuild on `backend/**` and `render.yaml`; the static site on `web/**` |
| Key Value eviction | `noeviction`: dedupe keys and locks must not be silently evicted; memory pressure surfaces as errors (and alerts in phase 13) |
| Temporal Cloud | The worker gains API key + TLS support (`TEMPORAL_API_KEY`); the address and namespace are `sync: false` |
| Console API URL | `VITE_API_URL` is a `sync: false` build variable on the static site (Blueprints cannot reference another web service's public URL) |

## Tasks

1. `pyproject.toml`: `[project.scripts] app = "app.cli:main"`. Dockerfile `CMD ["app", "api"]`, no `ENTRYPOINT`. Compose commands become `["app", "<command>"]`.
2. Settings: `temporal_api_key` (secret) and TLS when set; `redis_url` accepted for later phases.
3. `render.yaml`:
   - `api-staging` (web, Docker): `dockerCommand: app api`, `preDeployCommand: app migrate`, `healthCheckPath: /healthz`, `maxShutdownDelaySeconds: 30`, previews on a small plan.
   - `worker-staging` (worker, Docker): `app worker`, `maxShutdownDelaySeconds: 120` (worker drains for 100 s).
   - `maintenance-staging` (cron, Docker): `app maintenance`, daily at 03:17 UTC.
   - `web-staging` (static): pnpm build from the lock file, publish `web/dist`, rewrite `/*` to `/index.html`, security headers.
   - `db-staging` (Postgres 16, PgBouncer pool on), `kv-staging` (Key Value, no external access).
   - Env groups: `backend-staging` (shared non secret settings). Secrets as `sync: false` on services.
   - `previews.generation: manual`, `expireAfterDays: 3`, preview plans and `previewValue` overrides.
4. `backend/tests/test_render_blueprint.py`: schema validation plus semantic checks (below).
5. `scripts/render_sim.py` (+ `make render-sim`): the local Render simulation described above.
6. `docs/runbooks/deploy.md`: first time setup (Blueprint creation, secrets to enter, Temporal Cloud, DNS), routine deploys, previews, rollback, what to check after a deploy.

## Tests

| Test | Checks |
|---|---|
| Schema | `render.yaml` validates against the vendored Render schema; a deliberately broken copy (unknown key, bad plan) fails |
| References | every `fromDatabase` / `fromService` / `fromGroup` points at a resource in the same environment |
| Commands | every `dockerCommand` / `preDeployCommand` is `app <subcommand>` and the subcommand exists in the CLI parser |
| Health check | `healthCheckPath` returns 200 from the real app |
| Env vars | every key set for a backend service is a field of `Settings` (catches typos); keys named like secrets are never literal values; `ENV` is `staging` |
| Shutdown | worker's `maxShutdownDelaySeconds` exceeds `WORKER_GRACEFUL_SHUTDOWN_SECONDS`; API's exceeds uvicorn's graceful timeout |
| Static site | publish path is `web/dist`, SPA rewrite present, build uses the frozen lock file |
| Data stores | Postgres major version 16+, PgBouncer on; Key Value has an empty IP allow list |
| Production project | exists and has no resources yet |
| Simulation | `make render-sim` passes: migrations via `preDeployCommand`, API healthy on port 10000, worker connects to Temporal, both stop on SIGTERM inside their delay, cron runs, static build from a clean clone serves `/` and deep links |

## Risks

- **Unverifiable until an account exists:** preview environments with projects, `checksPass` behavior, how `dockerCommand` and `preDeployCommand` treat the image, Render Postgres role capabilities (phase 3). All recorded under B-001.
