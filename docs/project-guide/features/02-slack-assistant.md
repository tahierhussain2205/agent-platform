# 2. The assistant in Slack

## Asking

Mention the agent in a channel (`@agent summarize today's discussion in #bugs`) or message it directly. Each Slack thread, or DM conversation, is a session: follow ups in the thread build on what came before ("and in #general?", "plan a fix for these tickets", "address the review comments").

What it can do depends on the playbooks and the tools the org has enabled:

| Ask for | It does |
|---|---|
| A summary, or a question about conversations | Reads the channels you can read, in the time window you mean, and answers with every point sourced |
| A question about Jira or GitHub | Searches within your own access and cites the issues, files or commits |
| Tickets for problems discussed | See [Jira tickets](03-jira-tickets.md) |
| An investigation, a plan to fix something | See [investigations](04-investigations-and-plans.md) |
| A change, a pull request, review comments addressed | See [code changes](05-code-changes-and-pull-requests.md) |
| A recurring report | See [scheduled reports](06-scheduled-reports.md) |
| Sharing an answer in another channel | Posts it, after your approval (posting elsewhere is outward facing, so it always asks) |

An unclear request gets one clarifying question instead of a guess.

## Answers you can check

Every point in an answer cites its sources, rendered as links: the Slack message (a permalink), the Jira issue, the file and lines at a pinned commit. Code checks the citations before the answer is posted: refs must exist, and the answer says how many of its points cite messages that were actually read. What the agent could not read (a channel you are not in, a project you cannot browse) is said in plain words, with the reason, rather than left out silently.

## Private when it has to be

The agent acts with your access, so it may read something not everyone in the channel can see. Before posting, it checks the answer's sources against the people in the channel. If anyone there could not see one of them, the answer goes to you by DM and the thread gets a short note saying why.

## While it works

A single status message in the thread shows what the agent is doing ("Reading #bugs…", "Waiting for 2 approvals", "Running the tests…") and is edited in place, so the thread does not fill with progress messages. While the task runs it has a **Cancel** button; cancelling never interrupts a write in progress. When it finishes, the status shows the outcome and the tokens used.

## Buttons on answers

When there is an obvious next step, the answer offers it as a button:

| After | Button | Starts |
|---|---|---|
| Tickets were created | **Plan a fix** | An investigation of those tickets |
| A plan | **Implement this plan** | The code change |
| A pull request opened | **View pull request** | Opens it in GitHub |

A click runs as the person who clicked, in the same thread, with all the usual checks. The button is replaced by "@name chose Plan a fix", and a second click (by anyone) is told it already started. Buttons only appear for playbooks the requester's roles include.

## Approvals

Changes that need approval (see [autonomy](07-autonomy-and-approvals.md)) appear in the thread as a message with **Approve** and **Reject**, a preview of exactly what will happen, who may decide and when the request expires. Someone who may not decide is told so privately.

## The App Home

The agent's Home tab in Slack shows:

- your org, your email and your roles,
- **How should I act?**: Ask or Auto, with the ceiling your admins allow and the mode you are actually in,
- your scheduled reports, with Run now, Pause or Resume, and Delete,
- a link to the console for linked accounts.

Someone who is not a member yet sees a **Request access** button instead.

## People who are not members

If someone who is not an invited member mentions the agent, they get a private reply explaining that they need access, and the org's admins get a DM with **Invite** and **Dismiss**. Invited people can use the agent in Slack as soon as their Slack email matches the invitation, even before they sign in to the console.
