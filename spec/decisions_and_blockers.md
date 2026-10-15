# Decisions and Blockers

Running log kept while implementing `implementation_plan.md`. Every decision made without asking is recorded here with its reason, and every blocker with the workaround in place and what is needed to resolve it properly.

- **Decisions** are numbered `D-NNN` and grouped by phase.
- **Blockers** are numbered `B-NNN`. Each lists: what is blocked, the workaround in use, and what is needed to resolve it.

---

## Blockers (summary)

| # | Blocker | Phases affected | Workaround in place | Needed to resolve |
|---|---|---|---|---|
| B-001 | No Render account or API key | 2, 3, 4, 5, 6, 7, 13 (all "on staging" checks) | `render.yaml` written and checked locally; a local "Render simulation" runs the same image the same way Render would (see D-020) | A Render workspace (Pro or higher for autoscaling), a connected GitHub repo, and the secrets listed in `.env.example` entered as `sync: false` values |
| B-002 | No GitHub remote, so GitHub Actions cannot run | 0 onward | The CI workflow is written; `make ci` runs the same steps locally against real Postgres | A GitHub repository; push, and check the first Actions run |
| B-003 | No Clerk instance | 5 onward | Local Clerk stand-in in the `fakes` service (see D-004) | A Clerk application (publishable and secret keys, webhook signing secret, allowed origins) |
| B-004 | No Slack app or test workspace | 6 onward | Local Slack stand-in with a small Slack-like UI in the `fakes` service | A Slack app created from `slack/manifest.yaml`, a test workspace, client ID/secret and signing secret; Marketplace submission |
| B-005 | No model provider API key | 7 onward | Local Anthropic Messages API stand-in (deterministic, heuristic) in the `fakes` service | An Anthropic API key (or Bedrock/Vertex AI route), then run the eval suite against the real model |
| B-006 | No Temporal Cloud namespace | 1 onward | Temporal dev server in Compose (the plan's local equivalent) | A Temporal Cloud namespace, mTLS cert or API key |
| B-007 | No Jira, GitHub or Google accounts | 8 onward | Local stand-ins for the subset of each API we use | OAuth apps / a GitHub App for each provider, plus test accounts |
| B-008 | No managed sandbox provider account | 11, 12 | To be decided in phase 11 | A provider account (open question 18.2.3) |

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
