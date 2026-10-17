# 9. Quality and operations

## The test strategy

| Layer | What it covers | Size |
|---|---|---|
| Schema suite | Tenant isolation for every tenant table (read, insert, update, link, delete across orgs; no context means no rows), every database guarantee, the migration runner, the database logins | Part of the backend suite |
| Mutation check | Weakens one database guarantee at a time (drops a policy, adds a grant, removes a trigger or constraint) and requires the schema suite to fail | 26 mutations, all caught |
| Backend | Units, the API with real Postgres, workflows on a real Temporal server, activities against the stand-ins, the agent end to end on a real worker, security properties asserted as denials | 1,225 tests |
| Stand-ins | The fakes themselves behave like the real APIs (errors, rate limits, signatures) | 79 tests |
| Console | Each page with a mocked API | 76 tests |
| Browser | Real flows on the full local stack: sign up and invitations, roles, integrations through consent pages, Slack through the stand-in's UI, tickets, investigations, pull requests and review comments, schedules, themes, cursors and phone widths | 49 Playwright tests |
| Evals | Agent quality on replayed cases (service, window, error signature, files touched by the fix, root cause, sourced claims) | 10 cases |

Rules the tests follow: real Postgres for anything touching the database, never a mocked row level security; every new tenant table joins the isolation suite (a meta test enforces it); security properties are tested by asserting the denial, not only the happy path; browser flows are tested end to end.

## Stand-ins

`fakes/` is one service, run only in Docker Compose and CI, that stands in for every external API we use: Clerk, Slack, Atlassian and Jira, GitHub, Google and Google Cloud, MCP servers, Anthropic and a sandbox provider. Each speaks the real HTTP shapes, including the real errors, signatures and rate limits, so the code under test only ever sees a base URL. A test admin API (`/_admin`) resets state, scripts responses and records requests.

The Anthropic stand-in is a deterministic, heuristic "model": it recognizes each agent step by its forced tool, reads our cited line format and answers the way a careful model would. Tests can script it to play a persuaded model, and read back exactly what reached it.

This is what let the platform be built and tested end to end before any real account existed. Real services then replaced the stand-ins on staging one at a time, and the demo runs on staging found the gaps the stand-ins had hidden (all logged in `docs/demo_test_log.md` and `spec/decisions_and_blockers.md`).

## Local development

| Command | Does |
|---|---|
| `make install` | Install backend (uv) and console (pnpm) dependencies |
| `make dev` | Build and start everything in Docker Compose and wait until healthy |
| `make infra` | Only Postgres, Valkey, Temporal, object storage and Jaeger |
| `make migrate`, `make seed` | Apply migrations, load the development fixture |
| `make lint`, `make typecheck`, `make test` | ruff and ESLint; mypy (strict) and tsc; all backend and console tests |
| `make e2e`, `make smoke` | Browser tests; stack smoke checks |
| `make test-mutations` | The schema mutation check |
| `make eval` | Replay the eval cases against the running stack |
| `make render-sim` | Run `render.yaml` locally the way Render would |

The local stack: the API on `localhost:8000`, the console on `localhost:5173` (seeded accounts such as `alice@acme.test`), the stand-ins on `localhost:4100` (with a Slack-like UI at `/slack/ui`), the Temporal UI on `localhost:8233`, Jaeger on `localhost:16686`. By default the worker uses the Anthropic stand-in; a gitignored Compose override points it at the real API.

## Deployment

Render deploys staging from `render.yaml` on every push to `main` (`docs/runbooks/deploy.md` has the runbook). The API's pre-deploy step runs `app migrate`, which applies migrations and creates or verifies each component's database login without rewriting unchanged passwords (a new password hash would break PgBouncer for services already connected).

CI (`.github/workflows/ci.yml`) has four jobs: backend (with Postgres, PgBouncer, Valkey, Temporal and the stand-ins), stand-ins, console, and a stack job that boots Compose and runs the smoke, image and browser checks. During the hackathon it runs on demand only, and staging deploys every commit; the same checks run locally before each push. Re-enabling it is two lines (see [decisions](../decisions/06-delivery-and-process.md#ci-paused-for-the-hackathon)).

## Observability

| Signal | How |
|---|---|
| Logs | One JSON object per line on stdout, with the request id, org id and trace ids added automatically; never tokens, secrets or Slack text |
| Traces | OpenTelemetry across the API, Temporal and activities; spans carry the tenant id but never SQL parameters or prompt content; model calls are GenAI spans without content |
| Usage | Every model call metered with tokens, cost and the step's purpose; the console's Usage page and each task's timeline read it |
| Audit | Every policy decision, approval and console change |
| Alerts | The maintenance run ends with an alerts job: task failure rate, queue depth and the oldest queued task, Slack acknowledgement latency (p95, p99), budget stops, schedules the system paused. Each alert goes to a webhook at most once an hour, figures only, no tenant content |

## Maintenance jobs

`app maintenance` runs every 15 minutes, as the maintenance database role:

| Job | Does |
|---|---|
| Partitions | Creates the next monthly partitions for usage events and the audit log |
| Slack reconciliation | Resyncs channel members with Slack for each active workspace |
| Task reconciliation | Closes tasks whose workflow is gone, so they do not block their thread |
| Schedule sync | Mirrors schedule rows into Temporal Schedules where the row is ahead |
| Sandbox cleanup | Destroys expired sandboxes |
| Retention | Deletes artifacts and old partitions past the org's retention |
| Offboarding | Deletes the data of orgs whose offboarding grace period has ended |
| Alerts | Checks the thresholds above and sends what needs attention |
