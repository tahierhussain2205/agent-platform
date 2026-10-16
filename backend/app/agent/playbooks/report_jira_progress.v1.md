---
key: report_jira_progress
version: 1
title: Jira progress report
description: A scheduled progress report for one Jira project: created, resolved and open issues counted by code, with an overview written by the model.
kind: report
read: jira.report_issues
write:
---
# Jira progress report

The figures (issues created and resolved in the period, open issues by status and priority) are computed by code and shown above your text. You write only the narrative.

## What the narrative contains
- A summary of at most two sentences: how the project moved in this period.
- Up to six highlights: notable issues created or resolved, high priority work still open, unassigned high priority issues, anything that looks stuck. Give the key, status and assignee.
- Every highlight cites the refs it comes from, for example [S3]. Never cite a ref you were not given.

## Numbers
- Never compute, estimate or restate counts; the figures already say them. Issue keys are fine.
