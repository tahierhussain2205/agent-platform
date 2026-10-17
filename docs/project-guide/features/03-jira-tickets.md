# 3. Jira tickets from chat

Problems reported in Slack become Jira tickets, assigned to the right people, without duplicates.

## How to use it

In a thread with the problems, or naming a channel: `@agent create Jira tickets for the problems reported today in #bugs`.

## What happens

1. **Read**: the agent reads the thread or the channel's recent messages (only channels on the allowlist that you are in), grouping messages about the same problem.
2. **Propose**: the model proposes tickets: a summary, a description, a project, an assignee.
3. **Check, in code** (`backend/app/agent/tickets.py`), before anything reaches Jira:

   | Check | Result |
   |---|---|
   | An open issue already covers it | Dropped, with "looks like PAY-101" and a link |
   | It repeats another ticket in the same plan | Dropped |
   | An assignee is suggested | Kept only with evidence: an owner of the matched service in the catalog (CODEOWNERS, a component lead, set by an admin), or someone the request named |
   | Project and component | From the matched service; a component the project does not have is left out, and the approval says so |
   | Issue type | One the project has (team managed and company managed projects differ) |
   | Description | Links back to the Slack messages it came from |
   | A source is private | Always asks, warning that everyone who can browse the project will see what came from a private conversation |
   | More than three changes | Auto mode asks anyway |

4. **Decide**: in Ask mode each ticket gets an approval request, all posted together; in Auto they are created at once. Each ticket is created as soon as its own approval arrives.
5. **Create**: with **your** linked Jira account, so tickets are yours and stay within your access. Each creation carries a marker label; if a retry happens after Jira answered but before we recorded it, the marker is found and the ticket is not created twice. A ticket someone else filed while the approval was waiting is not filed again.
6. **Answer**: the created tickets as links, what was skipped and why, and a **Plan a fix** button.

## Tickets follow the work

When a plan is made from these tickets and then implemented, the tickets move with it: In Progress as the change starts (in Auto), In Review with a link to the pull request once it opens. See [code changes](05-code-changes-and-pull-requests.md#tickets-follow-the-change).

## Who the owners are: the service catalog

Owner suggestions come from the service catalog (console, Catalog page). A workflow proposes services from the repositories the GitHub App can see (their CODEOWNERS) and the Jira components the admin can browse (their leads), mapped to members through linked accounts and email. Admins confirm or edit services; only confirmed services suggest owners, and the agent always says why someone owns one ("owner of cart-service: CODEOWNERS /src/pricing/").
