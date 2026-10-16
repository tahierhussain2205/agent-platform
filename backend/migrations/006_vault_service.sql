/*
  006: grants for the token vault service.

  The vault (role app_vault) exchanges OAuth codes, refreshes tokens and issues short lived,
  task scoped credentials. Besides the ciphertext it already owns, it needs to:
  - record what it learned about a connection (account id, granted scopes, expiry, refresh time)
    and mark a connection expired when a refresh fails;
  - check that a credential is requested for a running task, and that a user link is only used
    for a task its owner requested;
  - mark an integration's health broken when its connection stops working.
  Column level grants keep everything else (owners, scopes, enablement) out of its reach.
*/

set local role app_owner;

grant update (external_account_id, granted_scopes, status, expires_at, last_refreshed_at)
  on app.connections to app_vault;
grant select (id, org_id, status, requested_by) on app.tasks to app_vault;
grant select (id, org_id, integration_key, kind, health) on app.org_integrations to app_vault;
grant update (health) on app.org_integrations to app_vault;
