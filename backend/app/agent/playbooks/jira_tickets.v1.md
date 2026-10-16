---
key: jira_tickets
version: 1
title: Jira tickets from chat
description: Creates Jira tickets for problems reported in Slack (grouping repeated reports, skipping problems that already have an open ticket) and assigns them to the owners in the service catalog; also assigns or comments on existing tickets when asked. Changes are made with the requester's own Jira account.
read: slack.list_channels, slack.read_channel, slack.read_thread, jira.search_issues, jira.get_issue, jira.list_projects, jira.list_components, catalog.find_owners, github.get_codeowners
write: jira.create_issue, jira.assign_issue, jira.add_comment
---
# Jira tickets from chat

## What to gather
- The channels or thread the brief names, limited to its time window. Follow a thread when its replies add details (steps, errors, who is affected).
- Group reports of the same problem into one finding, citing every message it comes from.
- For each problem, search Jira for open issues about it (a narrow query with the project and a few words).
- For each problem, ask the service catalog who owns it (`catalog_find_owners`) with the problem's text and any repository, path, project or component mentioned. CODEOWNERS can add detail when a repository is named.
- If you can't read something, say so plainly and continue with what you can read.

## What the plan contains
- One `create_issue` per distinct problem, at most five. Skip a problem that already has an open issue; say which issue in the answer instead.
- For each ticket: `project` and `component` from the service the catalog matched (or what the requester named), a short factual `summary`, a `description` with what was reported, by whom and any error text, and `finding`: the number of the finding it comes from.
- `assignee_email`: an owner the catalog returned for this problem, or one the requester named. Leave it empty when the catalog has no owner; never guess.
- `assign_issue` or `add_comment` only when the requester asked for them on an existing issue.

## What the answer contains
- The problems found, each citing its messages, and which ones already had a ticket.
- Never claim a ticket exists before it is created; the links are added after the changes are made.
