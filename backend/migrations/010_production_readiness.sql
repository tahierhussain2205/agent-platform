-- Production readiness (plan phase 13): shadow mode, retention and offboarding.
--
-- Shadow mode (spec 15): the agent reads, plans and answers, but the policy module denies every
-- write. Orgs created by self service sign up (the Clerk webhook) start in it; admins turn it off.
--
-- Retention and offboarding run as the maintenance role, which has no BYPASSRLS on managed
-- Postgres (B-010): per org work sets the org's context like any tenant transaction; work that
-- crosses tenants (listing orgs, deleting an org, dropping old partitions) goes through security
-- definer functions owned by app_owner, executable by app_maintenance only.
set local role app_owner;
set local search_path = app, pg_temp;

alter table app.org_settings add column shadow_mode boolean not null default false;
grant update (shadow_mode) on app.org_settings to app_user;

-- When an org entered offboarding: the grace period before its data is deleted counts from here.
alter table app.orgs add column offboarding_since timestamptz;
update app.orgs set offboarding_since = now() where status = 'offboarding';

create function app.orgs_offboarding_since() returns trigger
language plpgsql as $$
begin
  if new.status = 'offboarding' and old.status is distinct from 'offboarding' then
    new.offboarding_since := now();
  elsif new.status <> 'offboarding' then
    new.offboarding_since := null;
  end if;
  return new;
end $$;

create trigger orgs_offboarding_since before update of status on app.orgs
  for each row execute function app.orgs_offboarding_since();

-- Self service orgs start in shadow mode (the settings row is created by the orgs insert trigger).
create or replace function app.identity_upsert_org(p_clerk_org_id text, p_name text)
returns uuid
language plpgsql security definer set search_path = app, pg_temp as $$
declare
  v_id uuid;
  v_created boolean;
begin
  if coalesce(p_clerk_org_id, '') = '' then
    raise exception 'clerk org id is required' using errcode = 'check_violation';
  end if;
  insert into app.orgs (clerk_org_id, name)
  values (p_clerk_org_id, coalesce(nullif(btrim(p_name), ''), p_clerk_org_id))
  on conflict (clerk_org_id) do update
     -- p_name, not excluded.name: the insert falls back to the Clerk id for a blank name.
     set name = coalesce(nullif(btrim(p_name), ''), app.orgs.name)
  returning id, (xmax = 0) into v_id, v_created;
  if v_created then
    update app.org_settings set shadow_mode = true where org_id = v_id;
    perform app.identity_audit(v_id, 'identity.org_created', p_clerk_org_id);
  end if;
  return v_id;
end $$;

-- Every org with what the lifecycle jobs need.
create function app.maintenance_orgs()
returns table (org_id uuid, status text, retention_days integer, offboarding_since timestamptz)
language sql stable security definer set search_path = app, pg_temp as $$
  select o.id, o.status, s.retention_days, o.offboarding_since
    from app.orgs o join app.org_settings s on s.org_id = o.id
   order by o.id
$$;

-- An operator offboards an org (`app offboard`): it stops being served at once; its data goes after
-- the grace period. Returns false for an unknown org or one already offboarding.
create function app.maintenance_begin_offboarding(p_org uuid)
returns boolean
language plpgsql security definer set search_path = app, pg_temp as $$
begin
  update app.orgs set status = 'offboarding' where id = p_org and status <> 'offboarding';
  if not found then
    return false;
  end if;
  insert into app.audit_log (org_id, actor_type, action, target, decision, metadata)
  values (p_org, 'system', 'maintenance.org_offboarding', p_org::text, 'info', '{}');
  return true;
end $$;

-- Deletes an offboarding org whose grace period has passed: every tenant row goes with it (each
-- tenant table references orgs with on delete cascade), and so do the people who belonged to no
-- other org. Returns false when the org is not offboarding or still in its grace period.
create function app.maintenance_offboard_org(p_org uuid, p_grace interval)
returns boolean
language plpgsql security definer set search_path = app, pg_temp as $$
declare
  v_people uuid[];
begin
  select array_agg(user_id) into v_people from app.org_memberships where org_id = p_org;
  delete from app.orgs
   where id = p_org and status = 'offboarding' and offboarding_since <= now() - p_grace;
  if not found then
    return false;
  end if;
  delete from app.users u
   where u.id = any(coalesce(v_people, '{}'))
     and not exists (select 1 from app.org_memberships m where m.user_id = u.id);
  return true;
end $$;

-- Monthly partitions of usage events or the audit log older than p_keep_months months (counting the
-- current month as the first). The default partition is never dropped.
create function app.maintenance_drop_partitions(p_parent text, p_keep_months integer)
returns integer
language plpgsql security definer set search_path = app, pg_temp as $$
declare
  v_name    text;
  v_cutoff  date := (date_trunc('month', now() at time zone 'UTC') - make_interval(months => p_keep_months - 1))::date;
  v_dropped integer := 0;
begin
  if p_parent not in ('usage_events', 'audit_log') or p_keep_months < 2 then
    raise exception 'partitions of % cannot be dropped this way', p_parent using errcode = 'check_violation';
  end if;
  for v_name in
    select c.relname
      from pg_inherits i
      join pg_class c on c.oid = i.inhrelid
      join pg_class p on p.oid = i.inhparent
      join pg_namespace n on n.oid = p.relnamespace
     where n.nspname = 'app' and p.relname = p_parent
       and c.relname ~ ('^' || p_parent || '_[0-9]{4}_[0-9]{2}$')
  loop
    if to_date(right(v_name, 7), 'YYYY_MM') < v_cutoff then
      execute format('drop table app.%I', v_name);
      v_dropped := v_dropped + 1;
    end if;
  end loop;
  return v_dropped;
end $$;

revoke all on function app.maintenance_orgs() from public;
revoke all on function app.maintenance_begin_offboarding(uuid) from public;
revoke all on function app.maintenance_offboard_org(uuid, interval) from public;
revoke all on function app.maintenance_drop_partitions(text, integer) from public;
grant execute on function app.maintenance_orgs() to app_maintenance;
grant execute on function app.maintenance_begin_offboarding(uuid) to app_maintenance;
grant execute on function app.maintenance_offboard_org(uuid, interval) to app_maintenance;
grant execute on function app.maintenance_drop_partitions(text, integer) to app_maintenance;
