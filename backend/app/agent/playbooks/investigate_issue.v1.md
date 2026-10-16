---
key: investigate_issue
version: 1
title: Investigate an issue and plan the fix
description: Investigates a bug or incident (a Jira issue, an error, "why is X failing") with the service's logs, its code and its recent changes, and proposes a sourced plan with ranked hypotheses; discusses and revises the plan in the thread. Does not change anything.
read: slack.read_thread, slack.read_channel, jira.get_issue, jira.search_issues, catalog.find_owners, observability.*, github.search_code, github.read_code, github.list_files, github.read_file, github.get_codeowners, github.list_commits, github.get_commit, github.list_deployments
write:
---
# Investigate an issue and plan the fix

## What the investigation covers
- The service the issue is about, from the catalog, and the incident's time window: when it started (the issue, the first errors), until now or its fix.
- Logs: the leading error signatures, how often and since when, a trace of one sample.
- Code: the files and lines the errors and the issue point to.
- Changes: the commits and deployments just before the problem started.

## What the plan contains
- Evidence: facts from the findings, each citing its sources.
- Ranked hypotheses, most likely first, each with a confidence, the evidence it rests on and how to confirm it. A hypothesis without evidence is a question, and the first step is to investigate it.
- Steps: reproduce first (a failing test), then the change, then verification; the repositories and files the work touches; risks; how to verify; how to roll back.
- Open questions for the people in the thread.

## Verified means
- Every evidence item and hypothesis cites findings that were actually read.
- What could not be read (logs aged out, access denied, no log source, no sandbox) is listed, and the confidence says how thin the evidence is.

## In the thread
- A follow-up question or new evidence revises the plan: say what changed and why, and keep what still holds.
