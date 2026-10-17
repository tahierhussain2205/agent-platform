# 8. The admin console

The console (`web/`) is a single page app served as static files. It talks only to our API; the API decides everything about permissions.

## Structure

| Part | Where | Notes |
|---|---|---|
| Routing | `src/App.tsx` | React Router. Every console page sits under `ConsoleGate`, which waits for sign in, an active org and a confirmed membership |
| Layout | `src/components/Layout.tsx` | Sidebar navigation (a scrolling bar on phones), org switcher, theme switcher, the member's badge |
| Pages | `src/pages/` | Overview, Tasks, Members, Roles, Slack, Integrations, Catalog, Repositories, Schedules, Usage, Settings, Audit log |
| Data | `src/api/hooks.ts` | TanStack Query hooks, one per resource, keyed by the active org so switching orgs never shows stale data |
| UI kit | `src/components/ui.tsx` | Buttons, cards, badges, dialogs, fields, notices; all in semantic colors |

Members who are not admins see a read only console: the same pages without the controls, and no Usage, Settings or Audit pages. The API enforces the same split (`console_identity` for any member, `require_admin` for admins).

## Authentication

Two adapters implement one auth context (`src/auth/`):

- **Clerk** in staging and production: Clerk's components for sign in, sign up, org creation and invitations; the console sends Clerk's session token with each request.
- **Dev** locally: a sign in page against the Clerk stand-in, with the seeded accounts listed.

The API verifies the token (issuer, keys, the browser origin it was issued to) and takes the person's privilege from our database, never from the token. Right after sign up or accepting an invitation, the membership webhook may not have arrived yet; the console retries those answers for a few seconds instead of showing "no access".

## Data fetching and mutations

`useApi()` attaches the token; `useApiMutation(request, invalidate)` runs a change and refreshes the affected queries. Errors carry the API's error code and request id, and a 401 offers to sign in again. Pages that follow live work (Tasks, a running task's timeline, a role preview) poll only while something is still running.

## Themes

Light (white and greys) and dark (black and greys), chosen in the header or following the system. The choice is applied before the first paint by a same origin script (so the content security policy needs no inline script). Components use only semantic colors defined as CSS variables (`bg-surface`, `text-fg-2`, `ok`, `warn`, `bad`…), never palette colors, so every page works in both themes. A browser test checks every page for colors from the other theme, for pointer cursors on everything clickable, and for horizontal scrolling at phone widths.

## The console API

All routes are under `/api/` in `backend/app/api/console/` and bind the unit of work to the verified org.

| Area | Routes |
|---|---|
| Me and the org | `GET /api/me`, `GET/PATCH /api/org`, `GET/PATCH /api/settings`, `GET/PUT /api/autonomy` |
| People | `/api/members` (invite, privilege, remove, roles), `/api/access-requests` |
| Roles | `/api/roles` (create, edit, archive), `/api/roles/options`, `/api/roles/preview` |
| Slack | `/api/slack` (status, connect, disconnect, sync, channel allowlist) |
| Integrations | `/api/integrations` (enable, connect, link, health, credential mode overrides, custom MCP servers) |
| Catalog and code | `/api/catalog` (proposal, services, owners), `/api/repositories` (protected paths, test command) |
| Work | `/api/schedules` (create, edit, pause, run now, preview), `/api/tasks` (list and timeline) |
| Insight | `/api/usage`, `/api/audit` |

Every change writes an audit row. Identity changes (invitations, privilege, removal) are applied through identity functions inside a transaction that calls Clerk, and commit only if Clerk succeeded.

## Testing

Each page has unit tests next to it (Vitest and Testing Library, with a mocked `fetch`), and the browser suite (Playwright) runs the real flows against the full local stack: sign up, invitations, roles, connecting integrations through consent pages, schedules, the Slack flows through the Slack stand-in's UI, and the theme, cursor and phone width checks.
