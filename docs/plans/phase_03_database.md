# Phase 3: Database and migrations

**Goal:** the tested schema running on Render Postgres, with isolation guarantees enforced in CI.

**Done when:** migrations apply cleanly on local, CI and Render staging; the 28 tests pass in CI; the cron job creates partitions on staging.

## Starting point

Phase 1 already delivered the runner (D-012): ordered SQL files, one transaction per file with its bookkeeping row, checksum protection, advisory lock. This phase adds Render compatibility, login users per component, the schema test suite and the maintenance job.

## Render Postgres compatibility

What is known (Render docs): users created with `CREATE USER` are "not managed by Render", which implies the default user can create roles. Nothing says it is a superuser, and in PostgreSQL 16 only a superuser (or a role that has `BYPASSRLS`) can grant `BYPASSRLS`. Working assumption, until checked on a real instance (B-001): **the default user has `CREATEROLE` and owns the database, but is not a superuser and has no `BYPASSRLS`.**

Design that works either way:

1. **`001` tolerates a non superuser migrator.** The four `alter role ... [no]bypassrls` statements run inside a block that reports and skips `insufficient_privilege`, followed by a hard check that `app_user` and `app_vault` do **not** have `BYPASSRLS` (fail closed). A non superuser migrator grants itself `SET` membership in `app_owner` (it has `ADMIN` on roles it created, PG16 rule) so it can create the schema owned by `app_owner`. Nothing else in `001` changes; the drift test pins the exact difference from `spec/schema.sql`.
2. **The application never relies on `BYPASSRLS`.** Identity sync and maintenance work go through `SECURITY DEFINER` functions owned by `app_owner`, executable only by their role (the spec's fallback, used everywhere so there is one code path). Phase 3 needs only `app.ensure_monthly_partitions` (already a security definer function); identity sync functions arrive with the Clerk webhook in phase 5, reconciliation in phase 6, retention and offboarding in phase 13.
3. **Local mirrors the assumption.** Compose's Postgres gets a non superuser `agent_admin` (`CREATEROLE`, owns database `agent`) standing in for Render's default user; `make migrate` and the `migrate` service run as it. Tests run migrations both as a superuser and as a Render-like user.

## Login users per component

| Component | Login user | Member of | Used by |
|---|---|---|---|
| API (request paths) | `api_service` | `app_user` | console API, Slack ingress |
| Worker | `worker_service` | `app_user` | agent workflows and activities |
| Vault | `vault_service` | `app_vault` | token vault (phase 8) |
| Identity sync | `identity_service` | `app_identity_sync` | Clerk webhook handler (phase 5) |
| Maintenance | `maintenance_service` | `app_maintenance` | `app maintenance` cron |

- `app migrate` creates or updates every login user whose password is configured (`DB_API_PASSWORD`, `DB_WORKER_PASSWORD`, `DB_VAULT_PASSWORD`, `DB_IDENTITY_PASSWORD`, `DB_MAINTENANCE_PASSWORD`).
- Passwords are sent as **SCRAM-SHA-256 verifiers computed client side**, so the plaintext never reaches the server (or its statement log).
- Components connect with `DATABASE_URL` (Render's PgBouncer `connectionPoolString`) with the user and password swapped for their own; `Settings.component_dsn(component)` builds it.
- On Render the passwords come from one generated env group per component (`generateValue: true`). The API service carries all of them because its `preDeployCommand` creates the users (recorded as a known trade-off).

## Maintenance job (`app maintenance`)

- Connects as `maintenance_service`; runs a list of jobs, each isolated (one failure does not skip the rest; exit code 1 if any failed).
- Phase 3 job: `select app.ensure_monthly_partitions(3)`. Later phases append reconciliation, retention and offboarding.

## Schema test suite (`backend/tests/schema/`)

A `world` fixture builds two orgs with **at least one row in every tenant table** (owner role, inside a throwaway database), so isolation tests can be generic.

| Area | Tests |
|---|---|
| Generic isolation (parametrized over every tenant table) | org A sees only its rows; no context sees nothing; updates and deletes aimed at org B touch nothing; inserting a row for org B is rejected by RLS; moving a row to org B is rejected; writes without context fail |
| Cross org links | composite foreign keys reject a child in org A pointing at a parent in org B (several representative tables) even when the parent id is guessed |
| Grants | `app_user` cannot read `connection_secrets`; partitions not reachable directly; `audit_log` and `usage_events` append only; `app_user` cannot write orgs/users/memberships except renaming its org and editing settings columns |
| Vault | `app_vault` reads secrets only for the current org |
| Users | the users table shows only people in the current org |
| Playbooks | built ins shared, org playbooks private, versions immutable (only `enabled` updatable) |
| Last admin | demote or remove the last admin fails; two concurrent demotions of the only two admins: exactly one succeeds |
| Roles | roles without a primary fail at commit; switching primary in one transaction works; deleting the primary promotes the oldest; two primaries rejected |
| Functions | `effective_mode` (user choice capped by repo over channel over org, default ask); `can_read_channel` (allowlist, membership, archived, removed member); `resolve_workspace` and `resolve_task_links` work without context and ignore inactive rows |
| Tasks | one running task per session; terminal states need a finish time; schedule trigger rule |
| Approvals | requester / admin / either rules; removed member denied; expired denied; decided once |
| Schedules | time zone and cron validation; delivery target matches type; per org quota; pause needs a reason |
| Triggers | allowlist removal or archiving pauses schedules delivering there; member removal revokes links, pauses schedules, closes sessions |
| Artifacts | retention defaults from org settings; exactly one body |
| Usage | rollups per member and for system usage (null member) |
| Uniqueness | idempotency keys unique per org only; one active org connection per tool; custom MCP needs approval |
| Deletion | deleting an org removes its data across tables and leaves the other org intact |
| Helpers | `uuid_v7` has version 7 and is time ordered |

Plus: migrations as a Render-like user, login users (membership, no `BYPASSRLS`, each limited to its grants), SCRAM verifier format, maintenance job (creates partitions, idempotent, isolates failures).

## Render Blueprint changes

- `DATABASE_URL` from `connectionPoolString` for `api-staging`, `worker-staging`, `maintenance-staging`.
- Env groups `db-<component>-staging` with generated passwords; `api-staging` references all five (it runs `app migrate`), the worker references worker and vault, the cron references maintenance.
- `make render-sim` runs migrations as a Render-like user, then the maintenance cron as `maintenance_service`, and checks partitions exist.

## Risks

- **Render specifics remain unverified** (B-001): the default user's attributes, and whether Render's PgBouncer accepts users created with SQL. If it does not, components would connect to the direct URL (no pooler) or use Render-managed credentials created through the API.
- **The API process environment holds the migration URL and all component passwords**, because Render runs `preDeployCommand` with the service's environment. The code only opens them inside `app migrate`; a dedicated migration job would remove this exposure later.
