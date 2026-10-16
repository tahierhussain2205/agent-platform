# Runbook: deploying to Render

Everything Render runs is defined in [`render.yaml`](../../render.yaml). The dashboard is only used to create the Blueprint once and to enter `sync: false` secrets. Never change service settings in the dashboard: the next Blueprint sync overwrites them.

> Status: staging was first deployed on 2026-10-16 (B-001). Previews and `checksPass` on a real push are still unverified.

## What the first deploy showed

- Render adds a suffix to a service's URL when the name is taken (staging: `https://api-staging-u9gh.onrender.com`, `https://web-staging-o82n.onrender.com`). After the first deploy, set `API_PUBLIC_URL`, `CONSOLE_URL` and `CONSOLE_ORIGINS` on `api-staging`, `CONSOLE_URL` on `worker-staging` and `VITE_API_URL` on `web-staging`, then redeploy them (the console bakes `VITE_API_URL` in at build time).
- On a first deploy the vault and worker start before the API's pre-deploy migration has created their login users: their logs show `FATAL: no such user` until it has, then the pools connect. Later deploys do not have this window.
- Migration 001 logs `skipped, needs a superuser` for the `bypassrls` statements: expected (Render's user has `CREATEROLE`, is not a superuser; B-010).
- `sync: false` values left empty in the Blueprint form are not stored; enter them later as their blocker is cleared.
- Checks run as one-off jobs with the service's environment (dashboard, service, Jobs, or the API): `app check vault` and `app check temporal` from `api-staging`; `app check model` from `worker-staging` (once phase 12 is on `main`).

## Resources

| Resource | Type | Command / notes |
|---|---|---|
| `api-staging` | Web service (Docker) | `app api`; `preDeployCommand: app migrate`; health check `/healthz` |
| `worker-staging` | Background worker (Docker) | `app worker`; 120 s shutdown delay, drains for 100 s |
| `vault-staging` | Private service (Docker) | `app vault` on port 10000; the only holder of `ENCRYPTION_KEYS`, the vault database user, provider client secrets and the GitHub App key; the API and worker reach it at its `hostport` with `VAULT_SERVICE_TOKEN` |
| `maintenance-staging` | Cron job (Docker) | `app maintenance` every 15 minutes (partitions, Slack and task reconciliation, schedule sync; all idempotent) |
| `web-staging` | Static site | pnpm build, `web/dist`, SPA rewrite |
| `db-staging` | Render Postgres 16 | PgBouncer pool enabled, no external access |
| `kv-staging` | Render Key Value | `noeviction`, private network only |
| `agent-platform-production` | Project | Empty until phase 13 |

## First time setup (unverified)

1. **Repository**: push the repo to GitHub and make sure the CI workflow runs (staging deploys use `autoDeployTrigger: checksPass`, so they wait for CI).
2. **Temporal Cloud**: create a namespace and an API key. Note the gRPC endpoint (`<namespace>.<account>.tmprl.cloud:7233`).
3. **Blueprint**: Render dashboard, New, Blueprint, pick the repo and `main`. Render reads `render.yaml` and lists the resources.
4. **Secrets** Render prompts for (`sync: false`):

   | Service | Key | Value |
   |---|---|---|
   | `api-staging`, `worker-staging` | `TEMPORAL_ADDRESS` | Temporal Cloud endpoint |
   | `api-staging`, `worker-staging` | `TEMPORAL_NAMESPACE` | `<namespace>.<account>` |
   | `api-staging`, `worker-staging` | `TEMPORAL_API_KEY` | Temporal Cloud API key |
   | `web-staging` | `VITE_API_URL` | Public URL of `api-staging` (for example `https://api-staging.onrender.com`). Known only after `api-staging` exists: enter a placeholder, then set it and redeploy `web-staging` |
   | `vault-staging` | `ENCRYPTION_KEYS` | `v1:<base64 32 bytes>` (see the comment in `render.yaml`); keep a copy offline: losing it loses every stored token |
   | `vault-staging` | `ATLASSIAN_CLIENT_ID`/`_SECRET`, `GITHUB_APP_ID`, `GITHUB_APP_PRIVATE_KEY`, `GITHUB_CLIENT_ID`/`_SECRET`, `GOOGLE_CLIENT_ID`/`_SECRET` | The provider apps (B-007); the private key as one line with `\n` escapes |
   | `api-staging` | `ATLASSIAN_CLIENT_ID`, `GITHUB_APP_SLUG`, `GITHUB_CLIENT_ID`, `GOOGLE_CLIENT_ID`, `GOOGLE_MCP_URL` | Public client ids for the authorize URLs |

   Every other `sync: false` key in `render.yaml` (Clerk, Slack, Anthropic, object storage, tracing) is documented in `.env.example`.

5. **Apply**. The first deploy runs `app migrate` before `api-staging` starts. If migrations fail, the deploy stops and the previous version (none, the first time) keeps running.
6. **Database roles** (phase 3): check whether the default Render user can create roles and grant `BYPASSRLS`; see the phase 3 notes in `spec/decisions_and_blockers.md`.

## Routine deploy

1. Merge to `main`.
2. CI runs. When checks pass, Render builds the backend image once per Docker service (build filters skip services whose paths did not change) and the static site.
3. `api-staging` runs `app migrate` in a pre-deploy instance, then rolls out with zero downtime behind `/healthz`. Workers and the cron job pick up the new image.

**Migration rule:** a migration must work with both the previous and the new code, because the old version keeps serving traffic while the new one starts. Add columns and tables first; remove them in a later release.

## After every deploy

- `api-staging`: `/healthz` returns `{"status":"ok"}`; the deploy log shows the migration line (`applied N migration(s)` or `none pending`).
- `worker-staging`: logs show `worker polling task queue 'agent-tasks'`. From the service's Shell tab, `app check temporal` prints `pong: smoke`.
- `web-staging`: the console loads; a deep link (for example `/settings`) loads the app, not a 404.

## Preview environments (unverified)

`previews.generation` is `manual`: add `[render preview]` to a pull request title to create one. Previews use the `starter` plans and the `previewPlan` values, and expire after 3 days without updates. Autoscaling is not available in previews.

## Rollback

1. Dashboard, service, Events: roll back to the previous successful deploy (do the same for `worker-staging` if it changed).
2. Migrations are not rolled back. Because every migration is backward compatible (rule above), the previous code runs against the newer schema. If a migration itself is wrong, fix it forward with a new migration.

## Shutdown behavior

Render sends SIGTERM and waits `maxShutdownDelaySeconds`. The API drains in-flight requests (uvicorn, 25 s); the worker stops polling and waits up to `WORKER_GRACEFUL_SHUTDOWN_SECONDS` (100 s) for running activities before cancelling them; Temporal retries cancelled activities on another worker.

## Checking the Blueprint without Render

- `make test-backend` includes `tests/test_render_blueprint.py` (schema plus semantic checks).
- `make render-sim` runs the Blueprint locally with Docker: migrations via `preDeployCommand`, services on port 10000, health checks, SIGTERM within the shutdown delay, cron, and the static build from a clean copy.
