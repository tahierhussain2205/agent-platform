/*
  003: Slack reconciliation support.

  The maintenance cron starts a membership reconciliation per active workspace. Listing
  workspaces crosses tenants, so it is a security definer function executable only by
  app_maintenance (no reliance on BYPASSRLS, decision D-034).
*/

set local role app_owner;
set local search_path = app, pg_temp;

create function app.maintenance_active_workspaces()
returns table (org_id uuid, workspace_id uuid, platform text, external_id text)
language sql stable security definer set search_path = app, pg_temp as $$
  select w.org_id, w.id, w.platform, w.external_id
    from app.workspaces w
    join app.orgs o on o.id = w.org_id
   where w.status = 'active' and o.status = 'active'
   order by w.org_id
$$;

revoke all on function app.maintenance_active_workspaces() from public;
grant execute on function app.maintenance_active_workspaces() to app_maintenance;
