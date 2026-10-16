# Phase 11: Investigation and planning (Example 2, part 1)

**Goal:** the agent understands a ticket and produces a sourced plan (spec 3.3 steps 1 to 3, 7.2, 7.3, 7.4).

**Done when:** for a set of replayed historical bugs, the agent produces plans whose root cause matches the actual fix at an agreed rate, with every claim sourced. Blocked parts: there is no Google Cloud project (new B-012), no sandbox provider account (B-008) and no real model (B-005), so this runs against stand-ins; the agreed rate is measured on the stand-in model and must be measured again with a real model before the playbook is released (spec 15, release gate).

## Scope

1. **Observability interface** with a Google Cloud Logging adapter.
2. **Read only code access through a sandbox provider**, and the recent changes around an incident.
3. **Playbook `investigate_issue` v1**: parallel investigators (logs, code, changes) as isolated subagents, a plan template checked in code, the plan posted in the thread, brainstorming and replanning in the same thread.
4. **Replay benchmark**: eval cases from resolved bugs (thread, log window, merged fix) with retrieval and planning metrics.

## 1. Observability interface

One interface the model sees, whatever the backend (spec 7.2): three read operations with one normalized shape.

- `search_events(service, start, end, severity, text, limit)`: events grouped by **error signature** (exception type plus the message with numbers, ids, hex and quoted values replaced, plus the top frame), each group with count, first and last seen, severity and up to three samples (message, timestamp, trace id, labels). Totals and the query used are returned too.
- `list_error_groups(service, start, end)`: the backend's own error groups when it has them (Error Reporting), otherwise computed from events; counts, first and last seen, a representative message and stack.
- `get_trace(trace_id)`: spans (name, service, start, duration, status, labels) of one trace.

The service is resolved through the catalog: `services.log_source` (exists since migration 001) says which backend and how to select the service's logs (for Cloud Logging: project, resource type, and the label carrying the service name). A request without a known log source answers with what is missing, not with a guess. Windows are required and capped (7 days); `limit` caps samples. Filtering is pushed to the backend (the Logging query language: resource, labels, severity, timestamp range, text), never done by downloading everything.

**Adapter structure:** `app/observability/` holds the normalized types, the signature function and the `LogBackend` protocol; `GoogleCloudLogging` implements it (Logging API `entries:list`, Error Reporting `groupStats.list`, Cloud Trace `traces.get`). The tool adapter (`app/tools/logs.py`) is the integration `google_cloud_logging` with `family = "observability"`, so playbooks allow `observability.*` and a later backend (CloudWatch, Datadog) plugs in without changing playbooks. Its operations are reads in `org` mode (logs belong to the org, not to a person). Results are restricted sources: posting them to a channel needs every member to be an active member of the org (no Slack Connect guests); log text is redacted like every tool output.

**Credentials:** a Google service account key, uploaded once by an admin in the console and kept by the vault. The vault signs a JWT assertion with it and exchanges it for a one hour access token (`urn:ietf:params:oauth:grant-type:jwt-bearer`), cached per connection; the key never leaves the vault, the token never reaches the model. Connection health checks list one entry.

**Stand-in (B-012):** the Google stand-in gains the service account token exchange (RS256 assertions verified against the public key of a local dev key pair, `dev/gcp/`, like D-117), `entries:list` with a parser for the filter subset we generate, `groupStats.list` and `traces.get`, and fixture logs for an `acme-prod` project matching the Jira fixture: the cart-service 500s for carts with 20 or more items (`IndexError` in `src/pricing/cart.py`), webhook signature failures after a key rotation, slow EU search, plus noise.

## 2. Code and recent changes

**Sandbox interface** (`app/sandbox/`): a `SandboxProvider` protocol (create with limits and a time to live, upload an archive, list files, read a file range, search, destroy; `exec` is added in phase 12) and an HTTP implementation for a managed provider's API. **B-008 workaround:** the stand-in implements that API locally (one directory per sandbox, size and time limits, no command execution in this phase). Replacing it with a managed provider is a client change.

**How code gets in:** the platform downloads the repository archive at a pinned commit with a short lived, read only installation token and uploads it to the sandbox; the token never enters the sandbox (stronger than spec 8.2.2, which removes it after the clone). The base commit is the default branch head when the task first reads the repository, recorded per task; every read in the task sees that commit.

**Sandbox lifecycle:** a `sandboxes` table (migration 009, tenant scoped) with one row per task and repository: provider id, base commit, status, expiry. Investigators running in parallel share it (an `insert ... on conflict` claim decides who creates it; others wait until it is ready). Destroyed when the task finishes, and by the maintenance job when expired. Sandbox seconds are metered (`usage_events` kind `sandbox`).

**Operations** (GitHub adapter, `org_with_check`: the requester's linked login must be able to read the repository, as for `read_file`):
- `search_code(repo, pattern, path)`, `read_code(repo, path, start, end)`, `list_files(repo, path)`: through the sandbox; secret file patterns (`.env*`, keys, credentials files) are never read or searched; results are capped and redacted, cite `repo@sha:path#Lstart-Lend` with a link to that commit.
- `list_commits(repo, since, until, path)`, `get_commit(repo, sha)` (message, author, time, files with change counts), `list_deployments(repo, environment)`: GitHub REST, for the changes investigator.

The GitHub stand-in gains the archive download (`GET /repos/{o}/{r}/tarball/{ref}`), commits with files and a history around the incident (the change that capped carts at 19 items), and deployments.

## 3. Playbook `investigate_issue` v1

Classified from requests like "investigate PAY-101", "why is checkout failing", "look into this thread and plan a fix".

**Understand:** the brief from the thread, plus the Jira issue the request names (followed one hop) and the service it is about (catalog). The service, window and identifiers drive every later query.

**Gather, in parallel:** three investigator activities started together by the task workflow (behind a `workflow.patched` marker), each an isolated subagent with its own context, a narrow toolset and a fixed return schema (spec 7.4):
- **logs**: catalog and observability tools; returns error signatures with counts, windows and sample references.
- **code**: catalog, CODEOWNERS and the code operations; returns relevant files and line ranges with why.
- **changes**: commits and deployments of the service's repository around the window; returns suspect changes.

Each returns findings with sources and gaps (what could not be read). The orchestrator only sees these summaries, never raw tool output.

**Plan:** a forced `submit_investigation_plan` with the template of spec 7.3: goal, evidence (each item with sources), ranked hypotheses (each with confidence, supporting evidence and how to confirm it), steps, repositories and files touched, risks, verification, rollback, open questions. Checked in code before it is shown:
- every evidence item and hypothesis must cite sources that exist; the rest is dropped or listed as not verified;
- if the leading hypothesis has no evidence, the first step is to investigate it, not to implement;
- bug work starts with a failing test that reproduces it (reproduce first): one is added as the first implementation step when missing;
- confidence is capped by the evidence (no "high" with fewer than two independent sources).

The plan is stored as a `plan` artifact with its source refs, posted in the thread with sources (the output check applies: restricted sources go privately), and marked as the session's current plan.

**Brainstorming and replanning:** a follow-up in the thread is a new task in the same session; it gets the session's current plan as durable state (spec 7.2). The model either answers about the plan (discussion) or revises it; a revision states what changed and why. New evidence contradicting an assumption triggers a revision (spec 7.3). Execution is phase 12: this phase never writes.

## 4. Replay benchmark

Cases from resolved bugs (spec 15): the Slack thread, the log window, the Jira issue and the merged fix (repository, files touched, root cause). The harness gains checks for: the right service and window in the brief; the right error signature found; recall of the files the fix touched; root cause match (the leading hypothesis names the cause); every claim sourced; tokens. Three cases from the fixture data. The agreed rate for this phase: root cause match on at least two of three cases with every claim sourced, measured against the stand-ins, and the rate is recorded per case.

## Console

- Integrations: a Google Cloud Logging card (enable, connect with a service account key file, health).
- Catalog: a service's log source (project, resource type, service label) editable by admins.

## Tests

- Schema: `sandboxes` isolation through the generic suite, its guarantees (one per task and repository, status values), a mutation.
- Stand-ins: service account token exchange (signature, audience, expiry), `entries:list` filters, error groups, traces, archive download at a commit, commits and deployments, the sandbox API (limits, expiry, paths outside the workspace refused).
- Observability: signature normalization, grouping, filter building (pushed to the backend), log source resolution and missing source answers, audience, redaction.
- Sandbox: lifecycle (created once under parallel use, base commit pinned, destroyed at finish and on expiry), the token never in the sandbox, secret files refused, access checks.
- Playbook: investigators in parallel through the workflow, plan checks (unsourced claims dropped, no evidence means investigate first, a failing test first, confidence capped), the plan artifact, a follow-up revising the plan with what changed, failure behavior (logs aged out, access denied).
- Eval: the three replay cases and the new metrics.
- Console API and web tests; Playwright: an admin connects Cloud Logging and sets a log source; an engineer asks to investigate a ticket in Slack and gets a sourced plan, then refines it in the thread.

## Changes during implementation

- **Past incidents are read in the code of their time** (D-165): the PAY-105 replay showed that at HEAD the code investigator read the fix. When the window ended over an hour ago, the sandbox is built at the last commit before it.
- **Follow-ups keep the current plan's issue** when they name none (D-164).
- **The answer goes privately** when the plan cites sources someone in the channel cannot see (a private repository, the org's logs for a guest): the audience rule of D-116 and D-158.
- **The gather loop is shared** by the investigators (`AgentRunner._subagent`).
- **Provider reads retry once** after a transport failure (D-154, found in a full CI run).
