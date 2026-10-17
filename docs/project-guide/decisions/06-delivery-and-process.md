# 6. Delivery and process

## A phased plan, with written plans and logs

**Decision.** The work followed a phased plan (`spec/implementation_plan.md`): foundation, local environment, deployment, database, backend core, identity and console, Slack, the agent runtime, integrations, scheduled reports, Jira actions, investigation, implementation, production readiness. Before each phase a detailed plan was written to `docs/plans/`; after it, what it delivered went into `docs/progress.md`, and every lasting decision or blocker into `spec/decisions_and_blockers.md`. A phase closed only when every "done when" item was true or blocked with a workaround logged.

**Why.** Each phase builds on proven foundations: the schema and its guarantees before the API, the API before the agent, reading before writing, writing tickets before writing code. Written plans and logs make the reasoning reviewable, which is the point of this guide.

**Tradeoffs.** Up front planning takes time, and some early decisions were revised as real services arrived (each revision is logged, for example D-185 superseding D-003).

## Testing depth over breadth

**Decision.** Real Postgres for every database test; an isolation suite covering every tenant table, enforced by a meta test; a mutation check proving the schema tests catch weakened guarantees; workflows tested on a real Temporal server; the agent tested end to end against stand-ins; browser tests for every flow; evals for quality. Security properties are tested by asserting the denial.

**Why.** The most damaging failures here (one org seeing another's data, the agent acting beyond someone's access, a secret leaking) are exactly the ones mocks hide. Testing the tests (mutations) shows the suite would notice a regression.

**Tradeoffs.** A slow suite (the full backend run takes minutes) and real infrastructure in CI. We accepted it; fast unit tests still exist for pure logic.

## Blockers as workarounds, not waits

**Decision.** When an account or a fact was missing (no Slack app, no Jira site, no sandbox provider, unknown Render Postgres behavior), the work continued against a stand-in or a safe assumption, logged as a blocker with its workaround, and the real service replaced the stand-in when it arrived.

**Why.** It kept the build moving and made the platform independent of any one vendor from the start.

**Tradeoffs.** Each real service brought a round of fixes for what the stand-in had hidden; those are logged and covered by tests that now encode the real behavior.

## Demo runs on staging

**Decision.** The demo flow was run end to end on staging with real Slack, Jira, GitHub, E2B and Claude, repeatedly, logging every gap, bug and rough edge in `docs/demo_test_log.md` and fixing them until a run was clean (six runs, 31 issues, all fixed or explained).

**Why.** The real model and real services behave differently from stand-ins in ways that matter: a model that ends an answer with tool markup, a team managed Jira project, Slack sending channel links without names. Only real runs find those.

**Tradeoffs.** Real runs create real tickets and pull requests that must be cleaned up between runs (moved to Done and closed, never merged).

## CI paused for the hackathon

**Decision.** During the hackathon, CI runs only on demand and Render deploys staging on every commit to `main`, which mirrors the working branch; the same checks (lint, types, the affected tests) run locally before each push. (D-200.)

**Why.** Speed: waiting for the full CI run on every change slowed the demo work, and staging must always have the latest change.

**Tradeoffs.** Nothing stops a broken commit from reaching staging except the local checks. This is a deliberate, temporary trade: re-enabling CI is two lines (the `push` and `pull_request` triggers in `.github/workflows/ci.yml`, and `autoDeployTrigger: checksPass` in `render.yaml`), and it comes back once the main development is done.
