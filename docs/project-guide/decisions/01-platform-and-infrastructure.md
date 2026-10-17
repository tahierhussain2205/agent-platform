# 1. Platform and infrastructure

## Temporal for durable execution

**Decision.** Every request, report run and follow up is a Temporal workflow; the worker runs its activities. Temporal Cloud on staging, the dev server locally. (Spec T9, T10.)

**Why.** An agent task is long running and full of waits: model calls that take a minute, approvals that take hours, retries after provider errors, cancellations, and scheduled runs. Temporal gives retries with backoff, durable timers, signals for approvals, cancellation, schedules and replay after a crash, without us writing a queue, a state machine and a scheduler.

**Tradeoffs.**
- One more system to run and understand, and a cloud dependency on staging.
- Workflow code must be deterministic, and changing the order of commands in a workflow that runs long needs `workflow.patched(...)` so old histories still replay. We follow that discipline (for example when approvals started being requested together, and when tickets started following the change).
- Workflow history is storage: it must never hold message text (see [no raw Slack text](02-security-and-tenancy.md#no-raw-slack-text-stored)).

**Alternatives.** A task queue (Celery, RQ) with our own state machine and cron; rejected because approval waits, retries and replay would all be ours to build and test. A hosted agent platform; rejected because policy, tenancy and Slack logic must stay in our code.

## Postgres as the source of truth

**Decision.** PostgreSQL holds all state, with row level security for isolation and the database itself enforcing the important rules (triggers, constraints, composite keys). Key Value (Valkey) only holds short lived data: event claims, locks, caches, rate counters. Object storage holds large artifacts. Temporal holds execution state. (Spec section 13.)

**Why.** One transactional store keeps the rules in one place where application bugs cannot skip them. The schema was written and tested against a live database before any application code, so the guarantees came first.

**Tradeoffs.** Some logic lives in SQL (triggers, functions), which is less familiar to some developers and needs its own tests: we have a schema suite and a mutation check for it. Monthly partitions for usage and audit need a maintenance job.

**Alternatives.** Application level enforcement only (simpler code, but every query path is a chance to forget a filter); a document store (no row level security, weaker constraints).

## One image, many commands

**Decision.** One Docker image; `app api`, `app worker`, `app vault`, `app maintenance`, `app migrate` select the process. No `ENTRYPOINT`; every caller passes a full command. (D-025.)

**Why.** Every service runs the same code at the same commit, so the API that starts a workflow and the worker that runs it never disagree. One build, one set of dependencies, one vulnerability surface.

**Tradeoffs.** The image carries code some services never use (the vault's code is in the worker's image, though the worker lacks the vault's keys and database user). Service specific dependencies cannot diverge.

**Alternatives.** An image per service; more builds and the risk of version skew between services that share workflow definitions.

## Managed services behind our own interfaces

**Decision.** Use managed services where they are not the product: Clerk for identity (T2), Temporal Cloud (B-006), Cloudflare R2 for object storage (B-011), E2B for sandboxes (D-197), Render for hosting (D-189). Each sits behind an interface we own (an auth adapter, an `ObjectStore`, a `SandboxProvider`, the vault's OAuth providers).

**Why.** A small team should spend its time on the agent, the policy and the integrations, not on SSO, a Temporal cluster or microVMs. Our own interfaces keep each choice reversible: the sandbox provider was swapped from the local runner to E2B without changing anything above the interface.

**Tradeoffs.** Vendor dependencies and their limits (E2B's free plan keeps a sandbox at most an hour; Render renames services when a name is taken). Some vendor behavior only shows on the real service, which is why staging runs were part of the plan.

**Alternatives.** Self hosting each (more control and data residency, much more operations); deferred to when a customer needs it. The architecture keeps it possible.

## Stand-ins for every external service

**Decision.** One `fakes` service speaks the real HTTP APIs of Clerk, Slack, Jira, GitHub, Google, MCP servers, Anthropic and a sandbox provider, for the subset we use. Code only sees base URLs. (D-004, D-066, D-088.)

**Why.** It let the whole platform be built and tested end to end, locally and in CI, before any real account existed, and it makes tests deterministic: the stand-in model answers each agent step the same way every time, can be scripted to play a persuaded model, and records exactly what reached it.

**Tradeoffs.**
- Stand-ins can drift from the real APIs. When real services arrived, the staging runs found gaps the stand-ins had hidden, for example Jira's team managed projects having no "Bug" type (D-196) and real Slack listing the bot as a channel member (D-193). Each gap was fixed in the code and in the stand-in.
- The stand-ins are a real codebase to maintain (with their own tests).

**Alternatives.** Mocks per test (fast, but they test our assumptions, not the protocol); sandbox accounts for everything (not available when the work started, slow and flaky in CI).

## Render for staging

**Decision.** A Render Blueprint (`render.yaml`) defines staging: web service, background worker, private service, cron, static site, Postgres and Key Value. A test validates the Blueprint against Render's schema and our settings; `make render-sim` runs it locally the way Render would.

**Why.** Every component type we need, defined in one reviewed file, with pre-deploy migrations, private networking and generated per component database passwords.

**Tradeoffs.** Render specific behavior (renamed services, build filters) that had to be learned on the first deploys (D-189). Less control than a Kubernetes setup, which we do not need at this size.
