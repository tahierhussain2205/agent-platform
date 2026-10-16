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
| Slack client, rate limiter, Block Kit adapter, install/channel services | `backend/app/slack/` |
| Agent: playbooks, prompts, the loop's steps (runner), checks on proposed tickets (`tickets.py`), investigations (`investigation.py`), outbox | `backend/app/agent/` |
| LLM gateway (tiers, budgets, rate limits, retries, metering) | `backend/app/llm/` |
| Policy module and redaction | `backend/app/policy/` |
| Tool adapter interface, executor and adapters (Slack, Jira, GitHub, MCP, the service catalog) | `backend/app/tools/` |
| Service catalog proposals (GitHub CODEOWNERS, Jira components) and their workflow | `backend/app/catalog/`, `backend/app/workflows/catalog.py` |
| Observability interface (log backends: Google Cloud Logging) | `backend/app/observability/` |
| Sandboxes (provider interface, per task working copies, baseline and export) | `backend/app/sandbox/`; local runner `fakes/fakes/sandbox_runner.py` (`sandbox-runner` service, no network) |
| Changing code: implementation steps, diff checks, setup recipes, GitHub notices | `backend/app/agent/implementation.py`, `changes.py`, `recipes.py`, `notices.py`; pusher `backend/app/tools/github_changes.py`; sandbox tools `backend/app/tools/sandbox.py` |
| Integration catalog, OAuth providers, signed state | `backend/app/integrations/` |
| Token vault service (secrets, OAuth exchange and refresh, task scoped credentials) | `backend/app/vault_service/` (`app vault`) |
| Task workflow and activities | `backend/app/workflows/task*.py` |
| Scheduled reports: cadences, figures, validation, Temporal mirror | `backend/app/schedules/`, `backend/app/agent/reports.py`, `backend/app/workflows/reports.py` |
| Eval harness and cases | `backend/app/eval/` (`app eval`) |
| Temporal workers (agent tasks, schedules, approvals) | `backend/app/` `worker` command |
| Maintenance jobs (partitions, retention, reconciliation) | `backend/app/` `maintenance` command |
| SQL migrations | `backend/migrations/NNN_*.sql` |
| Admin console | `web/` (React, TypeScript, Vite, React Router, TanStack Query, Tailwind) |
| Local SaaS stand-ins (Clerk, Slack, Anthropic, Atlassian, GitHub, Google, MCP servers) | `fakes/` (own image, Compose only, never deployed) |

**One image, many commands.** `backend/Dockerfile` builds one image; `app <api|worker|maintenance|migrate|seed|check>` (console script, same as `python -m app`) selects the process. The image has no `ENTRYPOINT`; `CMD` is `app api` and every caller passes a full command. On Render ([render.yaml](render.yaml), runbook [docs/runbooks/deploy.md](docs/runbooks/deploy.md)): `api-staging` web service (migrations in `preDeployCommand`), `worker-staging` background worker, `vault-staging` private service (`app vault`), `maintenance-staging` cron job, `web-staging` static site, `db-staging` Render Postgres (PgBouncer pool), `kv-staging` Render Key Value. Production is an empty project until phase 13.

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
| `make maintenance` | Run the maintenance jobs once (partitions, Slack and task reconciliation, schedule sync; later retention) |
| `make test-fakes` | Tests of the SaaS stand-ins |
| `make test-mutations` | Weaken each DB guarantee in turn; the schema suite must catch every one |
| `make eval` | Replay the agent eval cases against the running stack (`app eval --report FILE --min-score X`) |

Backend only: `cd backend && uv run pytest -k name`, `uv run python -m app api`.
Web only: `pnpm --dir web dev`.

Local URLs: API http://localhost:8000 (`/healthz`, `/readyz`), console http://localhost:5173 (seeded accounts: alice@acme.test, bob@acme.test, frank@both.test, dave@globex.test; password `password123`), fakes http://localhost:4100 (Clerk stand-in at `/clerk`, Slack stand-in at `/slack` with a Slack-like UI at http://localhost:4100/slack/ui, Anthropic stand-in at `/anthropic`, Atlassian at `/atlassian`, GitHub at `/github` (a page per pull request at `/github/{owner}/{repo}/pull/{n}`), Google at `/google`, MCP servers at `/google/mcp` and `/mcp/status`, Google Cloud at `/gcp`, a sandbox provider at `/sandbox/v1`, test admin at `/_admin`); the vault service listens on `vault:8100` inside Compose only, Temporal UI http://localhost:8233,
Jaeger http://localhost:16686, Postgres `localhost:55432` (postgres/postgres, db `agent`), PgBouncer `localhost:56432`,
Valkey `localhost:56379`, S3 (SeaweedFS) http://localhost:8333.

## Code conventions

- **Python 3.12**, `uv` for dependencies (commit `uv.lock`), `ruff` (lint and format, line length 110), `mypy --strict` on `app/`.
- **Layout** (grows per phase): `app/api/` HTTP layer only (`main.py` factory, `middleware.py`), `app/settings.py` the one typed settings object (environment only, never files in the image), `app/db/` pools, unit of work, error mapping, roles and the migration runner, `app/crypto/` envelope encryption, `app/storage.py` object storage, `app/kv.py` Key Value, `app/temporal.py` Temporal client, `app/logging.py` and `app/telemetry.py` observability, `app/workflows/` Temporal workflows and activities, `app/worker.py` the worker process, `app/maintenance.py` cron jobs, `app/seed/` the dev fixture.
- **Naming**: modules and functions `snake_case`, classes `PascalCase`, SQL identifiers `snake_case`, tables plural.
- **Typing**: full annotations; no `Any` in public signatures unless wrapping untyped JSON.
- **Errors**: raise domain exceptions in services; the API layer maps them to HTTP responses. Never put secrets or tokens in exception messages.
- **Logging**: structured JSON logs (phase 4). Never log tokens, secrets or raw Slack message text.
- **Migrations**: add `backend/migrations/NNN_description.sql`; never edit an applied migration (the runner checks checksums). Files must not contain top level `begin`/`commit`: the runner wraps each file and its bookkeeping row in one transaction. Start with `set local role app_owner;` so objects are owned by `app_owner`. Migrations run as a non superuser (Render-like `agent_admin` locally): never rely on BYPASSRLS; cross tenant work goes through `security definer` functions owned by `app_owner` with `execute` granted only to the role that needs it. In SQL functions, never select-then-insert on a unique key (two transactions both miss and one fails): use `insert ... on conflict`.
- **Database access by component**: each component connects as its own login user via `Settings.component_dsn("api" | "worker" | "vault" | "identity" | "maintenance")`; never use `MIGRATION_DATABASE_URL` outside `app migrate` / `app seed`.
- **Tenant data in request handlers**: take `uow: UnitOfWork = Depends(get_uow)` and `conn = await uow.bind(org_id)`. Never take a pool connection directly in a request path (only `/readyz` does). The middleware commits before the response starts; raise to roll back. Do not use the unit of work from background tasks or streaming bodies.
- **Errors**: let database errors propagate; `app/db/errors.py` maps them (403/409/422) with generic messages. Unhandled exceptions become a JSON 500 with the request id.
- **Logging**: `log = logging.getLogger(__name__)`, structured fields via `extra={...}`. Request id, org id and trace ids are added automatically from context. Never log tokens, secrets, raw Slack text or raw database errors to clients.
- **Tracing**: binding a unit of work tags spans with `tenant.id` automatically; spans never include SQL parameters or query strings.
- **Secrets at rest**: `Envelope.seal(org_id, plaintext, context="<table>:<row id>")`; store `ciphertext` and `kms_key_id`. Never put plaintext secrets in the database, logs or model context.
- **Connection secrets and credentials** go through the `VaultClient` (`resources.vault`, `AgentDeps.vault`): `RemoteVault` when `VAULT_URL` is set (Compose, Render), `LocalVault` in process otherwise. Commit the `connections` row before calling the vault (it uses its own connection). Ask for a credential with the task id (`credential(org, connection, task_id=...)`): user links are released only for their owner's running task. Only the vault reads client secrets, the GitHub App key and `ENCRYPTION_KEYS`.
- **OAuth flows** (`app/api/oauth.py`): state from `app.integrations.state.make_state` (signed, ten minutes), nonce claimed once in Key Value, PKCE verifier in Key Value; callbacks redirect to `CONSOLE_URL/integrations?connected=|linked=|error=`. Authorize URLs (browser) and token/API URLs (server) are separate settings.
- **Object storage**: `ObjectStore.put(org_id, kind, data)` returns the `storage_ref`; reads and deletes need the same org id.
- **Key Value**: build tenant keys with `kv.tenant_key(org_id, ...)`; `kv.global_key` only for data that is not tenant scoped.
- **Console routes**: depend on `console_identity` (any active member) or `require_admin`; both bind the unit of work to the verified org. Privilege always comes from our database. Every mutation writes an audit row (`app/api/console/common.audit`).
- **Identity changes** (invite, privilege, removal) go to Clerk: apply through the identity function in an `identity_tx`, call Clerk inside it, commit only if Clerk succeeded. Never write `org_memberships` from request code.
- **Identity functions** take the event time; events older than a membership's `identity_event_at` are ignored (webhooks arrive late or out of order).
- **External service stand-ins** live in `fakes/` and mirror the real HTTP API shapes; code under test only sees base URLs. Update the stand-in when the code starts using another endpoint. New tables need explicit grants and, if tenant scoped, the `tenant_isolation` policy and composite `(org_id, id)` foreign keys. Migrations must be safe to run against the previous code version.
- **Slack ingress** (`app/api/slack.py`): verify the signature on the raw body first, claim the event id in Key Value (release it and return 500 if starting the workflow fails), answer within 3 seconds, and start workflows with an id derived from the event id. Workflow inputs carry references only (team, channel, ts, user, event id), never message text (invariant 7).
- **Talking to Slack** from activities: `ChannelAdapter.send(team, token, intent)` with an intent from `app/slack/adapter.py` (Block Kit rendering and limits, rate limiting and coalescing are handled there); raw `SlackClient.call` only for reads. Get the token with `service.bot_token(pool, vault, org_id)`. Turn permanent Slack errors into non retryable `ApplicationError`s (`slack_activities.permanent`). Mark sends idempotent (the activity can run twice).
- **Activities** open their own tenant transactions with `tenant_tx(pool, org_id)` (sets `app.org_id` first); never share a connection across awaits on Slack.
- **Model calls** only through `LLMGateway.complete(CallContext(...), tier, ...)`: it resolves the tier against what the org allows, refuses when a budget is spent (`BudgetExceededError`), rate limits per org, retries and falls back, meters usage and traces without content. Structured output: a forced `submit_*` tool (`app/agent/prompts.py`), validated in code before use.
- **No raw Slack text in Temporal history** (invariant 7): read Slack and call the model inside the same activity; return only derived content and source references (`app/agent/types.py`).
- **Adding a tool adapter**: implement `ToolAdapter` (`app/tools/base.py`) with `Operation` metadata (kind, reversible, outward facing, default credential mode, approver rule), `resolve_target` and `execute(ctx, operation, args, credential)`; add it to `CATALOG` (`app/integrations/catalog.py`) and `build_registry` (`app/tools/executor.py`), and to `operations()` in the console API. Call tools only through `ToolExecutor.run`: it resolves the target, asks the policy module, fetches the credential for the operation's mode (`org`, `org_with_check`, `user_required`) and audits. For `org_with_check`, filter results to what the requester may see; label sources only some people may see `visibility="restricted"` and implement `audience_ok` so channel output can be checked. Cap and redact output; cite with `ctx.refs.add(Source(...))`. Send provider requests through `app.tools.http.request` (reads retried once after a transport failure, writes never). Add the provider's endpoints to the stand-in. A long tail tool is an MCP server, not an adapter.
- **Scheduled reports**: a schedule row is the truth; `app.schedules.temporal.sync_org` mirrors it into Temporal (only rows with `synced_revision` behind `revision`). After a change, start a sync with `start_sync(await temporal_client(request), ...)` (in console routes as a background task, so it runs after the commit). Save schedules through `app.schedules.service.validate` (the owner's access, not the editor's). Report figures come from internal operations (`Operation.internal`, through the `ToolExecutor`); render numbers with `figure_lines` and pass narratives through `verify_narrative`. Pausing for a reason goes through `ReportRunner.pause` (stamps the detail for admin notices).
- **Adding a playbook**: add `app/agent/playbooks/<key>.v<N>.md` (front matter: key, version, title, description, `read:`/`write:` operation lists). Never edit a released version: add `v<N+1>` (sync refuses changed content). `app migrate` syncs built ins; tasks pin the version they started with.
- **External writes from the agent** go through `post_once` (`app/agent/outbox.py`) or `AgentRunner.execute`: an `external_actions` key checked first, the key also in Slack message metadata. A write to another system also carries the key (a Jira label `marker(key)`, a comment marker) and looks it up before acting, so a crash between the provider's answer and our bookkeeping never writes twice.
- **Jira tickets**: the model proposes, `app/agent/tickets.py` decides (duplicates of open issues, assignee evidence from `catalog_find_owners` or the request, project and component from the matched service, Slack links, private sources always ask); the policy then applies tiers and modes. Keep new checks there, in code, with a reason for the thread. Only confirmed catalog services suggest owners.
- **Logs**: the agent reads a service's logs only through the observability interface and the service's catalog log source (`services.log_source`); a new backend implements `LogBackend` and a tool adapter with `family = "observability"`. Never build a query from model text except as a quoted literal (`app.observability.gcp.quoted`).
- **Code in sandboxes**: read repository code through the GitHub code operations (`app/tools/github_code.py`), which build the task's sandbox (`app.sandbox.workspaces.Workspaces`) at a pinned commit. Tokens never go into a sandbox: the platform fetches the archive and uploads it. Secret files are never read, searched, listed or shown (`secret_path`).
- **Investigations**: the case file is found in code (`Investigation.orient`); investigators get only their tools (`investigation.TOOLS`); new rules for plans go in `check_plan`, in code, with a reason in `not_verified`.
- **Changing code** (`implement_change`): the working copy is prepared in code (`github.prepare_workspace`, with `push: true` checking write access; baseline commit). The implementer changes it only through the sandbox tools; verification, the reproduction check, the review and the export are steps of `Implementation`. Rules about what may be pushed go in `app.agent.changes.check_change` (blocking or "needs a person", with a reason); the pusher re-runs the blocking ones. Never push from anywhere but `github.push_changes` executed through `AgentRunner.execute` (idempotency key, `task_links`). Branches are always under `agent/`.
- **GitHub feedback**: `/webhooks/github` maps a pull request to its task through `task_links` and starts `GitHubNoticeWorkflow`; notices are read within the requester's access and pass `check_output`. Webhooks never start work on their own.
- **Text into Slack mrkdwn**: anything the model or a person wrote goes through `app.slack.adapter.escape` where it is put into Slack text (not into `plain_text` fields, and not when stored): it cannot become a mention, broadcast or link. Links and mentions our code builds stay as they are.
- **Changing the order of workflow commands** (activities, timers, waits) in a workflow that runs long (tasks, reports): branch on `workflow.patched("<name>")` and keep the old path, so histories of running workflows still replay (see `TaskWorkflow._act_all`).
- **Every policy decision is audited** by the policy module (`actor_type` agent, `actor_id` the requester). Do not add tool calls or outputs that bypass `Policy.check_tool` / `Policy.check_output`.
- **Web**: TypeScript strict, ESLint + Prettier, components in `PascalCase.tsx`, tests next to code as `*.test.tsx`.
- **Console colors and themes**: light (white and greys) and dark (black and greys), chosen with the header's switcher or following the system (`src/theme.ts`, applied before paint by `public/theme-init.js`). Use only the semantic colors from `src/index.css` (`bg-canvas`, `bg-surface`, `bg-muted`, `bg-subtle`, `border-line`, `ring-line-strong`, `text-fg`/`fg-2`/`fg-3`/`fg-4`, `bg-accent`/`text-accent-fg`, and `ok`/`warn`/`bad` with `-soft` and `-line`), never palette colors like `bg-white` or `text-slate-600`: every page must work in both themes. Clerk's widgets get matching colors in `ClerkAuthProvider`. Badge tones: `neutral`, `strong`, `green`, `amber`, `red`.
- **Clickable things show a pointer**: a base rule in `index.css` covers links, buttons, selects, checkboxes, radios and their labels, and gives disabled controls `not-allowed`. Make clickable things real `<button>`s or links (never an `onClick` on a `div`). Table scroll wrappers are `relative overflow-x-auto`, or absolutely positioned children (`sr-only` headers) escape them and widen the page. `e2e/tests/ui.spec.ts` checks cursors, colors from the other theme and phone widths on every page.

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
- `app.model_tier[]` is a domain array psycopg returns as a string: select it as `::text[]`.
- `pydantic.EmailStr` rejects reserved domains such as `.test`; the console validates emails with a plain pattern.
- Clerk Core 3 (`@clerk/react` v6) removed `SignedIn`/`SignedOut`; the console uses `useAuth()` state.
- Webhook lag: right after signup or accepting an invitation `/api/me` can answer `not_a_member` for a moment; the console retries those codes for a few seconds.
- Playwright `globalSetup` resets the stand-ins (`POST /_admin/reset`: Clerk with its signing key kept, and Slack) and re-runs the seed, which also puts fixture orgs back (undeclared members removed, pending access requests dismissed, integrations other than Slack removed, token usage cleared); e2e tests use unique emails, org names and Slack teams. Spec files run in parallel workers and post into the same seeded channels: find your message by text, never by message count.
- The running Compose stack answers every Slack event the stand-in delivers, including those from backend tests that post into seeded workspaces: tests that call our ingress directly should post without mentioning the bot, or expect a second reply. Create a fresh team with `POST /_admin/slack/teams` when a test needs an uninstalled workspace. Backend tests share the stand-in's state with the dev stack and the e2e suite (which, for example, adds the bot to #random): a test that relies on stand-in state sets it up itself.
- Store the bot token only after the install transaction commits: the vault's connection must see the new `connections` row (foreign key).
- A controlled checkbox that waits for a mutation flips back for one render (TanStack Query reports `isPending` a tick late); keep the requested value in local state set in `onChange`.
- In the Slack stand-in's UI, typing `@agent` posts a real mention (`<@BOT>`).
- The Anthropic stand-in is a heuristic "model": it recognises steps by the forced tool name and reads our `[S1]` line format. Changing prompt tags (`<brief>`, `<findings>`, `<thread>`, `<request_ref>`) or tool names means updating `fakes/fakes/anthropic.py`. Script it (`/_admin/anthropic/script`) to play a persuaded model; read `/_admin/anthropic/requests` to assert what reached the model.
- Agent tests leave tasks `running` unless they close them; enough of them hit the org's concurrency limit (`wait_org`). The agent test modules close open tasks after each test.
- Temporal's workflow sandbox re-imports workflow modules: monkeypatching a workflow module constant only works with `UnsandboxedWorkflowRunner` (see the lost signal test). Keep at least one test on the sandboxed `build_worker`.
- Test tokens with a short TTL must be minted when used, not at import: a long earlier module lets module level tokens expire (`token_expired`).
- The Jira stand-in keeps issues between runs: tests delete what they create (`delete_jira_issues`), and modules that count or list issues call `reset_jira_issues()` first (`tests/connect.py`). Stand-in providers whose saved state predates their seed start afresh: bump the seed's `version` when the shape of existing records changes.
- Agent tests on a real Temporal worker use the `live` fixture (`tests/agent/conftest.py`, harness in `tests/agent/live.py`). An answer that cites Jira results goes privately unless everyone in the channel may browse the project: ask in a channel whose members can (a fresh one per test: `_admin/slack/channels` plus `app.channels` and `channel_members` rows).
- Temporal history events are protobuf messages: a sub-message is always truthy; compare `event.event_type` (see the parallel investigators test).
- The investigation answer cites a private repository and the org's logs: in a channel where someone cannot see them it goes privately; tests use a channel of people who can (`ticketsChannel(browser, [...])` in e2e).
- Tests must never start workflows on the dev stack's task queue (`agent-tasks` on localhost:7233): use a unique `TEMPORAL_TASK_QUEUE`, and stub the Temporal backed maintenance jobs when running `app maintenance` from a test. Workflow ids are global per namespace, whatever the queue.
- `app migrate` must not rewrite an unchanged login password: a new SCRAM verifier breaks PgBouncer's pass-through for services that authenticated before it (they lose the database until restarted). `ensure_login_user` logs in first and only alters on failure.
- After changing `fakes/`, rebuild the running stand-ins (`docker compose up -d --build --wait fakes`); the backend tests and e2e use the running container.
- The GitHub stand-in verifies app JWTs with `GITHUB_APP_PUBLIC_KEY` (`dev/github/app-dev-public-key.pem`, local only); a fakes container started without it refuses installation tokens. The vault signs with the matching private key (`GITHUB_APP_PRIVATE_KEY`, `\n` escapes accepted).
- `docker compose up --wait` (2.23) fails when a one-shot container that nothing depends on exits during the wait: make a long running service depend on it (`api` depends on `seed`).
- A run reports on the window ending at its scheduled minute: messages posted after that minute's start belong to the next run (tests that post then run use the next minute).
- Temporal describes a cron schedule as calendar specs: compare `info.next_action_times` with `Cadence.next_runs`, not cron strings.
- The API's Temporal client is connected lazily (`app.api.temporal_client`); `resources.temporal` may still be `None`. Tests that exercise API follow ups must point `TEMPORAL_TASK_QUEUE` at their own queue (and clear `get_settings`), or the dev stack's worker picks the workflows up.
- Console checkboxes show the requested state while saving: e2e tests wait for them to be enabled again before relying on the save.
- The Slack stand-in UI re-renders every second: click helpers (`openDm`, `openChannel`) retry until the view changed.
- The sandbox runner is a separate container sharing the `sandboxes` volume with the stand-ins; without it, commands answer 503 `runner_unavailable` (reads still work). A stand-in reset removes the sandboxes' folders but keeps the runner's heartbeat. Rebuild it with `docker compose up -d --build --wait sandbox-runner` after changing `fakes/fakes/sandbox_runner.py`.
- Inside a sandbox, the text of the running command is in a job file the sandbox can read: a test that greps for a secret builds the pattern inside the sandbox (`printf %s 'half' 'half'`), never puts the whole value in the command.
- The GitHub stand-in's branches, pull requests and pushes accumulate across tests and the dev stack (branch names carry the task id); read them with `GET /_admin/github/repos/{owner}/{repo}`. Its webhooks go to the dev stack's API (`GITHUB_WEBHOOK_URL`): backend tests take the payload from `GET /_admin/github/deliveries`, sign it and post it to their own API (with `TEMPORAL_TASK_QUEUE` pointed at their queue).
- The vault releases credentials only to running tasks: work done for a task that has ended (a GitHub notice) reads outside any task (`ctx.task_id = None`) with the org connection.
- A docstring-only revision adds no test, so its push asks a person even in Auto: e2e and backend tests approve it.
- The Compose console is Vite's dev server on a bind mount, where file events go missing for some writes and it keeps serving the old module: it polls (`VITE_WATCH_POLLING=1`). If a change does not show, compare `curl localhost:5173/src/<file>` with the file and restart `web`.
- The e2e global setup resets the Clerk stand-in, which ends every console session in other browsers (a Playwright MCP session too): sign in again.
