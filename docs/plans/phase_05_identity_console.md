# Phase 5: Identity and admin console foundations

**Goal:** an admin can sign up, set up an org, and manage people and roles.

**Done when:** on staging, a new signup gets an org and admin role; invites, promotions, removal and the last admin rule work from the console; roles with a primary role can be assigned. (Staging is blocked by B-001 and Clerk by B-003; everything runs locally against a Clerk stand-in.)

## Blocker workaround: a Clerk stand-in

A new `fakes` service (Python, FastAPI, its own image, Compose only, never deployed) hosts stand-ins for external SaaS. Phase 5 adds Clerk at `http://localhost:4100/clerk`:

| Real Clerk | Stand-in |
|---|---|
| Backend API `https://api.clerk.com/v1` (secret key auth) | Same paths and JSON shapes for what we call: users, organizations, memberships (update role, delete), invitations (create, list, revoke), `GET /v1/jwks` |
| Session tokens: RS256 JWT v2 (`sub`, `sid`, `sts`, `v: 2`, `o: {id, rol, slg}`, `azp`, `iss`, `exp`, `nbf`, `iat`) | Same claims, signed with a stand-in RSA key, JWKS at `/.well-known/jwks.json`. 60 second lifetime. `sts: pending` while the user has no org (Clerk's "organization required") |
| Webhooks through Svix (`svix-id`, `svix-timestamp`, `svix-signature: v1,<base64 HMAC>`) | Same signing scheme and event payloads (`user.*`, `organization.*`, `organizationMembership.*`, `organizationInvitation.*`), delivered asynchronously with retries |
| Hosted UI / ClerkJS (sign in, sign up, org switcher) | A small dev JSON API (`/clerk/dev/...`) that the console's dev auth adapter uses: sign up, sign in, sessions, tokens for the active org, create org, accept invitation, sign out |

State is seeded from `backend/app/seed/fixture.json` (same people and orgs as the database seed, dev password for everyone) and persisted to a volume; `/_admin/reset` restores the seed for tests.

The API only knows base URLs, a JWKS URL, an issuer and keys, so switching to real Clerk is configuration. The console has two auth adapters behind one interface: `@clerk/react` (Core 3) when `VITE_CLERK_PUBLISHABLE_KEY` is set, the dev adapter otherwise.

## Backend

### Migration 002: identity sync and console resolvers

Security definer functions owned by `app_owner` (no reliance on BYPASSRLS, D-034):

| Function | Executable by | Does |
|---|---|---|
| `identity_upsert_user(clerk_user_id, email, name)` | `app_identity_sync` | Upsert by Clerk id; otherwise link the invited user with that email (first signup) |
| `identity_upsert_org(clerk_org_id, name)` | `app_identity_sync` | Upsert by Clerk org id |
| `identity_upsert_membership(clerk_org_id, clerk_user_id, email, name, privilege)` | `app_identity_sync` | Ensure user and org, membership `active` with the privilege |
| `identity_invite(clerk_org_id, email, privilege, invited_by_clerk_user_id)` | `app_identity_sync` | User by email (no Clerk id yet), membership `invited` unless already active |
| `identity_set_privilege(clerk_org_id, clerk_user_id, privilege)` | `app_identity_sync` | Promote or demote (last admin trigger applies) |
| `identity_remove_membership(clerk_org_id, clerk_user_id)` / `identity_revoke_invitation(clerk_org_id, email)` | `app_identity_sync` | Status `removed` (removal trigger revokes links, pauses schedules, closes sessions) |
| `identity_disable_user(clerk_user_id)`, `identity_offboard_org(clerk_org_id)` | `app_identity_sync` | Clerk deletions: disable the user and remove memberships (keeping a membership only where it is the org's last admin, recorded in the audit log); mark the org `offboarding` |
| `resolve_console_identity(clerk_user_id, clerk_org_id)` | `app_user` | Membership, privilege and statuses: **privilege comes from our database, not the token** |
| `list_console_orgs(clerk_user_id)` | `app_user` | The user's active orgs (dev org switcher) |

Every identity function writes an audit row (`actor_type` system, action `identity.*`).

### Console authentication (spec 14.3)

`app/auth/clerk.py`: RS256 only; JWKS cached by `kid` with a refresh on an unknown `kid` (at most every 10 s); `iss` must equal `CLERK_ISSUER`; `exp`/`nbf` with 5 s leeway; `azp` must be in `CONSOLE_ORIGINS`; `sts == "pending"` rejected; org from `o.id`, falling back to v1 `org_id`. Then `resolve_console_identity`: unknown membership, `invited` or `removed` status, disabled user or non active org are refused (403). `require_admin` additionally needs an active admin membership. The verified identity binds the unit of work to its org.

### Identity sync (Clerk webhooks)

`POST /webhooks/clerk`: Svix signature (any of the `v1,` signatures, timestamp within 5 minutes), dedupe on `svix-id` in Key Value (claim, release on failure so Svix retries), dispatch by event type through the identity functions as `identity_service`. Unknown event types are acknowledged and ignored; bad signatures get 400; processing errors 500 (Svix retries).

### Console writes go to Clerk, mirrored synchronously

Clerk is the source of truth; the console never writes memberships directly. For invite, promote, demote, remove and revoke the API:
1. opens an identity transaction and applies the change through the identity function (the last admin trigger locks the org row and can refuse it here, before Clerk is touched);
2. calls Clerk's Backend API; if Clerk fails, rolls back (502);
3. commits. The webhook that follows is an idempotent no op.

This keeps the database guard authoritative and Clerk consistent (a crash between 2 and 3 is repaired by the webhook).

### Console API (`/api/*`)

| Route | Who |
|---|---|
| `GET /api/me`, `GET /api/orgs` | any member |
| `GET /api/org`, `PATCH /api/org` (name, via Clerk) | members read, admins write |
| `GET /api/members`; `POST /api/members/invitations`; `DELETE /api/members/invitations/{id}`; `PATCH /api/members/{id}` (privilege); `DELETE /api/members/{id}`; `PUT /api/members/{id}/roles` (role ids + primary) | members read; admins write |
| `GET/POST /api/roles`, `PATCH /api/roles/{id}`, `POST /api/roles/{id}/archive` | members read; admins write |
| `GET/PATCH /api/settings` | admins |
| `GET /api/audit` (filters, keyset pagination) | admins |
| `GET /api/access-requests` | admins (filled in phase 6) |

Every mutation writes an audit row with the acting membership. CORS allows `CONSOLE_ORIGINS` only.

## Web console

- React Router, TanStack Query, Tailwind CSS. One `AuthAdapter` interface (`getToken`, user, orgs, active org, switch org, sign out) with the Clerk and dev implementations; the Clerk one is lazy loaded.
- Pages: sign in, sign up, create org, invitations (accept), Overview (org name, counts), Members (table, invite, pending invitations, promote/demote, remove, roles with primary), Roles (create, edit, archive), Settings (models and limits), Audit (filters, paging). Non admins see Overview and the member list read only; admin pages are hidden and the API refuses them.
- Errors from the API are shown inline with the request id.

## Tests

| Area | Tests |
|---|---|
| Migration 002 | every identity function (idempotency, invited user linking by email, re-invite after removal, last admin refusal, disable and offboard), resolver outcomes, grants (only the right role can execute each function), isolation of new functions |
| Token verification | valid token; wrong signature, wrong `kid` with refresh, expired, not yet valid, wrong issuer, wrong `azp`, `alg: none`/HS256 confusion, pending session, missing org, v1 `org_id` fallback, JWKS fetch failure |
| Console identity | unknown org/user, invited, removed, disabled, suspended org refused; non admin refused on admin routes; privilege from the database even if the token says admin |
| Webhooks | signature checks (missing headers, bad signature, old timestamp, rotated secret list), dedupe, each event type mirrors correctly, out of order membership before user, retry after failure |
| Console API | every route: happy path, validation (422), permission (403), cross org access by id (404), audit rows written, Clerk failure rolls back (502), last admin refused (409) |
| Fakes | Clerk stand-in unit tests: token claims, Svix signatures, Backend API shapes, webhook retries |
| Web | component tests (auth adapter, members table, role editor, error display) |
| End to end (Playwright) | signup → create org → admin; invite → invitee signs up → accepts → active; promote and demote; last admin refused; remove → removed user locked out; roles with primary, change primary, archive; settings validation; audit shows actions; non admin view; org switcher for a member of two orgs |
