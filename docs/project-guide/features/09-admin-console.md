# 9. The admin console

The console is where admins set the agent up and where everyone can see what it did. It has a light and a dark theme and works on a phone. Members who are not admins see a read only version, without Usage, Settings and the Audit log.

| Page | What it is for |
|---|---|
| **Overview** | The org at a glance: members, admins, pending invitations, roles, allowlisted channels, active schedules, the Slack workspace |
| **Tasks** | Every request the agent handled (admins see all, members their own): status, playbook, channel, mode, duration, tokens. Each task opens a timeline |
| **Members** | Invite by email, promote, demote, remove (an org always keeps an admin), assign roles with a primary one, see who linked Slack; decide access requests from Slack |
| **Roles** | Create, edit and archive roles: the profile, the limits, approvals routed to the role, and a live preview |
| **Slack** | Install or disconnect the workspace, sync channels, choose the channels the agent may read (the allowlist), see whether the bot is in each one |
| **Integrations** | Enable Jira, GitHub, Google Workspace, Cloud Logging and MCP servers; connect the org's accounts; link your own; check connection health; turn operations on or off and override their credential mode; add and approve custom MCP servers |
| **Catalog** | Services with their repository, Jira project and component, log source, owners and channels; propose the catalog from GitHub and Jira, then confirm it |
| **Repositories** | Protected paths and the test command per repository, and how the tests last ran |
| **Schedules** | Every scheduled report: create, edit, pause, resume, run now, preview, reassign, delete |
| **Usage** | Tokens and cost this month against the org's limit, per day, per person (with today's use against the daily limit), per playbook and per model |
| **Settings** | Shadow mode; allowed model tiers and the default; limits (tokens per person per day, per org per month, concurrent tasks, active schedules, retention days); the autonomy ceiling |
| **Audit log** | Every console change, identity sync, policy decision and approval, filterable by action, decision and actor |

## The Tasks timeline

Each task's page shows:

- **The header**: the task's title (its brief's goal, never the raw message), status, playbook, Ask or Auto, links to the Slack thread, the tickets and the pull request.
- **The steps**: understand and read, plan, act, verify, report, each with its outcome.
- **The timeline**, in time order: steps starting and finishing; policy decisions (routine reads folded into one line); approvals, who may decide and who did; writes ("Created a Jira issue KAN-15", "Pushed a branch and opened a pull request"); model calls grouped with their purposes and tokens; what was saved (the brief, the plan, the diff, the summary).

A running task's page updates itself until the task finishes; a task waiting for approval says what it is waiting for and who decides.

It is built entirely from what tasks already record, with no extra logging: task steps, the audit log, approvals, external actions, usage events and artifacts.
