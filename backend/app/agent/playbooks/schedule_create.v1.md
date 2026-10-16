---
key: schedule_create
version: 1
title: Schedule a report
description: Sets up a recurring report on a schedule (every day, every weekday, every Monday...): a summary of Slack channels or the progress of a Jira project, delivered to a channel or by direct message.
read: slack.list_channels
write:
---
# Schedule a report

You turn the request into a proposed schedule. The requester confirms it with a button; nothing is scheduled before that.

## What a schedule is
- A report: a summary of one to five Slack channels, or the progress of one Jira project.
- A cadence: a cron expression in the requester's time zone, at most once an hour. "Every weekday at 9" is `0 9 * * 1-5`; "every Monday at 10:30" is `30 10 * * 1`; "daily" without a time means 09:00.
- A delivery: a direct message to the requester, unless they name a channel to post it in.
- A short name, like "Daily #bugs summary" or "Weekday PAY progress".

## When it is unclear
- If the request does not say what to report on, or how often, ask one short question instead of guessing.
