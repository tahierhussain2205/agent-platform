/*
  004: task reconciliation support.

  Temporal is the source of truth for execution; app.tasks is the product's view of it. If a task
  workflow ends without closing its row (terminated by an operator, a bug), the row would stay
  open and block its thread. The maintenance job lists open tasks across tenants, asks Temporal
  about each, and fails the ones whose workflow is gone. Both steps cross tenants, so they are
  security definer functions executable only by app_maintenance (decision D-034).
*/

set local role app_owner;
set local search_path = app, pg_temp;

create function app.maintenance_open_tasks(p_older_than interval)
returns table (org_id uuid, task_id uuid, workflow_id text, status text, updated_at timestamptz)
language sql stable security definer set search_path = app, pg_temp as $$
  select t.org_id, t.id, t.workflow_id, t.status, t.updated_at
    from app.tasks t
   where t.status in ('queued', 'running', 'awaiting_approval')
     and t.updated_at < now() - p_older_than
   order by t.updated_at
   limit 500
$$;

-- Fails one open task, expires its pending approvals and audits it. False if it was not open.
create function app.maintenance_fail_task(p_task uuid, p_reason text)
returns boolean
language sql volatile security definer set search_path = app, pg_temp as $$
  with done as (
    update app.tasks
       set status = 'failed', finished_at = now(), error = left(p_reason, 2000)
     where id = p_task and status in ('queued', 'running', 'awaiting_approval')
    returning org_id, id
  ), expired as (
    update app.approvals set status = 'expired'
     where task_id in (select id from done) and status = 'pending'
    returning 1
  ), audited as (
    insert into app.audit_log (org_id, actor_type, task_id, action, decision, reason)
    select org_id, 'system', id, 'task.reconciled', 'info', left(p_reason, 1000) from done
    returning 1
  )
  select exists (select 1 from done)
$$;

revoke all on function app.maintenance_open_tasks(interval) from public;
revoke all on function app.maintenance_fail_task(uuid, text) from public;
grant execute on function app.maintenance_open_tasks(interval) to app_maintenance;
grant execute on function app.maintenance_fail_task(uuid, text) to app_maintenance;
