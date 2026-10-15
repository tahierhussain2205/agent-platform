# Phase 6: Slack app and ingress

**Goal:** the agent can be installed in Slack and reliably receives every event.

**Done when:** installing into a test workspace works from the console; a mention in an allowlisted channel produces a reply in the thread; duplicates, bad signatures and unknown workspaces are handled; an uninvited user gets the access request flow. (No Slack app or workspace exists, B-004: everything runs against a Slack stand-in.)

## Blocker workaround: a Slack stand-in

The `fakes` service gains Slack at `http://localhost:4100/slack`:
- **Web API** subset (`/slack/api/<method>`, bot token auth, Slack's `{"ok": ...}` envelope and error codes): `oauth.v2.access`, `auth.test`, `auth.revoke`, `conversations.list|info|members|history|replies`, `chat.postMessage|update|postEphemeral|getPermalink`, `users.info|lookupByEmail`, `views.publish`. Optional per method rate limits answer `429` with `Retry-After`, to exercise our limiter.
- **OAuth v2**: an authorize page (pick a workspace, Allow) that redirects back with `code` and `state`.
- **Events API and interactivity**: signed (`X-Slack-Signature`, `X-Slack-Request-Timestamp`) deliveries with Slack style retries (`X-Slack-Retry-Num`).
- **A small Slack-like UI** (`/slack/ui`): pick a workspace and a person, read and post in channels and threads, see the bot's replies and ephemeral messages, click buttons, view the App Home. Playwright drives it.
- Seeded from the shared fixture (same workspaces, people, channels and members as the database) plus sample channel history for later phases.

## Backend

### Slack app manifest (`slack/manifest.yaml`) and Marketplace prep

Minimal bot scopes, each justified in `docs/slack/marketplace.md`: `app_mentions:read`, `channels:read`, `groups:read`, `channels:history`, `groups:history`, `im:history`, `chat:write`, `users:read`, `users:read.email`. Events over HTTP (`app_mention`, `message.im`, `member_joined_channel`, `member_left_channel`, channel and private channel lifecycle events, `app_home_opened`, `app_uninstalled`, `tokens_revoked`), interactivity on, App Home tab on, no Socket Mode (not allowed in the Marketplace).

### Install (OAuth v2) and uninstall

- `GET /api/slack/install` (admin) returns the authorize URL with a signed, single use `state` (org, membership, nonce in Key Value, 10 minute expiry).
- `GET /slack/oauth/callback` verifies `state`, exchanges the code (`oauth.v2.access`), and in one go: upserts the workspace (one per org; a workspace already connected to another org is refused), enables the `slack` integration, records the org connection, stores the bot token **encrypted** through the vault (`connection_secrets`, `app_vault` only), audits, starts a channel sync, and redirects to the console.
- `app_uninstalled` / `tokens_revoked` events and the console's Disconnect mark the workspace uninstalled, revoke the connection and delete the secret.

### Ingress (spec 14.2)

`POST /slack/events` and `POST /slack/interactions`:
1. Signature over `v0:{timestamp}:{raw body}` with the signing secret; timestamp within 5 minutes.
2. `url_verification` answers the challenge.
3. Claim the event id in Key Value (short TTL); extend after a successful enqueue; release and return 500 if enqueueing fails, so Slack's retry can succeed. A claimed id is acknowledged without work.
4. Resolve the workspace (`app.resolve_workspace`); unknown or uninstalled workspaces get 200 so Slack stops retrying.
5. Route: `app_mention` and user `message.im` start `AgentRequestWorkflow` with a workflow id derived from the event id (Temporal rejects duplicates: the second dedupe layer). **Only references** (channel, ts, thread, user) go into the workflow, never message text: raw Slack messages are not stored (invariant 7), and Temporal history is storage. Channel and membership events update `channels` / `channel_members` (and the Key Value membership cache) directly. `app_home_opened` starts a home publish workflow. Uninstall events revoke as above.
6. Acknowledge with 200, well under 3 seconds.

Interactions (button clicks, App Home selects) are verified the same way and dispatched: App Home mode changes, access request invite and dismiss. Approval buttons arrive in phase 7.

### Worker: workflows and activities

- `AgentRequestWorkflow` (stub until phase 7): identify the requester (platform identity, or match the Slack email to an invited or active org user and record the identity); if unknown, open an access request, tell the person privately, and DM the admins an Invite button; if the channel is not allowlisted, tell the requester privately; otherwise reply "received" in the thread through the channel adapter.
- `SyncWorkspaceWorkflow`: channels (`conversations.list`) and members of allowlisted channels where the bot is a member (`conversations.members`), into Postgres and the Key Value cache.
- `PublishHomeWorkflow`: App Home with org, roles and the Ask/Auto selector (capped by the admin ceiling).
- Activities run in their own tenant transactions (`set_config` first), as `worker_service`; the bot token comes from the vault as `vault_service`.

### Channel adapter and outbound queue

Agent intents (`Reply`, `Progress`, `ApprovalRequest`, `Report`, `Ephemeral`, `DirectMessage`) render to Block Kit (text split to fit Slack limits, long reports as multiple sections). Every send goes through a per workspace rate limiter in Key Value (token bucket per Slack method tier) and retries `429` honoring `Retry-After`; progress updates to the same message are coalesced (only the latest is sent). Sends carry idempotency keys (`external_actions` from phase 7; here the workflow activity id).

### Reconciliation

The maintenance cron lists active workspaces through a security definer function (`app_maintenance` only) and starts a `SyncWorkspaceWorkflow` per workspace (Temporal deduplicates by an hourly workflow id).

### Console

- Slack page: connection status, Connect (OAuth) and Disconnect, Sync now, channels with private/archived flags, bot membership (with the `/invite @agent` hint when missing), member counts and the allowlist toggle.
- Access requests: Invite (one click: a Clerk invitation through the same identity flow) and Dismiss.

### Data and seed

- Migration 003: `app.maintenance_active_workspaces()` (security definer, `app_maintenance` only).
- The seed also stores the seeded workspaces' bot tokens (encrypted with the local key) so the seeded orgs work without running OAuth; the Slack stand-in knows the same tokens.

## Tests

| Area | Tests |
|---|---|
| Signatures | valid; wrong secret; stale and future timestamps; missing headers; body tampering; constant time compare |
| Ingress | URL verification; duplicate event ids (claim) and duplicate workflow ids; enqueue failure releases the claim and returns 500; unknown and uninstalled workspaces get 200; bot's own messages ignored; acknowledgement time under 1 s; no message text in workflow input |
| OAuth | state forgery, reuse and expiry; code exchange failure; workspace already connected elsewhere; token stored encrypted and readable only by the vault; reinstall replaces the connection |
| Uninstall | `app_uninstalled` and `tokens_revoked` revoke the connection and delete the secret; routing stops |
| Channel and member events | create, rename, archive (pauses schedules via the trigger), member join/leave update Postgres and the cache; reconciliation fixes drift |
| Worker | requester matching by email (active, invited, unknown); access request opened once with admin DMs; non allowlisted channel answered privately; reply posted in the thread; sync workflow results |
| Adapter | Block Kit rendering and limits; rate limiter; `429` retry with `Retry-After`; update coalescing |
| Console API | Slack status, install URL, allowlist toggle (schedules pause when removed), sync, disconnect, access request invite/dismiss; permissions and isolation |
| End to end (Playwright) | connect Slack from the console through the stand-in's OAuth page; allowlist a channel; a member mentions the bot in the Slack UI and gets the reply in the thread; an uninvited person gets the private notice and the admin's Invite click creates an invitation; App Home mode change; disconnect |
