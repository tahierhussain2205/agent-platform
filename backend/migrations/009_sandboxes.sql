-- Sandboxes for read only repository work (plan phase 11; spec 8.1 and 8.2).
--
-- One sandbox per task and repository, at one base commit for the whole task. Investigators that
-- run in parallel share it: the first claims the row (insert ... on conflict) and creates it at
-- the provider; the others wait until it is ready. The task's end destroys it; the maintenance job
-- destroys whatever expired.
set local role app_owner;
set local search_path = app, pg_temp;

create table app.sandboxes (
  id            uuid primary key default app.uuid_v7(),
  org_id        uuid not null references app.orgs (id) on delete cascade,
  task_id       uuid not null,
  repository    text not null check (repository ~ '^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$'),
  base_sha      text check (base_sha ~ '^[0-9a-f]{7,64}$'),
  provider_id   text check (length(provider_id) between 1 and 200),
  status        text not null default 'creating'
                check (status in ('creating', 'ready', 'failed', 'destroyed')),
  error         text check (length(error) <= 500),
  expires_at    timestamptz not null,
  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now(),
  destroyed_at  timestamptz,
  unique (task_id, repository),
  unique (org_id, id),
  foreign key (org_id, task_id) references app.tasks (org_id, id) on delete cascade,
  constraint sandbox_ready_has_provider
    check (status <> 'ready' or (provider_id is not null and base_sha is not null)),
  constraint sandbox_destroyed_at check ((status = 'destroyed') = (destroyed_at is not null))
);
create index sandboxes_live on app.sandboxes (expires_at) where status in ('creating', 'ready');

alter table app.sandboxes enable row level security;
create policy tenant_isolation on app.sandboxes
  using (org_id = app.current_org_id())
  with check (org_id = app.current_org_id());
create trigger sandboxes_updated_at before update on app.sandboxes
  for each row execute function app.set_updated_at();

grant select, insert, update on app.sandboxes to app_user;

-- Expired sandboxes of every org, oldest first, for the maintenance job.
create function app.maintenance_expired_sandboxes(p_limit integer default 100)
returns table (org_id uuid, id uuid, provider_id text)
language sql stable security definer set search_path = app, pg_temp as $$
  select s.org_id, s.id, s.provider_id
    from app.sandboxes s
   where s.status in ('creating', 'ready', 'failed') and s.expires_at < now()
   order by s.expires_at
   limit p_limit
$$;

-- Marks one sandbox destroyed after the provider removed it (or never had it).
create function app.maintenance_sandbox_destroyed(p_sandbox uuid)
returns void
language sql volatile security definer set search_path = app, pg_temp as $$
  update app.sandboxes set status = 'destroyed', destroyed_at = now()
   where id = p_sandbox and status <> 'destroyed'
$$;

revoke all on function app.maintenance_expired_sandboxes(integer) from public;
revoke all on function app.maintenance_sandbox_destroyed(uuid) from public;
grant execute on function app.maintenance_expired_sandboxes(integer) to app_maintenance;
grant execute on function app.maintenance_sandbox_destroyed(uuid) to app_maintenance;
