/*
  Slack agent platform: Postgres schema
  Target: PostgreSQL 16 (runs on 15+; on 18, swap app.uuid_v7() for the built in uuidv7())

  HOW TENANT ISOLATION WORKS
  1. Every tenant table has org_id. Row level security limits each query to the
     org named in the transaction setting app.org_id.
  2. The API sets it at the start of every transaction:
        select set_config('app.org_id', '<org uuid>', true);
     Always transaction scoped (third argument true) so a pooled connection
     never carries one tenant's context into another tenant's request.
  3. No setting means no rows: reads return nothing, writes fail.
  4. Children reference parents through composite (org_id, id) foreign keys,
     so a row in one org can never point at a row in another, even with a
     guessed id. Foreign key checks bypass RLS, so this matters.

  ROLES (all NOLOGIN; see deployment notes at the end of this file)
  app_owner          owns every object, runs migrations
  app_user           API and workers; always subject to RLS
  app_vault          token vault service; the only role that can read secrets
  app_identity_sync  Clerk webhook handler; writes orgs, users, memberships
  app_maintenance    retention, offboarding, partitions, reconciliation

  Run as a superuser (or a role allowed to create roles and grant BYPASSRLS):
     psql -v ON_ERROR_STOP=1 -f schema.sql
*/

-- (transaction managed by the migration runner; see app/db/migrate.py)

/* ================================================================== ROLES */

do $$
declare r text;
begin
  foreach r in array array['app_owner','app_user','app_vault','app_identity_sync','app_maintenance'] loop
    if not exists (select 1 from pg_roles where rolname = r) then
      execute format('create role %I nologin', r);
    end if;
  end loop;
end $$;

alter role app_user          nobypassrls;
alter role app_vault         nobypassrls;
alter role app_identity_sync bypassrls;
alter role app_maintenance   bypassrls;

create schema app authorization app_owner;
set local role app_owner;
set local search_path = app, pg_temp;

/* ================================================================ HELPERS */

-- Time ordered UUID (version 7): 48 bit millisecond timestamp, then random bits.
create function app.uuid_v7() returns uuid
language sql volatile parallel safe as $$
  select encode(
           set_bit(set_bit(
             overlay(uuid_send(gen_random_uuid())
                     placing substring(int8send((extract(epoch from clock_timestamp()) * 1000)::bigint) from 3)
                     from 1 for 6),
             52, 1), 53, 1),
           'hex')::uuid
$$;

-- The org for the current transaction, or null when none is set.
create function app.current_org_id() returns uuid
language sql stable parallel safe as $$
  select nullif(current_setting('app.org_id', true), '')::uuid
$$;

create function app.set_updated_at() returns trigger
language plpgsql as $$
begin
  new.updated_at := now();
  return new;
end $$;

create function app.validate_timezone() returns trigger
language plpgsql as $$
begin
  perform now() at time zone new.timezone;
  return new;
exception when invalid_parameter_value then
  raise exception 'invalid time zone: %', new.timezone using errcode = 'check_violation';
end $$;

create domain app.model_tier    as text check (value in ('fast', 'standard', 'advanced'));
create domain app.autonomy_mode as text check (value in ('ask', 'auto'));
create domain app.slug          as text check (value ~ '^[a-z][a-z0-9_]{1,62}$');

/* ====================================================== 1. ORGS AND PEOPLE */

create table app.orgs (
  id            uuid primary key default app.uuid_v7(),
  clerk_org_id  text not null unique,
  name          text not null check (length(btrim(name)) between 1 and 200),
  region        text not null default 'us',
  plan          text not null default 'free',
  status        text not null default 'active' check (status in ('active', 'suspended', 'offboarding')),
  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now()
);

-- Typed columns for everything the policy module enforces; display_settings for the rest.
create table app.org_settings (
  org_id                   uuid primary key references app.orgs (id) on delete cascade,
  default_model_tier       app.model_tier not null default 'standard',
  allowed_model_tiers      app.model_tier[] not null default '{fast,standard,advanced}',
  user_daily_token_limit   bigint check (user_daily_token_limit > 0),     -- null means no limit
  org_monthly_token_limit  bigint check (org_monthly_token_limit > 0),    -- null means no limit
  max_concurrent_tasks     integer not null default 10 check (max_concurrent_tasks between 1 and 1000),
  max_schedules            integer not null default 25 check (max_schedules between 0 and 10000),
  retention_days           integer not null default 90 check (retention_days between 1 and 3650),
  display_settings         jsonb not null default '{}' check (jsonb_typeof(display_settings) = 'object'),
  created_at               timestamptz not null default now(),
  updated_at               timestamptz not null default now(),
  constraint tiers_not_empty      check (cardinality(allowed_model_tiers) > 0),
  constraint default_tier_allowed check (default_model_tier = any (allowed_model_tiers))
);

-- Every new org gets default settings.
create function app.create_default_org_settings() returns trigger
language plpgsql security definer set search_path = app, pg_temp as $$
begin
  insert into app.org_settings (org_id) values (new.id);
  return null;
end $$;

create trigger orgs_default_settings
  after insert on app.orgs
  for each row execute function app.create_default_org_settings();

-- Global people. A user exists before signing up once invited, so Slack email
-- matching works for invited users; clerk_user_id is linked at signup.
create table app.users (
  id             uuid primary key default app.uuid_v7(),
  clerk_user_id  text unique,
  email          text not null check (email ~ '^[^@\s]+@[^@\s]+$'),
  name           text,
  status         text not null default 'active' check (status in ('active', 'disabled')),
  created_at     timestamptz not null default now(),
  updated_at     timestamptz not null default now()
);
create unique index users_email_key on app.users (lower(email));

create table app.org_memberships (
  id          uuid primary key default app.uuid_v7(),
  org_id      uuid not null references app.orgs (id) on delete cascade,
  user_id     uuid not null references app.users (id) on delete cascade,
  privilege   text not null default 'user' check (privilege in ('admin', 'user')),
  status      text not null default 'invited' check (status in ('invited', 'active', 'removed')),
  invited_by  uuid,
  joined_at   timestamptz,
  removed_at  timestamptz,
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now(),
  unique (org_id, user_id),
  unique (org_id, id),
  foreign key (org_id, invited_by) references app.org_memberships (org_id, id) on delete set null (invited_by),
  constraint active_has_joined check (status <> 'active' or joined_at is not null),
  constraint removed_has_time  check ((status = 'removed') = (removed_at is not null))
);
create index org_memberships_user on app.org_memberships (user_id);

create function app.membership_status_times() returns trigger
language plpgsql as $$
begin
  if new.status = 'active' and new.joined_at is null then
    new.joined_at := now();
  end if;
  if new.status = 'removed' then
    if tg_op = 'INSERT' or old.status <> 'removed' then
      new.removed_at := now();
    end if;
  else
    new.removed_at := null;
  end if;
  return new;
end $$;

create trigger org_memberships_status_times
  before insert or update on app.org_memberships
  for each row execute function app.membership_status_times();

-- An org must always keep at least one active admin. The org row lock
-- serializes concurrent demotions so two admins can't remove each other.
create function app.guard_last_admin() returns trigger
language plpgsql as $$
begin
  if old.privilege = 'admin' and old.status = 'active'
     and (tg_op = 'DELETE' or new.privilege <> 'admin' or new.status <> 'active') then
    perform 1 from app.orgs where id = old.org_id for update;
    if found and not exists (
         select 1 from app.org_memberships m
          where m.org_id = old.org_id and m.id <> old.id
            and m.privilege = 'admin' and m.status = 'active') then
      raise exception 'org % must keep at least one active admin', old.org_id
        using errcode = 'check_violation';
    end if;
  end if;
  return case when tg_op = 'DELETE' then old else new end;
end $$;

create trigger org_memberships_guard_last_admin
  before update or delete on app.org_memberships
  for each row execute function app.guard_last_admin();

create table app.custom_roles (
  id           uuid primary key default app.uuid_v7(),
  org_id       uuid not null references app.orgs (id) on delete cascade,
  title        text not null check (length(btrim(title)) between 1 and 80),
  description  text not null default '' check (length(description) <= 4000),
  status       text not null default 'active' check (status in ('active', 'archived')),
  created_at   timestamptz not null default now(),
  updated_at   timestamptz not null default now(),
  unique (org_id, id)
);
create unique index custom_roles_title_key on app.custom_roles (org_id, lower(title)) where status = 'active';

-- A user can hold several roles; exactly one is primary whenever any exist.
create table app.membership_roles (
  org_id         uuid not null references app.orgs (id) on delete cascade,
  membership_id  uuid not null,
  role_id        uuid not null,
  is_primary     boolean not null default false,
  created_at     timestamptz not null default now(),
  primary key (membership_id, role_id),
  foreign key (org_id, membership_id) references app.org_memberships (org_id, id) on delete cascade,
  foreign key (org_id, role_id)       references app.custom_roles (org_id, id)    on delete cascade
);
create unique index membership_roles_one_primary on app.membership_roles (membership_id) where is_primary;
create index membership_roles_role on app.membership_roles (role_id);

-- When the primary role goes away, the oldest remaining role becomes primary.
create function app.promote_primary_role() returns trigger
language plpgsql as $$
begin
  if old.is_primary then
    update app.membership_roles mr
       set is_primary = true
     where (mr.membership_id, mr.role_id) = (
             select x.membership_id, x.role_id
               from app.membership_roles x
              where x.membership_id = old.membership_id
              order by x.created_at, x.role_id
              limit 1);
  end if;
  return null;
end $$;

create trigger membership_roles_promote
  after delete on app.membership_roles
  for each row execute function app.promote_primary_role();

-- Checked at commit, so switching the primary role within a transaction works.
create function app.check_primary_role() returns trigger
language plpgsql as $$
declare
  v_membership uuid := coalesce(new.membership_id, old.membership_id);
begin
  if exists (select 1 from app.membership_roles where membership_id = v_membership)
     and not exists (select 1 from app.membership_roles where membership_id = v_membership and is_primary) then
    raise exception 'membership % has roles but no primary role', v_membership
      using errcode = 'check_violation';
  end if;
  return null;
end $$;

create constraint trigger membership_roles_primary_check
  after insert or update or delete on app.membership_roles
  deferrable initially deferred
  for each row execute function app.check_primary_role();

/* ======================================================= 2. PLATFORM LAYER */

-- One workspace per org at launch is an app rule; the schema allows many.
create table app.workspaces (
  id             uuid primary key default app.uuid_v7(),
  org_id         uuid not null references app.orgs (id) on delete cascade,
  platform       text not null default 'slack' check (platform in ('slack')),
  external_id    text not null,
  enterprise_id  text,
  name           text,
  bot_user_id    text,
  installed_by   uuid,
  status         text not null default 'active' check (status in ('active', 'uninstalled', 'suspended')),
  installed_at   timestamptz not null default now(),
  created_at     timestamptz not null default now(),
  updated_at     timestamptz not null default now(),
  unique (org_id, id),
  foreign key (org_id, installed_by) references app.org_memberships (org_id, id) on delete set null (installed_by)
);
create unique index workspaces_active_external on app.workspaces (platform, external_id) where status = 'active';

-- Slack user to membership, stored at first email match so later email changes don't break it.
create table app.platform_identities (
  id                uuid primary key default app.uuid_v7(),
  org_id            uuid not null references app.orgs (id) on delete cascade,
  membership_id     uuid not null,
  workspace_id      uuid not null,
  external_user_id  text not null,
  matched_email     text not null,
  matched_at        timestamptz not null default now(),
  created_at        timestamptz not null default now(),
  updated_at        timestamptz not null default now(),
  unique (workspace_id, external_user_id),
  unique (membership_id, workspace_id),
  unique (org_id, id),
  foreign key (org_id, membership_id) references app.org_memberships (org_id, id) on delete cascade,
  foreign key (org_id, workspace_id)  references app.workspaces (org_id, id)      on delete cascade
);

create table app.access_requests (
  id                uuid primary key default app.uuid_v7(),
  org_id            uuid not null references app.orgs (id) on delete cascade,
  workspace_id      uuid not null,
  external_user_id  text not null,
  email             text,
  status            text not null default 'pending' check (status in ('pending', 'approved', 'dismissed')),
  resolved_by       uuid,
  resolved_at       timestamptz,
  created_at        timestamptz not null default now(),
  updated_at        timestamptz not null default now(),
  constraint resolved_has_time check ((status = 'pending') = (resolved_at is null)),
  foreign key (org_id, workspace_id) references app.workspaces (org_id, id)      on delete cascade,
  foreign key (org_id, resolved_by)  references app.org_memberships (org_id, id) on delete set null (resolved_by)
);
create unique index access_requests_one_pending on app.access_requests (workspace_id, external_user_id) where status = 'pending';

create table app.channels (
  id              uuid primary key default app.uuid_v7(),
  org_id          uuid not null references app.orgs (id) on delete cascade,
  workspace_id    uuid not null,
  external_id     text not null,
  name            text,
  is_private      boolean not null default false,
  is_allowlisted  boolean not null default false,
  bot_is_member   boolean not null default false,
  archived        boolean not null default false,
  created_at      timestamptz not null default now(),
  updated_at      timestamptz not null default now(),
  unique (workspace_id, external_id),
  unique (org_id, id),
  foreign key (org_id, workspace_id) references app.workspaces (org_id, id) on delete cascade
);

-- Durable copy of channel membership. Redis serves the hot path; a periodic
-- job reconciles both against Slack because events can be missed.
create table app.channel_members (
  org_id            uuid not null references app.orgs (id) on delete cascade,
  channel_id        uuid not null,
  external_user_id  text not null,
  synced_at         timestamptz not null default now(),
  primary key (channel_id, external_user_id),
  foreign key (org_id, channel_id) references app.channels (org_id, id) on delete cascade
);
create index channel_members_user on app.channel_members (org_id, external_user_id);

/* ========================================== 3. INTEGRATIONS AND CREDENTIALS */

-- The integration catalog and per operation metadata (read or write,
-- reversible, default sensitivity) live in code. This table holds what an org turned on.
create table app.org_integrations (
  id               uuid primary key default app.uuid_v7(),
  org_id           uuid not null references app.orgs (id) on delete cascade,
  integration_key  app.slug not null,
  kind             text not null check (kind in ('native', 'vendor_mcp', 'custom_mcp')),
  enabled          boolean not null default false,
  config           jsonb not null default '{}' check (jsonb_typeof(config) = 'object'),
  health           text not null default 'unknown' check (health in ('unknown', 'ok', 'degraded', 'broken')),
  approved_by      uuid,
  approved_at      timestamptz,
  created_at       timestamptz not null default now(),
  updated_at       timestamptz not null default now(),
  unique (org_id, integration_key),
  unique (org_id, id),
  constraint custom_mcp_needs_approval check (kind <> 'custom_mcp' or not enabled or approved_at is not null),
  foreign key (org_id, approved_by) references app.org_memberships (org_id, id) on delete set null (approved_by)
);

create table app.connections (
  id                   uuid primary key default app.uuid_v7(),
  org_id               uuid not null references app.orgs (id) on delete cascade,
  integration_key      app.slug not null,
  scope                text not null check (scope in ('org', 'user')),
  membership_id        uuid,
  external_account_id  text,
  granted_scopes       text[] not null default '{}',
  status               text not null default 'active' check (status in ('active', 'expired', 'revoked')),
  expires_at           timestamptz,
  last_refreshed_at    timestamptz,
  created_at           timestamptz not null default now(),
  updated_at           timestamptz not null default now(),
  unique (org_id, id),
  constraint scope_matches_owner check ((scope = 'org') = (membership_id is null)),
  foreign key (org_id, integration_key) references app.org_integrations (org_id, integration_key) on delete cascade,
  foreign key (org_id, membership_id)   references app.org_memberships (org_id, id)               on delete cascade
);
create unique index connections_one_active_org  on app.connections (org_id, integration_key)
  where scope = 'org' and status = 'active';
create unique index connections_one_active_user on app.connections (org_id, integration_key, membership_id)
  where scope = 'user' and status = 'active';

-- Only app_vault has privileges here. Ciphertext is envelope encrypted with a per tenant KMS key.
create table app.connection_secrets (
  connection_id  uuid primary key,
  org_id         uuid not null references app.orgs (id) on delete cascade,
  ciphertext     bytea not null,
  kms_key_id     text not null,
  encrypted_at   timestamptz not null default now(),
  foreign key (org_id, connection_id) references app.connections (org_id, id) on delete cascade
);

-- Org overrides on top of the defaults defined in code.
create table app.operation_policies (
  org_id           uuid not null references app.orgs (id) on delete cascade,
  integration_key  app.slug not null,
  operation        app.slug not null,
  credential_mode  text not null check (credential_mode in ('org', 'org_with_check', 'user_required')),
  enabled          boolean not null default true,
  updated_by       uuid,
  created_at       timestamptz not null default now(),
  updated_at       timestamptz not null default now(),
  primary key (org_id, integration_key, operation),
  foreign key (org_id, integration_key) references app.org_integrations (org_id, integration_key) on delete cascade,
  foreign key (org_id, updated_by)      references app.org_memberships (org_id, id)               on delete set null (updated_by)
);

/* ================================================ 4. CODE AND SERVICE CATALOG */

create table app.repositories (
  id                  uuid primary key default app.uuid_v7(),
  org_id              uuid not null references app.orgs (id) on delete cascade,
  provider            text not null default 'github' check (provider in ('github')),
  external_id         text not null,
  full_name           text not null,
  default_branch      text not null default 'main',
  card                jsonb not null default '{}' check (jsonb_typeof(card) = 'object'),
  setup_recipe        jsonb not null default '{}' check (jsonb_typeof(setup_recipe) = 'object'),
  snapshot_image_ref  text,
  protected_paths     text[] not null default '{}',
  status              text not null default 'active' check (status in ('active', 'archived', 'disconnected')),
  last_indexed_at     timestamptz,
  created_at          timestamptz not null default now(),
  updated_at          timestamptz not null default now(),
  unique (org_id, provider, external_id),
  unique (org_id, id)
);

create table app.services (
  id             uuid primary key default app.uuid_v7(),
  org_id         uuid not null references app.orgs (id) on delete cascade,
  name           text not null check (length(btrim(name)) between 1 and 120),
  description    text,
  repository_id  uuid,
  log_source     jsonb not null default '{}' check (jsonb_typeof(log_source) = 'object'),
  status         text not null default 'active' check (status in ('active', 'archived')),
  created_at     timestamptz not null default now(),
  updated_at     timestamptz not null default now(),
  unique (org_id, id),
  foreign key (org_id, repository_id) references app.repositories (org_id, id) on delete set null (repository_id)
);
create unique index services_name_key on app.services (org_id, lower(name)) where status = 'active';

create table app.service_owners (
  org_id         uuid not null references app.orgs (id) on delete cascade,
  service_id     uuid not null,
  membership_id  uuid not null,
  primary key (service_id, membership_id),
  foreign key (org_id, service_id)    references app.services (org_id, id)        on delete cascade,
  foreign key (org_id, membership_id) references app.org_memberships (org_id, id) on delete cascade
);

create table app.service_channels (
  org_id      uuid not null references app.orgs (id) on delete cascade,
  service_id  uuid not null,
  channel_id  uuid not null,
  primary key (service_id, channel_id),
  foreign key (org_id, service_id) references app.services (org_id, id) on delete cascade,
  foreign key (org_id, channel_id) references app.channels (org_id, id) on delete cascade
);

/* ============================================== 5. AUTONOMY AND PREFERENCES */

-- Admin ceiling per scope. Most specific wins: repo over channel over org.
create table app.autonomy_policies (
  id             uuid primary key default app.uuid_v7(),
  org_id         uuid not null references app.orgs (id) on delete cascade,
  scope_type     text not null check (scope_type in ('org', 'channel', 'repo')),
  channel_id     uuid,
  repository_id  uuid,
  max_mode       app.autonomy_mode not null,
  updated_by     uuid,
  created_at     timestamptz not null default now(),
  updated_at     timestamptz not null default now(),
  constraint scope_target check (
       (scope_type = 'org'     and channel_id is null     and repository_id is null)
    or (scope_type = 'channel' and channel_id is not null and repository_id is null)
    or (scope_type = 'repo'    and channel_id is null     and repository_id is not null)),
  foreign key (org_id, channel_id)    references app.channels (org_id, id)        on delete cascade,
  foreign key (org_id, repository_id) references app.repositories (org_id, id)    on delete cascade,
  foreign key (org_id, updated_by)    references app.org_memberships (org_id, id) on delete set null (updated_by)
);
create unique index autonomy_policies_org     on app.autonomy_policies (org_id)                where scope_type = 'org';
create unique index autonomy_policies_channel on app.autonomy_policies (org_id, channel_id)    where scope_type = 'channel';
create unique index autonomy_policies_repo    on app.autonomy_policies (org_id, repository_id) where scope_type = 'repo';

create table app.user_preferences (
  membership_id  uuid primary key,
  org_id         uuid not null references app.orgs (id) on delete cascade,
  mode           app.autonomy_mode not null default 'ask',
  timezone       text not null default 'UTC',
  notify_on      text[] not null default '{task_done,approval_needed}'
                 check (notify_on <@ '{task_done,task_failed,approval_needed,schedule_paused}'::text[]),
  created_at     timestamptz not null default now(),
  updated_at     timestamptz not null default now(),
  foreign key (org_id, membership_id) references app.org_memberships (org_id, id) on delete cascade
);

create trigger user_preferences_timezone
  before insert or update of timezone on app.user_preferences
  for each row execute function app.validate_timezone();

/* ================================================================ 6. PLAYBOOKS */

-- org_id null means built in (shipped by you, managed by app_maintenance).
-- Versions are immutable: app_user can only toggle enabled on an existing row.
create table app.playbooks (
  id            uuid primary key default app.uuid_v7(),
  org_id        uuid references app.orgs (id) on delete cascade,
  key           app.slug not null,
  version       integer not null check (version > 0),
  title         text not null check (length(btrim(title)) between 1 and 120),
  description   text not null default '',
  instructions  text not null check (length(instructions) between 1 and 100000),
  enabled       boolean not null default true,
  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now(),
  constraint playbooks_version_key unique nulls not distinct (org_id, key, version)
);

/* ================================================= 7. SESSIONS, TASKS AND WORK */

-- One session per thread or DM conversation.
create table app.sessions (
  id                   uuid primary key default app.uuid_v7(),
  org_id               uuid not null references app.orgs (id) on delete cascade,
  workspace_id         uuid not null,
  channel_id           uuid,                 -- null for DMs and untracked channels
  external_channel_id  text not null,
  external_thread_id   text not null,
  started_by           uuid,
  status               text not null default 'active' check (status in ('active', 'closed')),
  last_activity_at     timestamptz not null default now(),
  created_at           timestamptz not null default now(),
  updated_at           timestamptz not null default now(),
  unique (workspace_id, external_channel_id, external_thread_id),
  unique (org_id, id),
  foreign key (org_id, workspace_id) references app.workspaces (org_id, id)      on delete cascade,
  foreign key (org_id, channel_id)   references app.channels (org_id, id)        on delete set null (channel_id),
  foreign key (org_id, started_by)   references app.org_memberships (org_id, id) on delete set null (started_by)
);

create table app.schedules (
  id                    uuid primary key default app.uuid_v7(),
  org_id                uuid not null references app.orgs (id) on delete cascade,
  owner_membership_id   uuid not null,
  name                  text not null check (length(btrim(name)) between 1 and 120),
  instruction           text not null check (length(instruction) between 1 and 8000),
  playbook_key          app.slug,
  cron                  text not null check (cron ~ '^\s*\S+(\s+\S+){4}\s*$'),
  timezone              text not null default 'UTC',
  delivery_type         text not null check (delivery_type in ('channel', 'dm')),
  delivery_channel_id   uuid,
  status                text not null default 'active' check (status in ('active', 'paused', 'deleted')),
  pause_reason          text check (pause_reason in ('owner_removed', 'access_lost', 'admin', 'budget')),
  temporal_schedule_id  text unique,
  last_run_at           timestamptz,
  created_at            timestamptz not null default now(),
  updated_at            timestamptz not null default now(),
  unique (org_id, id),
  constraint delivery_target  check ((delivery_type = 'channel') = (delivery_channel_id is not null)),
  constraint pause_has_reason check ((status = 'paused') = (pause_reason is not null)),
  foreign key (org_id, owner_membership_id) references app.org_memberships (org_id, id) on delete cascade,
  foreign key (org_id, delivery_channel_id) references app.channels (org_id, id)        on delete cascade
);
create index schedules_owner on app.schedules (org_id, owner_membership_id);

create trigger schedules_timezone
  before insert or update of timezone on app.schedules
  for each row execute function app.validate_timezone();

-- Per org cap on active schedules. Locking the settings row serializes creation per org.
create function app.enforce_schedule_quota() returns trigger
language plpgsql as $$
declare
  v_max     integer;
  v_active  integer;
begin
  if new.status = 'active' and (tg_op = 'INSERT' or old.status <> 'active') then
    select max_schedules into v_max from app.org_settings where org_id = new.org_id for update;
    select count(*) into v_active from app.schedules
     where org_id = new.org_id and status = 'active' and id <> new.id;
    if v_active >= v_max then
      raise exception 'org % has reached its limit of % active schedules', new.org_id, v_max
        using errcode = 'check_violation';
    end if;
  end if;
  return new;
end $$;

create trigger schedules_quota
  before insert or update of status on app.schedules
  for each row execute function app.enforce_schedule_quota();

-- A channel leaving the allowlist (or being archived) pauses schedules that deliver there.
create function app.on_channel_access_change() returns trigger
language plpgsql as $$
begin
  if (old.is_allowlisted and not new.is_allowlisted) or (not old.archived and new.archived) then
    update app.schedules
       set status = 'paused', pause_reason = 'access_lost'
     where org_id = new.org_id and delivery_channel_id = new.id and status = 'active';
  end if;
  return null;
end $$;

create trigger channels_access_change
  after update of is_allowlisted, archived on app.channels
  for each row execute function app.on_channel_access_change();

-- One task per request in a session, or per scheduled run. Temporal is the
-- source of truth for execution; this is the product's view of it.
create table app.tasks (
  id              uuid primary key default app.uuid_v7(),
  org_id          uuid not null references app.orgs (id) on delete cascade,
  session_id      uuid,
  schedule_id     uuid,
  requested_by    uuid,
  trigger         text not null check (trigger in ('mention', 'dm', 'schedule', 'webhook')),
  playbook_id     uuid references app.playbooks (id) on delete set null,
  model_tier      app.model_tier not null,
  effective_mode  app.autonomy_mode not null default 'ask',
  status          text not null default 'queued'
                  check (status in ('queued', 'running', 'awaiting_approval', 'completed', 'failed', 'cancelled')),
  workflow_id     text unique,
  token_budget    bigint check (token_budget > 0),
  tokens_used     bigint not null default 0 check (tokens_used >= 0),
  cost            numeric(14, 6) not null default 0 check (cost >= 0),
  started_at      timestamptz,
  finished_at     timestamptz,
  error           text,
  created_at      timestamptz not null default now(),
  updated_at      timestamptz not null default now(),
  unique (org_id, id),
  constraint terminal_has_finish check (status not in ('completed', 'failed', 'cancelled') or finished_at is not null),
  constraint schedule_trigger    check (schedule_id is null or trigger = 'schedule'),
  foreign key (org_id, session_id)   references app.sessions (org_id, id)        on delete set null (session_id),
  foreign key (org_id, schedule_id)  references app.schedules (org_id, id)       on delete set null (schedule_id),
  foreign key (org_id, requested_by) references app.org_memberships (org_id, id) on delete set null (requested_by)
);
-- The per thread lock: at most one running task per session.
create unique index tasks_one_running_per_session on app.tasks (session_id) where status = 'running';
create index tasks_org_status on app.tasks (org_id, status);
create index tasks_requester  on app.tasks (org_id, requested_by, created_at desc);
create index tasks_schedule   on app.tasks (schedule_id, created_at desc) where schedule_id is not null;
create index tasks_session    on app.tasks (session_id, created_at) where session_id is not null;

create table app.task_steps (
  id           uuid primary key default app.uuid_v7(),
  org_id       uuid not null references app.orgs (id) on delete cascade,
  task_id      uuid not null,
  seq          integer not null check (seq > 0),
  kind         text not null check (kind in ('gather', 'plan', 'act', 'verify', 'report')),
  status       text not null default 'pending' check (status in ('pending', 'running', 'completed', 'failed', 'skipped')),
  summary      text,
  started_at   timestamptz,
  finished_at  timestamptz,
  created_at   timestamptz not null default now(),
  updated_at   timestamptz not null default now(),
  unique (task_id, seq),
  unique (org_id, id),
  foreign key (org_id, task_id) references app.tasks (org_id, id) on delete cascade
);

create table app.approvals (
  id              uuid primary key default app.uuid_v7(),
  org_id          uuid not null references app.orgs (id) on delete cascade,
  task_id         uuid not null,
  step_id         uuid,
  action_summary  text not null check (length(action_summary) between 1 and 2000),
  action_tier     text not null check (action_tier in ('reversible_write', 'irreversible')),
  approver_rule   text not null default 'requester_or_admin'
                  check (approver_rule in ('requester', 'admin', 'requester_or_admin')),
  status          text not null default 'pending' check (status in ('pending', 'approved', 'rejected', 'expired')),
  decided_by      uuid,
  decided_at      timestamptz,
  expires_at      timestamptz not null,
  message_ref     text,
  created_at      timestamptz not null default now(),
  updated_at      timestamptz not null default now(),
  unique (org_id, id),
  constraint decided_has_time      check ((status in ('approved', 'rejected')) = (decided_at is not null)),
  constraint expiry_after_creation check (expires_at > created_at),
  foreign key (org_id, task_id)    references app.tasks (org_id, id)           on delete cascade,
  foreign key (org_id, step_id)    references app.task_steps (org_id, id)      on delete set null (step_id),
  foreign key (org_id, decided_by) references app.org_memberships (org_id, id) on delete set null (decided_by)
);
create index approvals_task    on app.approvals (task_id);
create index approvals_pending on app.approvals (expires_at) where status = 'pending';

-- Only the requester or an active admin (per approver_rule) can decide, only
-- while pending and before expiry. Decisions are final.
create function app.check_approval_decision() returns trigger
language plpgsql as $$
declare
  v_requester         uuid;
  v_decider_status    text;
  v_decider_privilege text;
begin
  if old.status <> 'pending' then
    if new.status is distinct from old.status
       or new.decided_at is distinct from old.decided_at
       or (new.decided_by is distinct from old.decided_by and new.decided_by is not null) then
      raise exception 'approval % is already %', old.id, old.status using errcode = 'check_violation';
    end if;
    return new;
  end if;

  if new.status in ('approved', 'rejected') then
    if now() >= old.expires_at then
      raise exception 'approval % has expired', old.id using errcode = 'check_violation';
    end if;
    if new.decided_by is null then
      raise exception 'approval % needs decided_by', old.id using errcode = 'check_violation';
    end if;

    select t.requested_by into v_requester from app.tasks t where t.id = old.task_id;
    select m.status, m.privilege into v_decider_status, v_decider_privilege
      from app.org_memberships m where m.id = new.decided_by and m.org_id = old.org_id;

    if coalesce(v_decider_status, 'removed') = 'removed' or not (
         (old.approver_rule in ('requester', 'requester_or_admin') and new.decided_by = v_requester)
      or (old.approver_rule in ('admin', 'requester_or_admin')
          and v_decider_privilege = 'admin' and v_decider_status = 'active')) then
      raise exception 'member % may not decide approval %', new.decided_by, old.id
        using errcode = 'insufficient_privilege';
    end if;

    new.decided_at := coalesce(new.decided_at, now());
  end if;
  return new;
end $$;

create trigger approvals_decision
  before update on app.approvals
  for each row execute function app.check_approval_decision();

-- Every write the agent performs. The idempotency key is checked before
-- acting, which makes retries after a crash safe.
create table app.external_actions (
  id               uuid primary key default app.uuid_v7(),
  org_id           uuid not null references app.orgs (id) on delete cascade,
  task_id          uuid not null,
  step_id          uuid,
  integration_key  app.slug not null,
  operation        app.slug not null,
  target_ref       text,
  connection_id    uuid,
  idempotency_key  text not null check (length(idempotency_key) between 8 and 200),
  status           text not null default 'pending' check (status in ('pending', 'succeeded', 'failed')),
  error            text,
  performed_at     timestamptz,
  created_at       timestamptz not null default now(),
  updated_at       timestamptz not null default now(),
  unique (org_id, idempotency_key),
  foreign key (org_id, task_id)       references app.tasks (org_id, id)       on delete cascade,
  foreign key (org_id, step_id)       references app.task_steps (org_id, id)  on delete set null (step_id),
  foreign key (org_id, connection_id) references app.connections (org_id, id) on delete set null (connection_id)
);
create index external_actions_task on app.external_actions (task_id);

-- Derived content and permalinks only, never raw Slack messages.
-- Small bodies inline as jsonb; anything larger goes to object storage.
create table app.artifacts (
  id            uuid primary key default app.uuid_v7(),
  org_id        uuid not null references app.orgs (id) on delete cascade,
  task_id       uuid not null,
  kind          text not null check (kind in ('brief', 'plan', 'report', 'diff', 'summary')),
  content       jsonb,
  storage_ref   text,
  source_refs   jsonb not null default '[]' check (jsonb_typeof(source_refs) = 'array'),
  retain_until  timestamptz not null,
  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now(),
  constraint one_body    check (num_nonnulls(content, storage_ref) = 1),
  constraint inline_size check (content is null or pg_column_size(content) <= 262144),
  foreign key (org_id, task_id) references app.tasks (org_id, id) on delete cascade
);
create index artifacts_task      on app.artifacts (task_id);
create index artifacts_retention on app.artifacts (retain_until);

-- Defaults retain_until from the org's retention setting.
create function app.default_artifact_retention() returns trigger
language plpgsql as $$
begin
  if new.retain_until is null then
    select now() + make_interval(days => s.retention_days)
      into new.retain_until
      from app.org_settings s
     where s.org_id = new.org_id;
  end if;
  return new;
end $$;

create trigger artifacts_retention_default
  before insert on app.artifacts
  for each row execute function app.default_artifact_retention();

-- Lets incoming webhooks (PR review comments, Jira updates) find their task.
create table app.task_links (
  org_id       uuid not null references app.orgs (id) on delete cascade,
  task_id      uuid not null,
  kind         text not null check (kind in ('pull_request', 'jira_issue')),
  external_id  text not null,
  url          text,
  created_at   timestamptz not null default now(),
  primary key (task_id, kind, external_id),
  foreign key (org_id, task_id) references app.tasks (org_id, id) on delete cascade
);
create index task_links_lookup on app.task_links (kind, external_id);

/* ======================================================== 8. USAGE AND AUDIT */

-- Append only, partitioned by month. No foreign keys to tasks or memberships
-- so the log outlives them; org deletion still cascades.
create table app.usage_events (
  id               uuid not null default app.uuid_v7(),
  org_id           uuid not null references app.orgs (id) on delete cascade,
  membership_id    uuid,
  task_id          uuid,
  kind             text not null check (kind in ('llm', 'sandbox')),
  model            text,
  input_tokens     bigint not null default 0 check (input_tokens >= 0),
  output_tokens    bigint not null default 0 check (output_tokens >= 0),
  cached_tokens    bigint not null default 0 check (cached_tokens >= 0),
  sandbox_seconds  numeric(12, 3) not null default 0 check (sandbox_seconds >= 0),
  cost             numeric(14, 6) not null default 0 check (cost >= 0),
  occurred_at      timestamptz not null default now(),
  primary key (id, occurred_at)
) partition by range (occurred_at);
create index usage_events_org_time on app.usage_events (org_id, occurred_at);

-- Rollup the LLM gateway reads for limit checks (UTC days).
create table app.usage_daily (
  org_id         uuid not null references app.orgs (id) on delete cascade,
  membership_id  uuid,
  day            date not null,
  tokens         bigint not null default 0,
  cost           numeric(14, 6) not null default 0,
  constraint usage_daily_key unique nulls not distinct (org_id, membership_id, day)
);

create function app.rollup_usage() returns trigger
language plpgsql as $$
begin
  insert into app.usage_daily as d (org_id, membership_id, day, tokens, cost)
  values (new.org_id, new.membership_id, (new.occurred_at at time zone 'UTC')::date,
          new.input_tokens + new.output_tokens, new.cost)
  on conflict (org_id, membership_id, day) do update
     set tokens = d.tokens + excluded.tokens,
         cost   = d.cost + excluded.cost;
  return null;
end $$;

create trigger usage_events_rollup
  after insert on app.usage_events
  for each row execute function app.rollup_usage();

create table app.audit_log (
  id           uuid not null default app.uuid_v7(),
  org_id       uuid not null references app.orgs (id) on delete cascade,
  actor_type   text not null check (actor_type in ('user', 'agent', 'system')),
  actor_id     uuid,
  task_id      uuid,
  action       text not null,
  target       text,
  decision     text not null default 'info' check (decision in ('allowed', 'denied', 'info')),
  reason       text,
  metadata     jsonb not null default '{}' check (jsonb_typeof(metadata) = 'object'),
  occurred_at  timestamptz not null default now(),
  primary key (id, occurred_at)
) partition by range (occurred_at);
create index audit_log_org_time on app.audit_log (org_id, occurred_at desc);

-- Catch all partitions so an insert never fails if the monthly job falls behind.
create table app.usage_events_default partition of app.usage_events default;
create table app.audit_log_default    partition of app.audit_log default;

-- Creates monthly partitions (UTC) from this month through p_months_ahead.
-- Run daily from app_maintenance. Partitions get no grants of their own,
-- so app_user can only reach rows through the parent and its RLS policy.
create function app.ensure_monthly_partitions(p_months_ahead integer default 3)
returns integer
language plpgsql security definer set search_path = app, pg_temp as $$
declare
  v_parent   text;
  v_month    date;
  v_name     text;
  v_created  integer := 0;
begin
  foreach v_parent in array array['usage_events', 'audit_log'] loop
    for i in 0 .. p_months_ahead loop
      v_month := (date_trunc('month', now() at time zone 'UTC') + make_interval(months => i))::date;
      v_name  := format('%s_%s', v_parent, to_char(v_month, 'YYYY_MM'));
      if to_regclass('app.' || v_name) is null then
        execute format('create table app.%I partition of app.%I for values from (%L) to (%L)',
                       v_name, v_parent,
                       v_month::timestamp at time zone 'UTC',
                       (v_month + interval '1 month')::timestamp at time zone 'UTC');
        v_created := v_created + 1;
      end if;
    end loop;
  end loop;
  return v_created;
end $$;

/* =========================================== 9. MEMBERSHIP REMOVAL CASCADE */

-- When someone is removed: revoke their linked accounts, pause schedules they
-- own, close their sessions. Running tasks are cancelled by the app through
-- Temporal, and provider tokens are revoked by the vault service.
create function app.on_membership_removed() returns trigger
language plpgsql security definer set search_path = app, pg_temp as $$
begin
  if new.status = 'removed' and old.status <> 'removed' then
    update app.connections
       set status = 'revoked'
     where org_id = new.org_id and membership_id = new.id and status = 'active';

    update app.schedules
       set status = 'paused', pause_reason = 'owner_removed'
     where org_id = new.org_id and owner_membership_id = new.id and status = 'active';

    update app.sessions
       set status = 'closed'
     where org_id = new.org_id and started_by = new.id and status = 'active';
  end if;
  return null;
end $$;

create trigger org_memberships_removed
  after update of status on app.org_memberships
  for each row execute function app.on_membership_removed();

/* ============================================================ 10. RESOLVERS */

-- Ingress routing: Slack event (team id) to org, before any tenant is known.
create function app.resolve_workspace(p_platform text, p_external_id text)
returns table (org_id uuid, workspace_id uuid)
language sql stable security definer set search_path = app, pg_temp as $$
  select w.org_id, w.id
    from app.workspaces w
    join app.orgs o on o.id = w.org_id
   where w.platform = p_platform
     and w.external_id = p_external_id
     and w.status = 'active'
     and o.status = 'active'
$$;

-- Webhook routing: PR or Jira event to the task (and thread) it belongs to.
create function app.resolve_task_links(p_kind text, p_external_id text)
returns table (org_id uuid, task_id uuid, session_id uuid)
language sql stable security definer set search_path = app, pg_temp as $$
  select l.org_id, l.task_id, t.session_id
    from app.task_links l
    join app.tasks t on t.id = l.task_id
   where l.kind = p_kind
     and l.external_id = p_external_id
$$;

-- The Ask or Auto mode a task runs in: the user's choice, capped by the most
-- specific admin ceiling (repo over channel over org). Defaults to ask.
-- Irreversible actions still need approval regardless of the result.
create function app.effective_mode(p_membership uuid, p_channel uuid default null, p_repository uuid default null)
returns app.autonomy_mode
language sql stable as $$
  with pref as (
    select coalesce((select mode from app.user_preferences where membership_id = p_membership), 'ask') as mode
  ), ceiling as (
    select coalesce(
             (select max_mode from app.autonomy_policies where scope_type = 'repo'    and repository_id = p_repository),
             (select max_mode from app.autonomy_policies where scope_type = 'channel' and channel_id = p_channel),
             (select max_mode from app.autonomy_policies where scope_type = 'org'),
             'ask') as mode
  )
  select (case when pref.mode = 'auto' and ceiling.mode = 'auto' then 'auto' else 'ask' end)::app.autonomy_mode
    from pref, ceiling
$$;

-- The channel rule: allowlisted, not archived, and the requester is a member.
create function app.can_read_channel(p_membership uuid, p_channel uuid)
returns boolean
language sql stable as $$
  select exists (
    select 1
      from app.channels c
      join app.org_memberships m      on m.id = p_membership and m.org_id = c.org_id
                                     and m.status in ('active', 'invited')
      join app.platform_identities pi on pi.membership_id = m.id and pi.workspace_id = c.workspace_id
      join app.channel_members cm     on cm.channel_id = c.id and cm.external_user_id = pi.external_user_id
     where c.id = p_channel
       and c.is_allowlisted
       and not c.archived)
$$;

/* ================================================= 11. updated_at TRIGGERS */

do $$
declare t text;
begin
  for t in
    select c.table_name
      from information_schema.columns c
      join pg_class k on k.relname = c.table_name and k.relnamespace = 'app'::regnamespace
     where c.table_schema = 'app' and c.column_name = 'updated_at' and k.relkind = 'r'
  loop
    execute format('create trigger %I before update on app.%I for each row execute function app.set_updated_at()',
                   t || '_updated_at', t);
  end loop;
end $$;

/* ================================================== 12. ROW LEVEL SECURITY */

-- Standard tenant policy for every table keyed by org_id.
do $$
declare t text;
begin
  foreach t in array array[
    'org_settings', 'org_memberships', 'custom_roles', 'membership_roles',
    'workspaces', 'platform_identities', 'access_requests', 'channels', 'channel_members',
    'org_integrations', 'connections', 'connection_secrets', 'operation_policies',
    'repositories', 'services', 'service_owners', 'service_channels',
    'autonomy_policies', 'user_preferences',
    'sessions', 'schedules', 'task_steps', 'approvals', 'external_actions', 'artifacts', 'task_links',
    'usage_events', 'usage_daily', 'audit_log']
  loop
    execute format('alter table app.%I enable row level security', t);
    execute format('create policy tenant_isolation on app.%I
                      using (org_id = app.current_org_id())
                      with check (org_id = app.current_org_id())', t);
  end loop;
end $$;

alter table app.orgs enable row level security;
create policy tenant_isolation on app.orgs
  using (id = app.current_org_id())
  with check (id = app.current_org_id());

-- Users are global; the API sees only people who belong to the current org.
alter table app.users enable row level security;
create policy users_in_current_org on app.users for select
  using (exists (select 1 from app.org_memberships m
                  where m.user_id = users.id and m.org_id = app.current_org_id()));

-- Built in playbooks are readable by every org; org playbooks only by their org.
alter table app.playbooks enable row level security;
create policy playbooks_read   on app.playbooks for select using (org_id is null or org_id = app.current_org_id());
create policy playbooks_insert on app.playbooks for insert with check (org_id = app.current_org_id());
create policy playbooks_update on app.playbooks for update using (org_id = app.current_org_id())
                                                           with check (org_id = app.current_org_id());
create policy playbooks_delete on app.playbooks for delete using (org_id = app.current_org_id());

-- tasks.playbook_id is a plain foreign key (built ins have no org), so the
-- policy also requires the playbook to be visible to the current org.
alter table app.tasks enable row level security;
create policy tenant_isolation on app.tasks
  using (org_id = app.current_org_id())
  with check (org_id = app.current_org_id()
              and (tasks.playbook_id is null
                   or exists (select 1 from app.playbooks p where p.id = tasks.playbook_id)));

/* ================================================================ 13. GRANTS */

grant usage on schema app to app_user, app_vault, app_identity_sync, app_maintenance;

-- API and workers
grant select, insert, update, delete on
  app.custom_roles, app.membership_roles,
  app.workspaces, app.platform_identities, app.access_requests, app.channels, app.channel_members,
  app.org_integrations, app.connections, app.operation_policies,
  app.repositories, app.services, app.service_owners, app.service_channels,
  app.autonomy_policies, app.user_preferences,
  app.sessions, app.schedules, app.tasks, app.task_steps, app.approvals,
  app.external_actions, app.artifacts, app.task_links
to app_user;

grant select on app.orgs, app.users, app.org_memberships to app_user;   -- Clerk is the source of truth
grant update (name) on app.orgs to app_user;
grant select, update (default_model_tier, allowed_model_tiers, user_daily_token_limit, org_monthly_token_limit,
                      max_concurrent_tasks, max_schedules, retention_days, display_settings)
  on app.org_settings to app_user;
grant select, insert, delete on app.playbooks to app_user;
grant update (enabled) on app.playbooks to app_user;                      -- versions are immutable
grant select, insert on app.usage_events, app.audit_log to app_user;      -- append only
grant select, insert, update on app.usage_daily to app_user;

-- Token vault
grant select, insert, update, delete on app.connection_secrets to app_vault;
grant select on app.connections to app_vault;

-- Clerk webhook handler
grant select, insert, update on app.orgs, app.users, app.org_memberships to app_identity_sync;

-- Retention, offboarding, reconciliation
grant select, insert, update, delete on all tables in schema app to app_maintenance;

-- Function access
revoke all on function app.ensure_monthly_partitions(integer)      from public;
revoke all on function app.resolve_workspace(text, text)           from public;
revoke all on function app.resolve_task_links(text, text)          from public;
revoke all on function app.create_default_org_settings()           from public;
revoke all on function app.on_membership_removed()                 from public;
grant execute on function app.ensure_monthly_partitions(integer)   to app_maintenance;
grant execute on function app.resolve_workspace(text, text)        to app_user, app_maintenance;
grant execute on function app.resolve_task_links(text, text)       to app_user, app_maintenance;

select app.ensure_monthly_partitions(3);

-- (end of transaction)

/*
  DEPLOYMENT NOTES

  1. Login users. The roles above are NOLOGIN. Create login users per service:
        create user api_service login password '...' in role app_user;
        create user vault_service login password '...' in role app_vault;
     BYPASSRLS is a role attribute and is NOT inherited through membership.
     For app_identity_sync and app_maintenance, either log in as those roles
     directly (alter role app_maintenance login password '...') or have the
     login user run "set role app_maintenance" before work.

  2. Managed Postgres. Some providers restrict BYPASSRLS. If yours does, drop
     it from the two roles and route their work through SECURITY DEFINER
     functions owned by app_owner, the same way the resolvers work.

  3. Connection pooling. Always set app.org_id with set_config(..., true)
     inside a transaction. In PgBouncer transaction mode a session level
     setting would leak to the next client.

  4. Partitions. Run "select app.ensure_monthly_partitions(3)" daily as
     app_maintenance. Drop old monthly partitions per your audit retention
     policy; artifacts are pruned by retain_until.

  5. New tables in later migrations need explicit grants and, if tenant
     scoped, the same tenant_isolation policy. No default privileges are set
     on purpose, so nothing becomes reachable by accident.
*/
