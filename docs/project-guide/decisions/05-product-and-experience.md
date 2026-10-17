# 5. Product and experience

## Slack first, the console for admins

**Decision.** People work with the agent in Slack, including their own settings (Ask or Auto, schedules) in the App Home. The web console is for admins (setup, people, roles, limits, audit) and for anyone who wants to see what the agent did. The agent emits platform neutral intents that a channel adapter renders, so another chat platform would be an adapter, not a rewrite. (Spec T1, T14.)

**Why.** Work starts in chat; leaving Slack to use the agent would defeat the purpose. Admin work is occasional and benefits from a full web UI.

**Tradeoffs.** Two surfaces to keep consistent (the App Home and the console show the same mode, computed by the same database function). Only Slack is implemented.

## One status message per task

**Decision.** Each task posts a single status message in the thread and edits it as it progresses, with a Cancel button while it runs; the answer, approvals and notes are separate messages. Approval requests for one plan are posted together. (D-100.)

**Why.** A thread that fills with progress messages buries the conversation. One edited message shows what is happening without noise. Requests posted together let a person decide a plan at once instead of waiting for each action in turn.

**Tradeoffs.** Edits are rate limited by Slack, so updates are coalesced; Slack's own clients occasionally redraw the thread while it changes.

## Ask and Auto, with ceilings

**Decision.** Each person chooses Ask or Auto; admins set ceilings (per org in the console, with per channel and per repository supported by the schema, the most specific winning); roles can keep their holders in Ask; Auto still asks on low confidence, large plans, private sources and code changes that need a person. The default is Ask. (Spec P6, 4.5, D-199.)

**Why.** Power users get speed, admins keep control, and the riskiest cases ask regardless of settings.

**Tradeoffs.** The effective mode is the result of several inputs, which can confuse; the App Home spells out "your admins allow up to Auto; you are working in Ask mode".

## Shadow mode

**Decision.** An org setting in which the agent reads, plans and answers but every write is denied by the policy module, with the answer saying what it would have done. Self service orgs start in it. (Spec 15.)

**Why.** Trust is earned: an org can watch the agent's plans for a while before letting it act.

**Tradeoffs.** A new org must turn it off before the agent does anything useful beyond answers, which is one more step in onboarding.

## Roles that only narrow

**Decision.** A role's profile is a set of fixed choices (detail, language, format, what to call out) turned into our own sentences in the prompt, plus the admin's free text. Its limits only narrow: playbooks it includes, operations it blocks or always asks before, a cap at Ask. A role can approve operations for everyone. Every limit is enforced in code or the database; a preview shows a profile's effect before saving. (D-202, D-203, D-204.)

**Why.** The specification kept v1 roles to tone only (P5) and left room for policies; the demo needed both: answers that fit the reader, and limits teams actually use ("Support cannot push code", "pushes are approved by a Tech Lead"). Narrowing only keeps the core promise that a role never grants access.

**Tradeoffs.** With several roles the most restrictive wins, which is safe but can surprise; refusals name the role so people know why. Fixed profile choices are less expressive than free text, on purpose.

**Alternatives.** Free text roles only (unpredictable, invisible in effect); role based grants (would break "within the requester's own access").

## Buttons that carry intents

**Decision.** Answers offer the next step as a button (Plan a fix, Implement this plan, View pull request). The button carries an intent key, never text; a click starts the request in the same thread as the person who clicked, once, with all the usual checks. (D-205.)

**Why.** The demo's next step used to be a sentence people had to phrase right. A button makes it one click without letting a click do more than the same request typed by the same person.

**Tradeoffs.** Only a few next steps are offered, chosen in code. The request a button makes is written by us, so the brief reads it as the clicker's message.

## A Tasks page from existing records

**Decision.** The console's Tasks page and each task's timeline are built from what tasks already record (steps, audit, approvals, external actions, usage events, artifacts), with one addition: the purpose of each model call. (D-201.)

**Why.** Reviewers and admins need to see what the agent did and why without reading logs. Everything was already in the database for audit; showing it needed no new logging, so the timeline cannot drift from the truth.

**Tradeoffs.** The timeline is only as detailed as the audit trail; message text is never shown (a task's title is its brief's goal).
