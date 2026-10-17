# 7. Integrations

Native adapters cover the tools that matter most (Slack, Jira, GitHub); MCP servers cover the long tail. Every adapter implements the same interface and every call goes through the same executor, so the policy, credentials, auditing, output caps and citations work the same way for all of them.

## The adapter interface

An adapter (`backend/app/tools/base.py`) declares its **operations** with metadata the rest of the platform relies on:

| Metadata | Used by |
|---|---|
| Kind (read or write), reversible, outward facing | The policy module derives the tier: read, reversible write, irreversible |
| Default credential mode (`org`, `org_with_check`, `user_required`) | The executor fetches the right credential; admins can override per org |
| Approver rule (requester, admin, either) | Who decides an approval |
| Internal | Operations only code may call (moving a ticket when its change ships, computing report figures); never offered to the model |
| Input schema | Shown to the model; validated before execution |

It implements `resolve_target` (which channel, project or repository a call touches, for the policy) and `execute(ctx, operation, args, credential)`. Provider requests go through one HTTP helper: reads are retried once after a transport failure, writes never.

**Adding a tool** means implementing the adapter, adding it to the integration catalog and the executor's registry, exposing its operations in the console, and adding the provider's endpoints to the stand-in. A long tail tool is an MCP server instead.

## Slack

| Part | What it does |
|---|---|
| Install | OAuth v2 with signed single use state; the bot token goes to the vault only after the install transaction commits |
| Client | Per workspace rate limiter following Slack's method tiers, retries on 429 and 5xx, pagination |
| Channel adapter | Renders the agent's intents as Block Kit: sections within size limits, long reports split across messages, buttons, one status message edited in place with updates coalesced |
| Channels | The console lists channels with the bot's membership and the allowlist; member lists are synced into Postgres, cached, kept current by Slack events and reconciled hourly |
| App Home | Each person's mode (Ask or Auto), their schedules with controls, or an access request button for people who are not members yet |
| Uninstall | `app_uninstalled` and `tokens_revoked` disconnect the workspace and revoke the token |
| Agent operations | List channels, read a channel in a time window with threads, read a thread, post a message (outward facing: always approved), channel statistics (internal, for reports) |

Readable output names people and channels instead of ids, and a channel link Slack sends without a name is resolved to the channel's name.

## Jira

Org connection by an admin (Atlassian OAuth), personal links by anyone. Writes always use the requester's own linked account.

| Operation | Mode | Notes |
|---|---|---|
| Search issues, get an issue, list projects, list components | `org_with_check` | Results are filtered to the projects the requester may browse |
| Create, assign, comment | `user_required` | Idempotent: each write carries a marker (a label or a comment marker) that is looked up before acting |
| Move to a status | Internal | Moves by status name; used when a change starts and when its pull request opens |
| Report figures | Internal | Opened, closed and in progress counts computed by code for scheduled reports |

Issue types come from the project's create metadata (team managed projects have different types than company managed ones), and components are matched against the project's own list.

## GitHub

A GitHub App, installed by an admin for the repositories the org chooses; people link their GitHub login.

| Operation | Notes |
|---|---|
| List repositories, read files, CODEOWNERS | The requester's linked login must have read access |
| Search code, read code, list files | Run inside the task's sandbox at a pinned commit; cite `repo@sha:path#L10-L20` |
| Commits, a commit's diff, deployments, a pull request | For the "recent changes" investigator and for revisions |
| Prepare a workspace | Internal: builds the sandbox, checks push access when a change will be made |
| Push changes | The trusted pusher, see [code changes](05-code-changes-and-sandboxes.md#the-trusted-pusher) |

GitHub webhooks bring reviews, failed checks and merges back to the Slack thread.

## MCP servers

A generic adapter speaks MCP over Streamable HTTP. Tools are discovered from the server; their tier comes from the tool's annotations (read only, destructive), their output is capped and redacted. Vendor servers (such as Google Workspace) are in the catalog; customer supplied servers can be added only with an admin's approval.

## Logs

The agent reads a service's logs only through the observability interface (`backend/app/observability/`) and the service's log source in the catalog. The first backend is Google Cloud (Logging, Error Reporting, Trace): events grouped by error signature with counts and samples. Model text never becomes query syntax; it is only ever inserted as a quoted literal. (Google Cloud is not connected on staging; the stand-in covers it locally.)

## The service catalog

The catalog maps services to repositories, Jira projects and components, log sources, owners and channels. A workflow proposes it from what the GitHub App can see (CODEOWNERS) and from the Jira components the proposing admin can browse (component leads), mapped to members by linked accounts and email. Admins confirm or edit it; proposals never overwrite what an admin confirmed. The `catalog_find_owners` tool ranks confirmed services for a problem and explains why someone owns one, which is how ticket assignees get their evidence.

## Stand-ins

Every integration has a stand-in in `fakes/` that speaks the provider's real HTTP API for the subset we use: Slack (Web API, OAuth, signed event delivery, a Slack-like UI), Atlassian (OAuth and Jira REST with a small JQL parser), GitHub (App JWTs, installation tokens, contents, the Git Data API, pull requests, reviews, checks, signed webhooks), Google (OAuth with PKCE, Cloud Logging), MCP servers, Clerk, Anthropic and a sandbox provider. Code under test only ever sees base URLs. See [quality and operations](09-quality-and-operations.md#stand-ins).
