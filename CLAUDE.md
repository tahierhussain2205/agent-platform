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

**One image, many commands.** `backend/Dockerfile` builds one image; `python -m app <api|worker|maintenance|migrate|seed>` selects the process. On Render: `api` web service (migrations in `preDeployCommand`), `worker` background worker, `maintenance` cron job, `web` static site, Render Postgres (PgBouncer pool), Render Key Value.

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
| `make dev` / `down` / `reset` / `seed` | Local stack (phase 1) |
| `make migrate` | Apply migrations (phase 3) |

Backend only: `cd backend && uv run pytest -k name`, `uv run python -m app api`.
Web only: `pnpm --dir web dev`.

## Code conventions

- **Python 3.12**, `uv` for dependencies (commit `uv.lock`), `ruff` (lint and format, line length 110), `mypy --strict` on `app/`.
- **Layout** (grows per phase): `app/api/` HTTP layer only, `app/settings.py` the one typed settings object (environment only, never files in the image).
- **Naming**: modules and functions `snake_case`, classes `PascalCase`, SQL identifiers `snake_case`, tables plural.
- **Typing**: full annotations; no `Any` in public signatures unless wrapping untyped JSON.
- **Errors**: raise domain exceptions in services; the API layer maps them to HTTP responses. Never put secrets or tokens in exception messages.
- **Logging**: structured JSON logs (phase 4). Never log tokens, secrets or raw Slack message text.
- **Migrations**: add `backend/migrations/NNN_description.sql`; never edit an applied migration. New tables need explicit grants and, if tenant scoped, the `tenant_isolation` policy and composite `(org_id, id)` foreign keys. Migrations must be safe to run against the previous code version.
- **Adding a tool adapter**: arrives in phase 8.
- **Adding a playbook**: arrives in phase 7.
- **Web**: TypeScript strict, ESLint + Prettier, components in `PascalCase.tsx`, tests next to code as `*.test.tsx`.

## Testing rules

- Real Postgres for anything touching the database. Never mock row level security.
- Every new tenant table gets an isolation test (org A cannot read, insert, update, link or delete org B's rows; no context means no rows).
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
