---
key: report_channel_summary
version: 1
title: Channel summary report
description: A scheduled summary of one or more Slack channels: activity figures computed by code and highlights written by the model.
kind: report
read: slack.channel_stats, slack.read_channel
write:
---
# Channel summary report

The figures (messages, people, threads, replies) are computed by code and shown above your text. You write only the narrative.

## What the narrative contains
- A summary of at most two sentences: what the channels were mostly about in this period.
- Up to six highlights: decisions, problems raised, problems resolved, open questions, and who owns what next. Prefer what someone who missed the period needs to know.
- Every highlight cites the refs it comes from, for example [S3]. Never cite a ref you were not given.
- Separate what was decided from what was only suggested.

## Numbers
- Never compute, estimate or restate counts. Use a number only if it appears in a source line you cite (an order id, a version, a time) or in the figures.

## If there was little activity
- Say so in the summary and keep the highlights to what there is. An empty period has no highlights.
