# Phase 10: Jira actions and service catalog (Example 1)

**Goal:** turn conversations into correctly assigned tickets (spec 3.2, 4.4, 4.5).

**Done when:** Example 1 works end to end in both modes, writes are never duplicated on retry, and the tickets appear under the requesting user's name. (Staging and real Jira are blocked by B-001 and B-007: this runs against the local stack and the Jira stand-in.)

## Service catalog

Migration 008 extends the catalog tables of migration 001:
- `services`: status `proposed` besides `active` and `archived`; `jira_project`, `jira_component`, `paths` (code paths within the repository), `evidence` (what a proposal is based on, for admins), `confirmed_by`, `confirmed_at`. A service name is unique per org among services that are not archived.
- `service_owners`: `source` (`manual`, `codeowners`, `component_lead`) and `reason` (shown wherever an owner is suggested).

**Proposals** (`CatalogProposalWorkflow`, started by an admin from the console) are computed in code, within the admin's access, through the `ToolExecutor` (so the policy module and audit apply as for any tool):
- one service per repository the GitHub App sees, owners from CODEOWNERS (the catch-all rule and the most common owners of other rules), mapped to members through their linked GitHub logins;
- Jira components of the projects the admin can browse (a new read operation, `jira.list_components`), matched to a repository service by name or proposed on their own, with the component lead as an owner (mapped through linked Jira accounts);
- owners that map to no member are kept in the evidence, so an admin can link or assign them.

Proposals never overwrite what an admin confirmed: an existing confirmed service only gains missing links (repository, component). Admins confirm, edit (name, description, owners, repository, Jira project and component, paths, channels) or archive services in the console; members see the catalog read only.

## Tools

- **Jira writes** (`user_required`: the requester's linked Jira account acts, so tickets carry their name): `create_issue` (project, type, summary, description with links back to the Slack messages, assignee, labels, component), `assign_issue`, `add_comment`; all reversible writes (Ask needs approval, Auto proceeds; approver: the requester or an admin). Idempotent beyond `external_actions`: each created issue carries a label derived from the action's idempotency key, and a retry looks it up first, so a crash between Jira's answer and our bookkeeping never creates a second issue. Comments carry a marker checked the same way.
- **Catalog** (`catalog` adapter, read, our own database, always on): `find_owners` returns ranked candidates for a text, repository, path or component, each with its reasons (owner of a confirmed service, CODEOWNERS rule, component lead) and the member's email.
- The Jira stand-in gains issue creation (reporter from the token), assignment, comments, project components with leads, and JQL on labels and components.

## Playbook `jira_tickets` v1

Read Slack, Jira, the catalog (and GitHub CODEOWNERS); write the three Jira operations. Gather reads the channel in the window, searches Jira for existing tickets and asks the catalog for owners. Plan proposes at most five tickets, each citing the finding it comes from.

Code then checks every proposed ticket before it reaches the policy:
- **Duplicates**: a ticket whose summary matches an open issue the gather step found (or that Jira's search finds at execution time) is dropped and reported as a duplicate of that issue.
- **Assignees**: an assignee must be one of the catalog's candidates for the ticket's text or component; the action carries the candidates' reasons ("owner of cart-service, CODEOWNERS `src/pricing/` in acme/cart-service"). Without evidence the ticket stays unassigned and says so. Approval requests show the assignee and the reasons, so people can trust or correct them.
- **Links**: the description gets the permalinks of the Slack messages the finding cites.

## Ask and Auto

The policy already applies tiers, the user's choice and the admin ceiling (org, channel, repository). New: Auto drops to Ask when a plan has more than three actions (spec 4.5.3, plan size threshold), with a note in the thread. Created issues are posted in the thread as links.

## Console

A Catalog page: services (proposed first) with owners and their reasons, repository, Jira project and component, channels and evidence; Propose services (admin), Confirm, Edit, Archive. API `/api/catalog`.

## Tests

- Schema: the new columns and checks, isolation through the existing suite, a mutation for the name uniqueness.
- Jira stand-in: create, assign, comment, components, label and component JQL, reporter from the token, permissions.
- Adapter: writes with the requester's link, refused without one, idempotency (a retry after a lost answer finds the issue by its label; a comment is not repeated), assignee resolution by email.
- Catalog: proposals from CODEOWNERS and components, owner mapping, unmapped owners in evidence, confirmed services untouched; `find_owners` ranking and reasons.
- Runner and workflows: duplicates dropped, assignees validated with reasons, links in descriptions, Ask (approval requests, approve and reject), Auto (created without approval, within the ceiling), Auto dropping to Ask above three actions, links posted in the thread, no duplicates when an activity is retried.
- Console API: permissions, edits, owners and channels, confirm and archive, isolation; web unit tests; Playwright: an admin proposes and confirms the catalog, then Example 1 in Ask mode (approve in Slack) and in Auto mode, tickets created under the requester's Jira account and assigned to the catalog owner.

## Changes during implementation

- **Approval requests appear together** (D-145): the task workflow decides every action first and performs each when its own decision arrives, behind `workflow.patched("approvals-together")` so running tasks replay as before. With up to five tickets, one request after another hid the plan and was slow.
- **Model and user text is escaped in Slack mrkdwn** (D-147): found while adding ticket notes; covers answers, summaries, previews, questions, report narratives, schedule names and App Home.
- **Duplicates are also checked at execution** (D-143), for an issue someone filed while the approval waited.
- **Answers citing restricted sources go privately, ticket links included** (D-146): Example 1 is asked where the problems were reported.
- **Eval**: a ticket case scored after the checks in code, skipped when Jira is not connected (D-150).
- **Stand-ins** can list, delete and reset Jira issues; tests clean up what they create (D-148).
