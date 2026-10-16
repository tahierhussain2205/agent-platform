# Phase 9: Scheduled reports (Example 3)

**Goal:** recurring reports that are exact and permission safe (spec 3.4, 4.6, 6.4).

**Done when:** a QA user schedules a daily Jira report and receives exact numbers on time; removing the owner pauses it and notifies admins. (Staging is blocked by B-001 and real Jira by B-007: this runs against the local stack and the stand-ins.)

## Model

A schedule (`app.schedules`) has an owner, a name, a report kind (its playbook), parameters, a cron expression with a time zone, a delivery target (a channel or a DM to the owner) and a status (`active`, `paused` with a reason, `deleted`). The database is the source of truth; each schedule has a Temporal Schedule (`schedule-<id>`) that mirrors it.

Migration 007:
- `schedules.params jsonb` (report parameters, validated in code), `paused_at`, `pause_notified_at`, `synced_at` (when Temporal last matched the row).
- A trigger stamps `paused_at` (and clears `pause_notified_at`) when a schedule pauses, clears it when it resumes, and bumps `updated_at` on every change, so "unsynced" is `synced_at is null or synced_at < updated_at`.
- `app.maintenance_schedule_orgs()` (security definer, `app_maintenance` only): orgs with unsynced schedules or unnotified pauses.

The existing triggers keep doing their part: the quota on active schedules, pausing on owner removal (`owner_removed`) and when a delivery channel leaves the allowlist or is archived (`access_lost`).

## Report kinds

Two report playbooks (front matter `kind: report`, never offered to the classifier):
- `report_channel_summary` v1: params `channels` (1 to 5 channel ids). Figures by code from the full channel history in the window: top level messages, people (distinct authors), threads started, replies in those threads, per channel and in total. The narrative: the model reads the channels' messages (the same normalised, capped, redacted lines as the Slack tool) and writes highlights citing refs.
- `report_jira_progress` v1: params `project` (a key). Figures by code from Jira, with the owner's credential mode: issues created in the window, resolved in the window, open now by status and by priority. The narrative: the model gets the figures and the most relevant issues (key, summary, status, assignee, priority) and writes a short overview.

The window runs from the previous scheduled time to this one (computed from the cron), capped at 31 days; anchored on the run's nominal scheduled time, so jitter and retries never change the numbers.

**Numbers are data.** Figures are rendered into the message by code (a fixed block above the narrative). The narrative is verified in code: any number it contains must be one of the figures or appear in a source line it cites (issue keys and dates are ignored); otherwise the sentence is dropped, and if nothing is left the report goes out with figures only. Raw Slack text never leaves the activity that read it (invariant 7).

Two internal read operations, never offered to a model (`Operation.internal`): `slack.channel_stats` (paginates the whole window, counts without keeping text) and `jira.report_issues` (paginated JQL with the fields the figures need, filtered to what the owner may browse). Both go through the `ToolExecutor`, so the policy module, credential modes and audit apply as for any tool. The Jira stand-in's JQL learns `created`, `resolved`, `statusCategory` and `ORDER BY`, and paginates.

## Temporal

- One Temporal Schedule per schedule: the cron in the schedule's time zone, jitter (`SCHEDULE_JITTER_SECONDS`, default 300) so every tenant's 9 AM report does not start at once, overlap `SKIP`, a one hour catch-up window, paused when the row is not active. The action starts `ScheduledReportWorkflow(org_id, schedule_id)` on the agent task queue.
- `app.schedules.temporal.sync_org`: makes Temporal match every unsynced row of an org (create, update, pause, unpause, delete) and marks it synced only if the row did not change meanwhile. Called after console and Slack changes (best effort), by `ScheduleSyncWorkflow(org)`, and by the maintenance job for anything missed.
- `ScheduledReportWorkflow`:
  1. `begin`: reads the schedule; a deleted or missing one deletes its Temporal Schedule; a paused one pauses it; otherwise checks the owner is active, the delivery target is still allowed (allowlisted, not archived, the owner a member, the bot in it), and budgets. Creates the task (trigger `schedule`, the owner as requester, the playbook pinned, the batch lane) keyed by the run's workflow id, so a retried activity finds the same task.
  2. Waits for an org slot like interactive tasks.
  3. `compose`: figures, narrative, verification (one activity, so raw text stays inside it).
  4. `deliver`: the policy's output check against the delivery target (the audience rule included); if the target cannot see the sources, the report goes to the owner by DM with a note. Idempotent through `external_actions` (key `report:<schedule>:<scheduled time>`).
  5. Finishes the task and records `last_run_at`.
- Access failures (a source the owner can no longer read, the delivery target gone) pause the schedule with `access_lost`; a spent budget pauses it with `budget`. Either way `ScheduleSyncWorkflow` runs, which pauses the Temporal Schedule and notifies.

## Pause notifications

`ScheduleSyncWorkflow(org)` also sends, for every pause not yet notified (reasons `owner_removed`, `access_lost`, `budget`; not `admin`): a DM to every active admin with the schedule, owner, reason and a console link, and a DM to the owner when they are still active and opted in (`user_preferences.notify_on` has `schedule_paused`). Each DM is idempotent (`external_actions`), then `pause_notified_at` is set.

Started right after the paths that pause schedules commit: member removal (console and the Clerk webhook), the allowlist toggle, channel archive and unarchive events, and the report workflow itself. The maintenance job is the safety net. The maintenance cron moves from daily to every 15 minutes (all its jobs are idempotent).

## Creating and managing schedules

- **Slack, natural language:** a new interactive playbook `schedule_create` v1. The classifier picks it for requests like "every weekday at 9 send me the PAY Jira progress". The task then asks the model for a structured proposal (`submit_schedule`: name, kind, channels or project, cadence as cron, delivery). Code validates it: the cron parses, runs at most hourly, the time zone is the requester's (`user_preferences.timezone`), the requester can read every channel, Jira is enabled for a Jira report, the delivery target is allowed. It then posts a confirmation in the thread with the parsed schedule and its next three runs, plus **Create schedule** and **Cancel** buttons. The proposal is kept in Key Value for a day. Only the requester can confirm; confirming re-validates, inserts (the database enforces the quota) and syncs Temporal.
- **App Home:** "Your scheduled reports" with status, cadence and next run, and Pause, Resume, Run now and Delete buttons (the owner or an admin). New schedules are created by asking the agent or in the console.
- **Console:** a Schedules page. Members see and manage their own; admins see all, pause or resume any, delete, and reassign the owner of a paused schedule. It has a create and edit form (name, report kind, channels from those the member may read, Jira project, cadence presets or cron, time zone, delivery, a preview of the next runs) and Run now.
- **API** `/api/schedules`: list, create, update, pause, resume, delete, run now, reassign, plus `preview` (validate a cron and time zone, return the next runs and a description) and `options` (the channels the current member may read and deliver to, and whether Jira is enabled). Every mutation is audited. Resuming re-validates the owner and the target; the quota trigger refuses over-limit resumes (409).

## Limits

The org's `max_schedules` (active only, database trigger); at most one run an hour per schedule (code); 1 to 5 channels per channel report; reports use the batch rate lane and count toward the org's concurrency and budgets.

## Tests

- Schema: the pause stamp trigger, `updated_at` bumps, the maintenance function's grants, and isolation of the new columns through the existing suite; mutations for the new trigger and grant.
- Units: cron validation, description, next runs across DST and the minimum interval; windows; figure computation; narrative number verification; proposal validation.
- **Figures against direct queries:** Jira figures equal counts computed straight from the stand-in's API; channel figures equal counts from `conversations.history` and `conversations.replies`.
- Workflows with Temporal and the stand-ins: a run delivers to a channel and to a DM; the window is anchored on the scheduled time; retries do not duplicate; a paused or deleted schedule skips and fixes Temporal; owner removal, allowlist removal, lost source access and budgets pause and notify admins once; an over-limit resume is refused; a schedule created for the next minute fires and delivers on time.
- Slack: a natural language request proposes the right schedule, only the requester can confirm, cancel works, App Home controls work and check permissions.
- Console API: permissions (a member sees only their own; admins all), validation errors, reassign, quota, audit rows; tenant isolation.
- Web unit tests for the Schedules page; Playwright: create a Jira report in the console and receive it with exact numbers (Run now and a scheduled run), schedule from Slack in natural language and confirm, App Home controls, removing the owner pauses it and DMs admins, an allowlist change pauses and resume works.
