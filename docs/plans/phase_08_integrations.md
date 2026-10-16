# Phase 8: Integrations framework

**Goal:** a uniform, safe way to add tools.

**Done when:** an admin connects Jira and GitHub, a user links their own accounts, and the agent answers Jira and GitHub questions with the right credential per operation. (No Jira, GitHub or Google accounts exist, B-007: everything runs against local stand-ins; staging is blocked by B-001.)

## Blocker workaround: provider stand-ins (B-007)

The `fakes` service gains the subset of each provider we use, at the HTTP boundary:
- **Atlassian / Jira**: OAuth 2.0 (3LO) authorize page and token endpoint (rotating refresh tokens, `offline_access`), `accessible-resources`, and Jira REST v3 under `/ex/jira/{cloudid}`: `search/jql`, `issue/{key}`, `project/search`, `myself`, `user/search`, `user/permission/search`. Seeded projects (one restricted), issues and accounts matching the fixture's people.
- **GitHub**: a GitHub App (installation page with user authorization, JWT authenticated installation tokens) and user OAuth (expiring user tokens with refresh, revocation), REST: `/installation/repositories`, `/user/installations`, `/user`, `/repos/{o}/{r}/contents/{path}`, `/repos/{o}/{r}/collaborators/{u}/permission`. Seeded repositories (one private that Bob cannot read) with files and CODEOWNERS.
- **Google**: OAuth (offline access, refresh, revocation, userinfo) and a vendor MCP server (Streamable HTTP JSON-RPC: `initialize`, `tools/list`, `tools/call`) with Sheets and Drive tools, authenticated with the Google access token.
- Consent pages pick who is authorizing, so Playwright can connect as an admin and link as a user.

## Backend

### Token vault service (`app vault`)

A separate internal service with its own database user (`vault_service`, role `app_vault`) and the providers' client secrets; the API and the worker never see refresh tokens or app private keys.
- `POST /v1/exchange`: OAuth code to tokens (the vault holds the client secret), account lookup (Jira cloud and account id, GitHub login, Google email), encrypted storage, connection metadata update.
- `POST /v1/github/installations`: register a GitHub App installation after checking, with the installer's user authorization, that they can access it.
- `POST /v1/credentials`: a short lived credential for one connection and one running task: OAuth access tokens refreshed when near expiry (rotating refresh tokens stored), GitHub installation tokens minted from the app key (one hour), static tokens (Slack bot token, MCP API keys) as they are. User links are issued only to tasks requested by their owner. A failed refresh marks the connection expired and the integration's health broken.
- `POST /v1/secrets`, `GET`/`DELETE`: raw secrets (Slack bot token, custom MCP API keys) and revocation (`POST /v1/revoke`: revoke at the provider where supported, delete the secret).
- Authenticated with a shared service token; every request names its org and runs in a tenant transaction.
- One `VaultClient` interface: `RemoteVault` (HTTP, used by the API and worker when `VAULT_URL` is set) and `LocalVault` (in process, used by tests and as a fallback).
- Migration 006: `app_vault` may update connection metadata columns (account id, scopes, status, expiry, refresh time), read task status and requester (task scoping), and update integration health.

### Integration catalog and connections

- Catalog in code (`app/integrations/catalog.py`): Slack, Jira, GitHub, Google Workspace (vendor MCP), and per org custom MCP servers: auth kind, scopes, whether user links are supported, default credential modes.
- OAuth: a generic authorization code flow with signed single use state (org, membership, integration, org or user scope, nonce; PKCE verifier kept in Key Value). `/oauth/{integration}/callback`: verify state, create the connection row (replacing the previous active one), commit, then ask the vault to exchange the code; revoke the row if that fails. GitHub org connections use the App installation flow.
- Console API: list integrations with enablement, org connection and health, my links; enable or disable (admin); connect and disconnect (admin); link and unlink (any member); credential mode and enablement overrides per operation (admin); custom MCP servers: add (admin, URL and optional API key) and approve (admin); check health now.

### Tool execution

- `ToolExecutor` (`app/tools/executor.py`) runs every tool call the same way: resolve the target, `Policy.check_tool`, pick the credential the decision names (org connection, org connection with check, or the requester's link) through the vault, run the adapter, cap and redact. Gather and act both use it.
- Adapters receive the credential with its mode; with `org_with_check` they must verify the requester can access every resource they return.
- Jira adapter (read): `search_issues`, `get_issue`, `list_projects`. With the org connection, results are filtered to projects the requester's Jira account (their link, or matched by email) can browse.
- GitHub adapter (read): `list_repos`, `read_file`, `get_codeowners`. Reads use the installation token; with `org_with_check` the requester's linked GitHub login must have read access to the repository.
- MCP adapter: Streamable HTTP client; tools discovered with `tools/list` (cached), operation metadata from tool annotations (read only, destructive), output capped and redacted. Vendor MCP integrations authenticate with their connection's token; custom MCP servers need admin approval before they can be enabled (database check plus API).
- Playbook `slack_qa` v2: questions and summaries over Slack, Jira, GitHub and approved MCP tools. Only enabled and connected integrations are loaded. The brief gains repositories and files.

### Console

Integrations page: each integration with enable toggle, connection status and health, Connect / Disconnect (admins), Link / Unlink my account (everyone, where supported), the operations with their default credential mode and an override (admins), custom MCP servers with Approve.

## Tests

| Area | Tests |
|---|---|
| Stand-ins | each provider's OAuth (codes single use, refresh rotation, revocation), REST subsets, permissions, MCP protocol |
| Vault service | service token required; exchange per provider; credentials refreshed near expiry and rotation stored; invalid refresh marks the connection expired and health broken; GitHub installation tokens minted; user links only for their owner's running task; another org's connection not found; revoked connections refused; revoke deletes the secret |
| OAuth flows | state forgery, reuse and expiry; non admins cannot connect; links for any member; exchange failure revokes the row; reconnect replaces the connection; GitHub installation not owned by the installer refused |
| Executor and adapters | credential per mode; user_required uses only the requester's link; Jira results filtered by project permission; GitHub file read refused without access or link; MCP discovery, annotations to tiers, caps and redaction; custom MCP refused until approved |
| Console API | permissions, overrides change the decision, disconnect revokes, health check |
| Agent (stand-ins, Temporal) | Jira and GitHub questions answered with sources and the right credential; refusals for restricted projects and repositories |
| End to end (Playwright) | admin connects Jira and GitHub through consent pages; a user links accounts; the agent answers a Jira question and a GitHub question in Slack; a restricted project is refused; an override takes effect; a custom MCP server needs approval |
