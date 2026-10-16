-- Production readiness (plan phase 13): shadow mode, plans, alerts, retention and offboarding.
--
-- Shadow mode (spec 15): the agent reads, plans and answers, but the policy module denies every
-- write. Orgs created by self service sign up (the Clerk webhook) start in it; admins turn it off.
--
-- Plans (spec 4.8): each org is on one; the plan caps what its admins may set (token limits,
-- concurrency, schedules, retention, model tiers). New orgs start on Free with its limits applied;
-- operators change an org's plan (`app org plan`), which brings its settings within the new plan.
--
-- Alerts read counts across tenants through one security definer function.
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

-- Plans are platform data (no org_id): every org may read them; only migrations change them.
-- A null token ceiling means admins may lift the limit altogether.
create table app.plans (
  key                      text primary key check (key ~ '^[a-z][a-z0-9_]*$'),
  title                    text not null,
  user_daily_token_limit   bigint check (user_daily_token_limit > 0),
  org_monthly_token_limit  bigint check (org_monthly_token_limit > 0),
  max_concurrent_tasks     integer not null check (max_concurrent_tasks between 1 and 1000),
  max_schedules            integer not null check (max_schedules between 0 and 10000),
  max_retention_days       integer not null check (max_retention_days between 1 and 3650),
  model_tiers              app.model_tier[] not null check (cardinality(model_tiers) > 0)
);
insert into app.plans values
  ('free',       'Free',       250000,  5000000,   3,     5,   90, '{fast,standard}'),
  ('team',       'Team',       2000000, 100000000, 25,    100, 365, '{fast,standard,advanced}'),
  ('enterprise', 'Enterprise', null,    null,      1000, 10000, 3650, '{fast,standard,advanced}');
alter table app.plans enable row level security;
create policy plans_read on app.plans for select using (true);
grant select on app.plans to app_user, app_maintenance;

alter table app.orgs add constraint orgs_plan_known foreign key (plan) references app.plans (key);

-- Brings an org's settings within its plan: limits above a ceiling come down to it (no limit
-- counts as above), tiers the plan does not offer go (keeping at least the plan's own).
create function app.apply_plan(p_org uuid)
returns void
language plpgsql security definer set search_path = app, pg_temp as $$
declare
  p       app.plans;
  v_tiers app.model_tier[];
begin
  select pl.* into strict p from app.plans pl join app.orgs o on o.plan = pl.key where o.id = p_org;
  select coalesce(array_agg(t order by n), p.model_tiers) into v_tiers
    from app.org_settings s, unnest(s.allowed_model_tiers) with ordinality as u(t, n)
   where s.org_id = p_org and t = any (p.model_tiers);
  update app.org_settings s
     set user_daily_token_limit = case when p.user_daily_token_limit is null then s.user_daily_token_limit
           else least(coalesce(s.user_daily_token_limit, p.user_daily_token_limit), p.user_daily_token_limit) end,
         org_monthly_token_limit = case when p.org_monthly_token_limit is null then s.org_monthly_token_limit
           else least(coalesce(s.org_monthly_token_limit, p.org_monthly_token_limit), p.org_monthly_token_limit) end,
         max_concurrent_tasks = least(s.max_concurrent_tasks, p.max_concurrent_tasks),
         max_schedules = least(s.max_schedules, p.max_schedules),
         retention_days = least(s.retention_days, p.max_retention_days),
         allowed_model_tiers = v_tiers,
         default_model_tier = case when s.default_model_tier = any (v_tiers) then s.default_model_tier
           when 'standard' = any (v_tiers) then 'standard' else v_tiers[1] end
   where s.org_id = p_org;
end $$;
revoke all on function app.apply_plan(uuid) from public;

-- An operator moves an org to another plan (`app org plan`). Returns false for an unknown org.
create function app.maintenance_set_plan(p_org uuid, p_plan text)
returns boolean
language plpgsql security definer set search_path = app, pg_temp as $$
declare
  v_old text;
begin
  select plan into v_old from app.orgs where id = p_org for update;
  if not found then
    return false;
  end if;
  update app.orgs set plan = p_plan where id = p_org;
  perform app.apply_plan(p_org);
  insert into app.audit_log (org_id, actor_type, action, target, decision, metadata)
  values (p_org, 'system', 'maintenance.plan_changed', p_plan, 'info', jsonb_build_object('from', v_old));
  return true;
end $$;

-- Self service orgs start on the Free plan's limits and in shadow mode (the settings row is
-- created by the orgs insert trigger).
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
    perform app.apply_plan(v_id);
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

-- Figures for the alerts job, per active org: counts and ages only, no tenant content.
create function app.maintenance_alert_figures()
returns table (org_id uuid, name text, finished bigint, failed bigint, queued bigint,
               oldest_queued_seconds integer, budget_stops bigint, month_tokens bigint, month_limit bigint,
               schedules_paused bigint)
language sql stable security definer set search_path = app, pg_temp as $$
  select o.id, o.name,
         (select count(*) from app.tasks t where t.org_id = o.id
             and t.status in ('completed', 'failed', 'cancelled') and t.finished_at > now() - interval '1 hour'),
         (select count(*) from app.tasks t where t.org_id = o.id
             and t.status = 'failed' and t.finished_at > now() - interval '1 hour'),
         (select count(*) from app.tasks t where t.org_id = o.id and t.status = 'queued'),
         (select extract(epoch from now() - min(t.created_at))::integer from app.tasks t
           where t.org_id = o.id and t.status = 'queued'),
         -- A budget stops a task between steps: it finishes with the reason (TaskWorkflow._stopped).
         (select count(*) from app.tasks t where t.org_id = o.id and t.error like 'stopped:%'
             and t.finished_at > now() - interval '1 hour'),
         (select coalesce(sum(d.tokens), 0)::bigint from app.usage_daily d
           where d.org_id = o.id and d.day >= date_trunc('month', now() at time zone 'UTC')::date),
         s.org_monthly_token_limit,
         (select count(*) from app.schedules sc where sc.org_id = o.id and sc.status = 'paused'
             and sc.pause_reason <> 'admin' and sc.paused_at > now() - interval '1 hour')
    from app.orgs o join app.org_settings s on s.org_id = o.id
   where o.status = 'active'
   order by o.id
$$;

revoke all on function app.maintenance_orgs() from public;
revoke all on function app.maintenance_begin_offboarding(uuid) from public;
revoke all on function app.maintenance_offboard_org(uuid, interval) from public;
revoke all on function app.maintenance_drop_partitions(text, integer) from public;
revoke all on function app.maintenance_set_plan(uuid, text) from public;
revoke all on function app.maintenance_alert_figures() from public;
grant execute on function app.maintenance_orgs() to app_maintenance;
grant execute on function app.maintenance_begin_offboarding(uuid) to app_maintenance;
grant execute on function app.maintenance_offboard_org(uuid, interval) to app_maintenance;
grant execute on function app.maintenance_drop_partitions(text, integer) to app_maintenance;
grant execute on function app.maintenance_set_plan(uuid, text) to app_maintenance;
grant execute on function app.maintenance_alert_figures() to app_maintenance;
