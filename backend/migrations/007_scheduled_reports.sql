/*
  007: scheduled reports.

  A schedule's Temporal Schedule mirrors its row. `revision` counts changes to what a schedule is
  (owner, definition, status); `synced_revision` is the revision Temporal last matched, so a row
  needs syncing when the two differ. Marking a row synced changes neither its revision nor its
  definition. `paused_at` and `pause_notified_at` drive the admin notification on every pause;
  `pause_detail` says what happened, in words for admins.

  Report parameters live in `params` (validated in code: channel ids, a Jira project).
*/

set local role app_owner;
set local search_path = app, pg_temp;

alter table app.schedules
  add column params            jsonb not null default '{}' check (jsonb_typeof(params) = 'object'),
  add column revision          integer not null default 1 check (revision > 0),
  add column synced_revision   integer,
  add column paused_at         timestamptz,
  add column pause_detail      text check (length(pause_detail) <= 500),
  add column pause_notified_at timestamptz;

update app.schedules set paused_at = updated_at where status = 'paused';

create function app.schedules_track() returns trigger
language plpgsql as $$
declare
  v_changed boolean;
begin
  if tg_op = 'INSERT' then
    new.revision := 1;
    new.paused_at := case when new.status = 'paused' then now() end;
    new.pause_notified_at := null;
    return new;
  end if;
  v_changed := (new.owner_membership_id, new.name, new.instruction, new.playbook_key, new.params, new.cron,
                new.timezone, new.delivery_type, new.delivery_channel_id, new.status, new.pause_reason)
               is distinct from
               (old.owner_membership_id, old.name, old.instruction, old.playbook_key, old.params, old.cron,
                old.timezone, old.delivery_type, old.delivery_channel_id, old.status, old.pause_reason);
  -- Only a change to the schedule moves its revision; nobody sets it by hand.
  new.revision := case when v_changed then old.revision + 1 else old.revision end;
  if new.status = 'paused' and (old.status <> 'paused' or new.pause_reason is distinct from old.pause_reason) then
    new.paused_at := now();
    new.pause_notified_at := null;
  elsif new.status <> 'paused' then
    new.paused_at := null;
    new.pause_detail := null;
    new.pause_notified_at := null;
  end if;
  return new;
end $$;

create trigger schedules_track
  before insert or update on app.schedules
  for each row execute function app.schedules_track();

-- Orgs whose schedules need attention: Temporal out of date, or a pause nobody was told about.
create function app.maintenance_schedule_orgs()
returns table (org_id uuid)
language sql stable security definer set search_path = app, pg_temp as $$
  select distinct s.org_id
    from app.schedules s
   where s.synced_revision is distinct from s.revision
      or (s.status = 'paused' and s.pause_notified_at is null and s.pause_reason <> 'admin')
   limit 1000
$$;

revoke all on function app.maintenance_schedule_orgs() from public;
grant execute on function app.maintenance_schedule_orgs() to app_maintenance;
