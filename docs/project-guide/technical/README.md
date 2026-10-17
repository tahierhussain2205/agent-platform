# Technical documentation

How the application is built. Each chapter stands on its own; read them in order for the full picture.

| Chapter | Covers |
|---|---|
| [1. Architecture](01-architecture.md) | Components, the technology stack, one image with many commands, the staging deployment |
| [2. The life of a request](02-request-lifecycle.md) | Slack ingress, the intake workflow, the task workflow and its steps, approvals, delivery, durability |
| [3. The agent runtime](03-agent-runtime.md) | Playbooks, prompts, model calls and the LLM gateway, subagents and tools, structured output, investigations, buttons |
| [4. Security and multi tenancy](04-security-and-tenancy.md) | The ten invariants, tenant isolation, database roles, the policy module, credentials and the vault, prompt injection, audit |
| [5. Code changes and sandboxes](05-code-changes-and-sandboxes.md) | Sandbox providers, working copies, the implementation steps, diff checks, the trusted pusher, GitHub feedback |
| [6. Data model](06-data-model.md) | Tables by domain, guarantees the database enforces, migrations, what is kept out of Postgres |
| [7. Integrations](07-integrations.md) | Slack, Jira, GitHub, MCP servers, logs, the service catalog, and how a new tool is added |
| [8. The admin console](08-console.md) | The web app's structure, authentication, data fetching, theming and its API |
| [9. Quality and operations](09-quality-and-operations.md) | The test strategy, stand-ins, evals, local development, deployment, observability, maintenance jobs |

## The short version

![System architecture](images/system-architecture.png)

- **People** use Slack (mentions, DMs, buttons, the App Home) and admins use a React console.
- **The API** (FastAPI) verifies Slack's signed events, claims each event once and starts a durable **Temporal** workflow, then answers Slack within three seconds. It also serves the console's REST API and the OAuth callbacks.
- **The worker** runs every workflow's activities: the agent's steps, scheduled reports, follow ups. Every model call goes through the **LLM gateway** (tiers, budgets, rate limits, metering). Every tool call and every output goes through the **policy module**.
- **The vault** is a separate private service, the only holder of secrets. It exchanges OAuth codes, refreshes tokens and releases short lived credentials to a running task.
- **Postgres** holds the state, with row level security so every query sees one org. Key Value holds event claims, locks and caches; object storage holds large artifacts such as diffs.
- **Code changes** happen in an **E2B sandbox** with no internet and no credentials; only the diff comes back, and a separate trusted step pushes it to GitHub.
