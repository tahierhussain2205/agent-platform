# Slack Marketplace preparation

Status: draft, prepared before a Slack app exists (blocker B-004). Commercially distributed apps must be listed in the Slack Marketplace; until approval, `conversations.history` and `conversations.replies` are limited to 1 request per minute with 15 items per page (spec section 10), which makes reading threads impractical. Start the submission early.

## Listing requirements to prepare

| Item | Status |
|---|---|
| App name, icon (512 px), short and long description | Draft in `slack/manifest.yaml`; icon to design |
| Public landing page, privacy policy URL, support URL and email | To write and host |
| Installation flow from the website ("Add to Slack") | Implemented as the console's Connect Slack (OAuth v2) |
| Event subscriptions over HTTPS (no Socket Mode, which the Marketplace does not allow) | Implemented (`/slack/events`, `/slack/interactions`) |
| Data handling answers: what is stored, retention, deletion | See below |
| Test workspace and reviewer instructions | To prepare once staging exists |

## Scopes and why each is needed

| Scope | Used for | Without it |
|---|---|---|
| `app_mentions:read` | Receive `@agent` mentions in channels the app is in | The agent cannot be asked anything in channels |
| `im:history` | Receive and read direct messages to the agent | No direct conversations |
| `channels:history` | Read the thread or channel a person asks about, on demand, in public channels the app is in and the admin allowed | Cannot summarize or investigate public conversations |
| `groups:history` | Same for private channels the app was invited to and the admin allowed | Private channels cannot be used even when allowed |
| `channels:read` | List public channels for the admin's allowlist; channel lifecycle and membership events | Admins cannot choose channels; membership checks go stale |
| `groups:read` | Same for private channels the app is in | Private channel allowlist and membership checks impossible |
| `users:read` | Show names, detect bots | Cannot tell people apart in summaries |
| `users:read.email` | Match a Slack person to an invited org user by email (the only automatic, admin controlled identity link) | Every person would need a manual linking step |
| `chat:write` | Reply in threads, ephemeral notices, DMs to admins, App Home messages | The agent cannot answer |

Not requested on purpose: `channels:join` (the app never joins channels by itself; people invite it), `chat:write.public`, `files:read`, `search:read`, any user scope (reads use the bot token plus a membership check, decision T6).

## Data handling

- Raw Slack messages are never stored. Messages are fetched on demand for the request at hand; only derived artifacts (summaries, briefs) and permalinks are stored, with per org retention (default 90 days, 1 to 3650).
- Workflow history holds references only (channel, timestamp, user id), never message text.
- Bot tokens are envelope encrypted per tenant and readable only by the vault service.
- Uninstalling the app (or revoking its token) revokes the connection and deletes the token immediately.
- No bulk indexing or embedding of workspace data.
