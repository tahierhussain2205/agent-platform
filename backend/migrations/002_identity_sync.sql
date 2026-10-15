/*
  002: identity sync (Clerk webhooks and console mirroring) and console identity resolution.

  Clerk is the source of truth for orgs, users and memberships; these functions mirror it. They
  are SECURITY DEFINER functions owned by app_owner, so they work without BYPASSRLS on managed
  Postgres (decision D-034). Only app_identity_sync may execute the identity_* functions; the
  console resolvers are executable by app_user and return the privilege stored here, never the
  one claimed by a token.
*/

set local role app_owner;
set local search_path = app, pg_temp;

/* ---------------------------------------------------------------- helpers */

create function app.identity_audit(p_org uuid, p_action text, p_target text, p_metadata jsonb default '{}')
returns void
language sql security definer set search_path = app, pg_temp as $$
  insert into app.audit_log (org_id, actor_type, action, target, metadata)
  values (p_org, 'system', p_action, p_target, coalesce(p_metadata, '{}'::jsonb))
$$;

create function app.identity_privilege(p_clerk_role text) returns text
language sql immutable as $$
  select case p_clerk_role when 'org:admin' then 'admin' when 'admin' then 'admin' else 'user' end
$$;

/* ---------------------------------------------------------------- orgs and users */

create function app.identity_upsert_org(p_clerk_org_id text, p_name text)
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
    perform app.identity_audit(v_id, 'identity.org_created', p_clerk_org_id);
  end if;
  return v_id;
end $$;

-- Upsert by Clerk user id; otherwise link the user created at invite time (same email, no Clerk id).
create function app.identity_upsert_user(p_clerk_user_id text, p_email text, p_name text)
returns uuid
language plpgsql security definer set search_path = app, pg_temp as $$
declare
  v_id uuid;
begin
  if coalesce(p_clerk_user_id, '') = '' or coalesce(p_email, '') = '' then
    raise exception 'clerk user id and email are required' using errcode = 'check_violation';
  end if;

  select id into v_id from app.users where clerk_user_id = p_clerk_user_id;
  if found then
    begin
      update app.users set email = p_email, name = coalesce(nullif(btrim(p_name), ''), name) where id = v_id;
    exception when unique_violation then
      -- The new email belongs to another row; keep the old one rather than fail the sync.
      update app.users set name = coalesce(nullif(btrim(p_name), ''), name) where id = v_id;
    end;
    return v_id;
  end if;

  update app.users
     set clerk_user_id = p_clerk_user_id, name = coalesce(nullif(btrim(p_name), ''), name)
   where lower(email) = lower(p_email) and clerk_user_id is null
  returning id into v_id;
  if found then
    return v_id;
  end if;

  insert into app.users (clerk_user_id, email, name)
  values (p_clerk_user_id, p_email, nullif(btrim(p_name), ''))
  returning id into v_id;
  return v_id;
end $$;

/* ---------------------------------------------------------------- memberships */

-- Webhooks can arrive late or out of order (and the console mirrors its own changes before the
-- webhook for them arrives). Each membership remembers when its identity state last changed;
-- an event older than that is stale and ignored, so a late "invitation created" cannot undo a
-- revocation that already happened.
alter table app.org_memberships add column identity_event_at timestamptz;

create function app.identity_is_stale(p_org uuid, p_user uuid, p_event_at timestamptz)
returns boolean
language sql stable as $$
  select exists (select 1 from app.org_memberships
                  where org_id = p_org and user_id = p_user and identity_event_at > p_event_at)
$$;

create function app.identity_upsert_membership(
  p_clerk_org_id text, p_org_name text, p_clerk_user_id text, p_email text, p_name text, p_clerk_role text,
  p_event_at timestamptz default now())
returns uuid
language plpgsql security definer set search_path = app, pg_temp as $$
declare
  v_org uuid := app.identity_upsert_org(p_clerk_org_id, p_org_name);
  v_user uuid := app.identity_upsert_user(p_clerk_user_id, p_email, p_name);
  v_privilege text := app.identity_privilege(p_clerk_role);
  v_id uuid;
  v_before text;
begin
  if app.identity_is_stale(v_org, v_user, p_event_at) then
    select id into v_id from app.org_memberships where org_id = v_org and user_id = v_user;
    return v_id;
  end if;
  select status || ':' || privilege into v_before from app.org_memberships where org_id = v_org and user_id = v_user;
  insert into app.org_memberships (org_id, user_id, privilege, status, identity_event_at)
  values (v_org, v_user, v_privilege, 'active', p_event_at)
  on conflict (org_id, user_id) do update
     set privilege = excluded.privilege, status = 'active', identity_event_at = excluded.identity_event_at
  returning id into v_id;
  if v_before is distinct from 'active:' || v_privilege then
    perform app.identity_audit(v_org, 'identity.membership_active', p_email,
                               jsonb_build_object('privilege', v_privilege, 'before', v_before));
  end if;
  return v_id;
end $$;

-- An invitation: the person exists from now on (Slack email matching works before signup).
create function app.identity_invite(
  p_clerk_org_id text, p_email text, p_clerk_role text, p_invited_by_clerk_user_id text,
  p_event_at timestamptz default now())
returns uuid
language plpgsql security definer set search_path = app, pg_temp as $$
declare
  v_org uuid;
  v_user uuid;
  v_inviter uuid;
  v_privilege text := app.identity_privilege(p_clerk_role);
  v_id uuid;
begin
  select id into v_org from app.orgs where clerk_org_id = p_clerk_org_id;
  if not found then
    raise exception 'unknown org %', p_clerk_org_id using errcode = 'foreign_key_violation';
  end if;
  if coalesce(p_email, '') !~ '^[^@\s]+@[^@\s]+$' then
    raise exception 'invalid email' using errcode = 'check_violation';
  end if;

  select id into v_user from app.users where lower(email) = lower(p_email);
  if not found then
    insert into app.users (email) values (p_email) returning id into v_user;
  end if;
  if app.identity_is_stale(v_org, v_user, p_event_at) then
    select id into v_id from app.org_memberships where org_id = v_org and user_id = v_user;
    return v_id;
  end if;
  select m.id into v_inviter
    from app.org_memberships m join app.users u on u.id = m.user_id
   where m.org_id = v_org and u.clerk_user_id = p_invited_by_clerk_user_id;

  insert into app.org_memberships (org_id, user_id, privilege, status, invited_by, identity_event_at)
  values (v_org, v_user, v_privilege, 'invited', v_inviter, p_event_at)
  on conflict (org_id, user_id) do update
     set status     = case when app.org_memberships.status = 'removed' then 'invited' else app.org_memberships.status end,
         privilege  = case when app.org_memberships.status = 'active' then app.org_memberships.privilege else excluded.privilege end,
         invited_by = case when app.org_memberships.status = 'active' then app.org_memberships.invited_by
                           else coalesce(excluded.invited_by, app.org_memberships.invited_by) end,
         identity_event_at = case when app.org_memberships.status = 'active' then app.org_memberships.identity_event_at
                                  else excluded.identity_event_at end
  returning id into v_id;
  perform app.identity_audit(v_org, 'identity.invited', p_email, jsonb_build_object('privilege', v_privilege));
  return v_id;
end $$;

-- The next three functions lock the membership, ignore stale events, and otherwise always advance
-- identity_event_at, even when the event confirms the current state: a later stale event must
-- not slip in under an older stamp.

-- Returns the membership id, or null when there is no such (non removed) membership.
create function app.identity_set_privilege(
  p_clerk_org_id text, p_clerk_user_id text, p_clerk_role text, p_event_at timestamptz default now())
returns uuid
language plpgsql security definer set search_path = app, pg_temp as $$
declare
  v_privilege text := app.identity_privilege(p_clerk_role);
  v_member record;
begin
  select m.id, m.org_id, m.privilege, m.identity_event_at into v_member
    from app.org_memberships m join app.orgs o on o.id = m.org_id join app.users u on u.id = m.user_id
   where o.clerk_org_id = p_clerk_org_id and u.clerk_user_id = p_clerk_user_id and m.status <> 'removed'
     for update of m;
  if not found then
    return null;
  end if;
  if v_member.identity_event_at > p_event_at then
    return v_member.id;
  end if;
  update app.org_memberships set privilege = v_privilege, identity_event_at = p_event_at where id = v_member.id;
  if v_member.privilege <> v_privilege then
    perform app.identity_audit(v_member.org_id, 'identity.privilege_changed', p_clerk_user_id,
                               jsonb_build_object('privilege', v_privilege));
  end if;
  return v_member.id;
end $$;

create function app.identity_remove_membership(
  p_clerk_org_id text, p_clerk_user_id text, p_event_at timestamptz default now())
returns uuid
language plpgsql security definer set search_path = app, pg_temp as $$
declare
  v_member record;
begin
  select m.id, m.org_id, m.status, m.identity_event_at into v_member
    from app.org_memberships m join app.orgs o on o.id = m.org_id join app.users u on u.id = m.user_id
   where o.clerk_org_id = p_clerk_org_id and u.clerk_user_id = p_clerk_user_id
     for update of m;
  if not found then
    return null;
  end if;
  if v_member.identity_event_at > p_event_at then
    return v_member.id;
  end if;
  update app.org_memberships set status = 'removed', identity_event_at = p_event_at where id = v_member.id;
  if v_member.status <> 'removed' then
    perform app.identity_audit(v_member.org_id, 'identity.membership_removed', p_clerk_user_id);
  end if;
  return v_member.id;
end $$;

create function app.identity_revoke_invitation(
  p_clerk_org_id text, p_email text, p_event_at timestamptz default now())
returns uuid
language plpgsql security definer set search_path = app, pg_temp as $$
declare
  v_member record;
begin
  select m.id, m.org_id, m.status, m.identity_event_at into v_member
    from app.org_memberships m join app.orgs o on o.id = m.org_id join app.users u on u.id = m.user_id
   where o.clerk_org_id = p_clerk_org_id and lower(u.email) = lower(p_email)
     for update of m;
  if not found or v_member.identity_event_at > p_event_at or v_member.status = 'active' then
    return null;  -- unknown, stale, or already joined (an accepted invitation is not revoked)
  end if;
  update app.org_memberships set status = 'removed', identity_event_at = p_event_at where id = v_member.id;
  if v_member.status = 'invited' then
    perform app.identity_audit(v_member.org_id, 'identity.invitation_revoked', p_email);
    return v_member.id;
  end if;
  return null;
end $$;

-- Clerk deleted the user: disable them and remove their memberships. A membership that is an
-- org's last active admin is kept (the database refuses to leave an org without one) and the
-- org's audit log records it, so an admin can be appointed.
create function app.identity_disable_user(p_clerk_user_id text)
returns integer
language plpgsql security definer set search_path = app, pg_temp as $$
declare
  v_user uuid;
  v_member record;
  v_removed integer := 0;
begin
  update app.users set status = 'disabled' where clerk_user_id = p_clerk_user_id returning id into v_user;
  if v_user is null then
    return 0;
  end if;
  for v_member in select id, org_id from app.org_memberships where user_id = v_user and status <> 'removed' loop
    begin
      update app.org_memberships set status = 'removed' where id = v_member.id;
      v_removed := v_removed + 1;
      perform app.identity_audit(v_member.org_id, 'identity.membership_removed', p_clerk_user_id,
                                 '{"reason": "user deleted"}');
    exception when check_violation then
      perform app.identity_audit(v_member.org_id, 'identity.last_admin_kept', p_clerk_user_id,
                                 '{"reason": "user deleted in Clerk but is the last active admin"}');
    end;
  end loop;
  return v_removed;
end $$;

create function app.identity_offboard_org(p_clerk_org_id text)
returns uuid
language plpgsql security definer set search_path = app, pg_temp as $$
declare
  v_id uuid;
begin
  update app.orgs set status = 'offboarding' where clerk_org_id = p_clerk_org_id and status <> 'offboarding'
  returning id into v_id;
  if v_id is not null then
    perform app.identity_audit(v_id, 'identity.org_offboarding', p_clerk_org_id);
  end if;
  return v_id;
end $$;

/* ---------------------------------------------------------------- console resolvers */

-- Maps a verified Clerk session (user, active org) to our membership. Privilege comes from here.
create function app.resolve_console_identity(p_clerk_user_id text, p_clerk_org_id text)
returns table (org_id uuid, membership_id uuid, user_id uuid, privilege text,
               membership_status text, user_status text, org_status text)
language sql stable security definer set search_path = app, pg_temp as $$
  select o.id, m.id, u.id, m.privilege, m.status, u.status, o.status
    from app.users u
    join app.org_memberships m on m.user_id = u.id
    join app.orgs o on o.id = m.org_id
   where u.clerk_user_id = p_clerk_user_id
     and o.clerk_org_id = p_clerk_org_id
$$;

create function app.list_console_orgs(p_clerk_user_id text)
returns table (org_id uuid, clerk_org_id text, name text, privilege text)
language sql stable security definer set search_path = app, pg_temp as $$
  select o.id, o.clerk_org_id, o.name, m.privilege
    from app.users u
    join app.org_memberships m on m.user_id = u.id and m.status = 'active'
    join app.orgs o on o.id = m.org_id and o.status = 'active'
   where u.clerk_user_id = p_clerk_user_id and u.status = 'active'
   order by o.name
$$;

/* ---------------------------------------------------------------- grants */

revoke all on function
  app.identity_audit(uuid, text, text, jsonb),
  app.identity_is_stale(uuid, uuid, timestamptz),
  app.identity_upsert_org(text, text),
  app.identity_upsert_user(text, text, text),
  app.identity_upsert_membership(text, text, text, text, text, text, timestamptz),
  app.identity_invite(text, text, text, text, timestamptz),
  app.identity_set_privilege(text, text, text, timestamptz),
  app.identity_remove_membership(text, text, timestamptz),
  app.identity_revoke_invitation(text, text, timestamptz),
  app.identity_disable_user(text),
  app.identity_offboard_org(text),
  app.resolve_console_identity(text, text),
  app.list_console_orgs(text)
from public;

grant execute on function
  app.identity_upsert_org(text, text),
  app.identity_upsert_user(text, text, text),
  app.identity_upsert_membership(text, text, text, text, text, text, timestamptz),
  app.identity_invite(text, text, text, text, timestamptz),
  app.identity_set_privilege(text, text, text, timestamptz),
  app.identity_remove_membership(text, text, timestamptz),
  app.identity_revoke_invitation(text, text, timestamptz),
  app.identity_disable_user(text),
  app.identity_offboard_org(text)
to app_identity_sync;

grant execute on function app.resolve_console_identity(text, text), app.list_console_orgs(text) to app_user;
