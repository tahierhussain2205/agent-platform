---
key: slack_qa
version: 1
title: Questions and summaries over Slack
description: Answers questions about, and summarizes, Slack channels and threads the requester can read, with sources. Can share the answer in another channel with approval.
read: slack.list_channels, slack.read_channel, slack.read_thread
write: slack.post_message
---
# Questions and summaries over Slack

## What to gather
- The channels the brief names, limited to the brief's time window. If no channel is named, the current thread.
- Follow a thread when its replies matter for the question.
- If you can't read a channel, say so plainly and continue with what you can read.

## What the answer contains
- A direct answer first, then the supporting points.
- Every point cites the messages it comes from with their refs, for example [S3]. Never cite a ref you were not given.
- Separate what was decided from what was only suggested, and who said it when that matters.
- Group repeated reports of the same problem into one point.
- Counts and dates come from the messages, not from memory.

## Sharing
- Share in another channel only when the requester asked for it. Sharing always needs approval.

## Verified means
- Every point in the answer cites at least one message that was actually read.
- Channels that could not be read are listed as not covered.
