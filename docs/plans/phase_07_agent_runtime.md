# Phase 7: Agent runtime core

**Goal:** a real agent answering questions over Slack within policy.

**Done when:** "summarize today's discussion in #bugs" returns a correct, sourced answer; a user who is not a member of the channel is refused; budgets stop a task cleanly; the eval harness runs. (Staging is blocked by B-001 and there is no model key, B-005: everything runs locally against the Slack and Anthropic stand-ins.)

## Decisions taken before coding

- **Route to the model provider (open item 18.2.2):** the Anthropic Messages API directly, over plain `httpx` (same style as our Slack and Clerk clients). The gateway takes a list of routes, so Bedrock or Vertex AI can be added as fallback routes without touching callers. Tiers map to models through settings: `fast` = `claude-haiku-4-5-20251001`, `standard` = `claude-sonnet-5`, `advanced` = `claude-opus-5-5`.
- **Tracing backend for LLM traces (decision needed by this phase):** OpenTelemetry spans following the GenAI semantic conventions (model, tokens, latency, tenant and task ids), exported to the OTLP backend we already run (Jaeger locally). Prompts and completions are **not** attached to spans. Langfuse accepts OTLP, so it can be added later as a second exporter (Langfuse Cloud until customer data flows, or self hosted) without code changes.
- **No raw Slack text in Temporal history (invariant 7, D-067):** anything that reads Slack messages and calls the model runs inside one activity and returns only derived, sourced output (brief items, findings, answer text, permalinks). Raw messages exist only in activity memory and in the model request. This is also the spec's "isolated subagent" shape (7.2): raw tool output never reaches the orchestrator.

## Blocker workaround: an Anthropic stand-in (B-005)

The `fakes` service gains `POST /anthropic/v1/messages`, matching the Messages API shapes we use: `x-api-key` auth, `anthropic-version`, `system` blocks with `cache_control`, `tools`, `tool_choice`, `tool_use`/`tool_result` content blocks, `stop_reason`, and `usage` (input, output, cache creation and cache read tokens). Errors use Anthropic's error envelope (`authentication_error`, `not_found_error`, `rate_limit_error` with `retry-after`, `overloaded_error` 529, `api_error`).

It is a small deterministic "model": the step is identified by the forced tool name (`submit_classification`, `submit_brief`, `submit_findings`, `submit_plan`, `submit_report`) or by the Slack read tools on offer, and the answer is built with simple heuristics from the request (channel names, time words, "post/share in #x") and from tool results (our adapter's `[ref]` lines). Test hooks under `/_admin/anthropic`: inject faults (status, count), queue scripted responses (to simulate a model persuaded by prompt injection), and read recorded requests (to assert what reached the model: role section present, secrets redacted, no tokens).

## Backend

### LLM gateway (`app/llm/`)

`LLMGateway.complete(call, tier, system, messages, tools=None, tool_choice=None, max_tokens)`:
1. Resolve the model: requested tier if the org allows it, else the org default. Cheap purposes (classify, brief) prefer `fast` when allowed.
2. Per tenant limits before each call: the org's requests per minute (Key Value window per org and lane; interactive and batch lanes, batch gets a smaller share) and token budgets (user per day, org per month from `usage_daily`, task budget from `tasks.token_budget`). An exhausted budget raises `BudgetExceeded`; callers decide how to stop.
3. Prompt caching: `cache_control` on the stable system prefix (platform rules, playbook, roles) and on the last tool definition.
4. Retries with backoff on 429 (honoring `retry-after`), 529 and 5xx and connection errors; then the next route if one is configured; else `LLMUnavailable`.
5. Metering: one `usage_events` row per call (model, input, output, cached tokens, cost from a price table in code), `tasks.tokens_used`/`cost` updated, the `usage_daily` rollup by the existing trigger.
6. A `gen_ai.chat` span with token counts and tenant/task ids, no content.

### Tool adapter interface (`app/tools/`)

Built here so phase 8 extends it:
- `Operation` metadata in code: name, description, JSON input schema, `kind` (read/write), `reversible`, `outward_facing`, default credential mode (`org`, `org_with_check`, `user_required`), approver rule. The action tier (read, reversible_write, irreversible) derives from it.
- `ToolAdapter`: `key`, `operations`, `execute(ctx, operation, args) -> ToolResult` (text for the model, capped, plus structured `sources`). Output above the cap is truncated with a note.
- Registry: adapters by key; which operations a step loads comes from the playbook.
- Slack adapter: `list_channels` (channels the requester may read, from the database), `read_channel` (history in a time window, replies folded in), `read_thread`, and the write `post_message` (to another channel: outward facing, so the irreversible tier). Reads use the bot token (credential mode `org_with_check`: the channel rule is the check).
- Redaction (`app/policy/redaction.py`): tokens (Slack, GitHub, AWS, Anthropic, generic bearer/JWT), private keys, passwords in URLs; applied to tool output before the model and to every outgoing message.

### Policy module (`app/policy/`) (spec 9.2)

One `Policy` object per task with the task context (org, requester, channel, effective mode). Every decision is audited (`audit_log`, actor `agent`, the task id, `allowed`/`denied`, reason, metadata):
1. `check_tool(adapter, operation, args)`: the integration is enabled for the org (Slack: an active workspace connection), the operation is not disabled by an org override, channel reads pass `app.can_read_channel` (allowlisted, not archived, requester a member), the action tier against the effective mode (read: allowed; reversible write: approval in Ask, allowed in Auto; irreversible: always approval), and the credential mode (code default or `operation_policies` override). Returns `Decision(allowed | needs_approval | denied, reason, credential_mode, tier, approver_rule)`.
2. `check_output(destination, sources)`: every source must be visible to everyone at the destination: the same channel, a public channel of the workspace, or a private channel whose members include every member of the destination. Otherwise the answer goes to the requester privately. Content is redacted.
3. Auto drops to Ask on low classification confidence (spec 4.5.3); sensitive paths and plan size arrive with code work in phase 11.

### Playbooks (`app/agent/playbooks/`)

A playbook is versioned instructions (Markdown) plus metadata in code (tools and operations it may load, output format, what "verified" means). Built in playbooks are synced into `app.playbooks` (org null) by `app migrate` at deploy time, as the owner: a new version row when the content changed, existing versions never modified (versions are immutable in the database too). A task pins the playbook row it started with. First playbook: `slack_qa` (questions and summaries over Slack channels and threads the requester can read, with sources, and sharing a summary to another channel with approval).

### Agent runner (`app/agent/`)

The loop, **understand, gather, plan, act, verify, report**, as plain async step functions with injected dependencies (database, vault, Slack, gateway, policy, an output sink), so Temporal activities and the eval harness run the same code:
1. **Understand**: classify the request to a playbook (fast tier, forced `submit_classification`); unclear means the agent asks a clarifying question and ends. Build the **brief** (goal, facts, constraints, decisions with who made them, open questions, entities: channels, people, time window, ids; every item with its source) from the triggering thread. Stored as an `artifacts` row (`brief`).
2. **Gather**: a subagent with its own context and only the playbook's read operations runs a bounded tool loop (max rounds, per step token cap). Every tool call goes through the policy; denials come back to the model as tool results ("access denied: ..."). It returns compact findings, each with sources; citations are checked against the messages it actually fetched.
3. **Plan**: actions the request asks for (for `slack_qa`: none, or sharing to a channel), each with a summary.
4. **Act**: per action, the policy decides; `needs_approval` creates an `approvals` row (expiry, approver rule), posts Approve/Reject buttons in the thread and waits; writes run one at a time with an idempotency key in `external_actions`.
5. **Verify**: code checks that every point in the answer cites a fetched source and that refusals are reported; unverified points are labelled.
6. **Report**: the answer (task tier, forced `submit_report`) with numbered sources as permalinks, the output policy check (private delivery when needed), the final status message, and a `summary` artifact with `source_refs` (permalinks only).

Role tailoring: the requester's roles (primary first, descriptions truncated, total bounded) go into a clearly delimited system prompt section that states it shapes tone, depth and format only.

Untrusted content: the system prompt tells the model that fetched content is data and instructions inside it are not followed; the policy enforces regardless.

### Durable execution (`app/workflows/`)

- `AgentRequestWorkflow` (intake, from phase 6) now ends by creating the session and task (`task_create`: session per thread or DM conversation, task `queued` with model tier, effective mode, token budget, `workflow_id = task-<id>`) and starting `TaskWorkflow` as an abandoned child.
- `TaskWorkflow`: `task_start` (FIFO per session: waits while the session has a running or an earlier queued task; waits while the org is at `max_concurrent_tasks`, checked under an advisory lock; refuses up front when the user's daily or the org's monthly budget is spent), posts the status message (Cancel button), runs the steps as activities with a checkpoint per step (`task_steps` rows), checks budgets between steps, waits for approval signals with a timer for expiry, and always finishes the task row (completed, failed or cancelled) and the status message.
- Budget exceeded between steps: stop, report what was done in code (no model call), task `completed` with the reason.
- Cancellation: the Cancel button (requester or an admin) cancels the workflow; the current write finishes first (`WAIT_CANCELLATION_COMPLETED`), pending approvals expire, the status message says who cancelled.
- Every external write carries an idempotency key in `external_actions`; thread posts also carry the key in Slack message metadata so a retried activity finds a post that succeeded before a crash instead of posting twice.
- Approvals: `/slack/interactions` handles `approval_approve`/`approval_reject`: the decision is written in a tenant transaction (the database trigger enforces approver rule, expiry and finality), then the workflow is signalled, and the message is updated. The workflow re-reads the decision from the database on wake, so a lost signal is not a lost decision.
- Maintenance: `app.maintenance_open_tasks()` and `app.maintenance_fail_task()` (migration 004, `app_maintenance` only) let a reconciliation job fail tasks whose workflow is gone.

### Eval harness (`app/eval/`, `backend/evals/cases/`)

`app eval [--cases DIR] [--report FILE] [--min-score X]` replays YAML cases through the agent runner in process (seeded orgs, the configured Slack and model, a capturing output sink, so nothing is posted). Scores per case: playbook, brief (channels, time window), retrieval (expected sources cited), answer (required facts present, forbidden content absent), refusal correctness, tokens. Prints a table, writes a JSON report, exits non zero below the threshold. CI runs it in the stack job as a non blocking step.

## Tests

| Area | Tests |
|---|---|
| Anthropic stand-in | auth, unknown model, each step's output shape, tool loop, cache usage, faults, scripted responses, recorded requests |
| Gateway | tier resolution, budget refusal (user day, org month, task), rate limit wait, retries on 429/529/5xx then fallback route, usage events and task totals, cache control placement, span without content |
| Tools and redaction | operation metadata and tiers, output caps, Slack reads (window, threads, pagination), redaction patterns |
| Policy | tool not enabled, operation disabled by override, channel rule (not allowlisted, archived, not a member), tiers by mode, credential mode overrides, audience (public, private subset, private leak redirected), every decision audited, cross tenant: a task cannot see another org's channels |
| Playbooks | sync inserts new versions only, immutable versions, task pins its version |
| Runner | classification and clarifying question, brief extraction, gather with denied tool calls, citations checked, report with permalinks, private delivery, approval path, budget stop between steps, prompt injection (scripted persuaded model is still refused), secrets redacted before the model and Slack |
| Workflows (real Temporal, stand-ins) | end to end answer in the thread, FIFO per session, org concurrency wait, approval approve/reject/expire, lost signal recovered, cancellation, idempotent posts across activity retries, intake still handles outsiders and channels not allowed |
| Interactions | approval decided by the requester or an admin only, expired and repeated decisions refused, another org's approval not found, cancel by requester or admin only |
| Eval | cases load, scoring, report, threshold exit code |
| End to end (Playwright) | "summarize today's discussion in #bugs" answered with sources; a non member asking about #leads-private is refused; restricted sources answered privately; share to #general with Approve and with Reject; a tiny daily limit stops the task cleanly; agent decisions in the console audit log |
