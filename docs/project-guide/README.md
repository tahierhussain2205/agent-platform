# Project guide

A guided tour of the Slack Agent Platform for reviewers: what it does, how it is built, and why it is built that way.

## What this is

A multi tenant AI agent that works as a teammate inside Slack. People mention it in a channel or message it directly, and it reads and summarizes conversations, files Jira tickets, investigates bugs in code, logs and recent changes, writes a plan, changes the code in a sandbox and opens a pull request. Scheduled reports come out of the same machinery.

Each customer is an **org**. Admins set it up in a web console: which Slack channels the agent may read, which tools it may use, which models and limits apply, and roles that shape how it answers different people. The agent always acts **within the requesting person's own access**, never with the bot's full reach. Safety rules live in code and in the database, never in a prompt.

The whole flow can be tried on the staging deployment. The console is at https://web-staging-o82n.onrender.com and the Slack workspace is "Agent Platform" (both need an invitation).

![System architecture](technical/images/system-architecture.png)

## How to read this guide

| Folder | Read it for | Start with |
|---|---|---|
| [technical/](technical/README.md) | How the application is built: components, the life of a request, the agent runtime, security and tenancy, sandboxes, the data model, integrations, the console, testing and operations | [Architecture](technical/01-architecture.md) |
| [features/](features/README.md) | What it does, feature by feature, as a person in Slack or an admin in the console sees it | [The demo, step by step](features/01-demo-walkthrough.md) |
| [decisions/](decisions/README.md) | The key technical decisions, the tradeoffs behind them and the alternatives we turned down | [Overview](decisions/README.md) |

If you have twenty minutes: read the [demo walkthrough](features/01-demo-walkthrough.md), then [architecture](technical/01-architecture.md), then [safety in layers](decisions/02-security-and-tenancy.md).

## Diagrams

Every diagram is drawn in Excalidraw in a dark theme. Next to each PNG is its `.excalidraw` source, which opens in [excalidraw.com](https://excalidraw.com) for editing.

| Diagram | Shows |
|---|---|
| [System architecture](technical/images/system-architecture.png) | Every component and external service, and how they connect |
| [Deployment](technical/images/deployment.png) | The staging services on Render and the managed services around them |
| [Lifecycle of a request](technical/images/slack-request-lifecycle.png) | A Slack mention, from the signed event to the answer |
| [Task workflow](technical/images/task-workflow.png) | The steps of one task and its three main paths |
| [Model call](technical/images/model-call.png) | How a step builds a prompt, goes through the gateway and checks what comes back |
| [Tenant isolation](technical/images/tenant-isolation.png) | One org per transaction, row level security and the database roles |
| [Policy decision](technical/images/policy-decision.png) | How the policy module decides every tool call |
| [Credentials](technical/images/credentials-vault.png) | Connecting accounts once and releasing credentials per task |
| [Code changes](technical/images/code-change-sandbox.png) | The sandbox boundary: only the diff leaves |
| [Data model](technical/images/data-model.png) | Tables by domain, and what is kept out of Postgres |
| [The demo flow](features/images/demo-flow.png) | From a bug report in Slack to a pull request, across Slack, Jira, GitHub and the console |
| [Autonomy and approvals](features/images/autonomy-approvals.png) | Ask or Auto, action tiers and the approval lifecycle |
| [Scheduled reports](features/images/scheduled-reports.png) | Creating a schedule and what happens at every run |
| [Roles](features/images/roles.png) | What a role contains and where each part takes effect |
| [Safety in layers](decisions/images/safety-layers.png) | The layers a request passes before anything happens |

## Where things are in the repository

| Path | Contents |
|---|---|
| `backend/` | The Python application: API, worker, vault, maintenance jobs, migrations, tests |
| `web/` | The admin console (React and TypeScript) |
| `fakes/` | Local stand-ins for every external service (Slack, Jira, GitHub, Clerk, Anthropic, Google, a sandbox provider), used in development and tests |
| `e2e/` | Browser tests (Playwright) |
| `spec/` | The requirements and design specification, the phased plan, the initial schema and the decisions and blockers log |
| `docs/plans/` | The detailed plan written before each phase |
| `docs/progress.md` | What each phase delivered |
| `docs/demo_test_log.md` | End to end runs of the demo on staging, with every issue found and how it was fixed |
| `render.yaml` | The Render Blueprint that defines staging |
| `CLAUDE.md` | The working rules: architecture, invariants, conventions and gotchas |

## Glossary

| Term | Meaning |
|---|---|
| Org | A customer. Every row of tenant data belongs to exactly one org |
| Member | A person in an org, an admin or a user. People can belong to several orgs |
| Requester | The person who asked for a task. The agent acts with their access |
| Task | One request handled by the agent, run as one durable workflow |
| Session | A Slack thread or DM conversation. Tasks in the same thread share it |
| Playbook | Versioned instructions for one kind of work (answer questions, file tickets, investigate, implement, report), plus the tools it may read and write with |
| Ask / Auto | Whether the agent asks before reversible changes (Ask) or makes them within the person's access (Auto). Irreversible actions always ask |
| Approval | A request posted in Slack with Approve and Reject buttons. The database decides who may answer it |
| Policy module | The code that approves or blocks every tool call and every output |
| Vault | The separate service that holds all secrets and releases short lived credentials per task |
| Stand-in | A local fake of an external service that speaks its real HTTP API, used so everything runs and is tested without real accounts |
| Shadow mode | An org setting in which the agent reads, plans and answers but makes no changes |
