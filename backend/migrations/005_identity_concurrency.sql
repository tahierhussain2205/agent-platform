/*
  005: identity functions safe under concurrent creation of the same person.

  identity_invite and identity_upsert_user looked a user up by email and inserted it when absent.
  When the console mirrors an invitation inside its transaction and Clerk's webhook for the same
  invitation arrives before that commit, the webhook's insert failed on the email index and the
  webhook returned 500 until Svix retried. Both functions now insert with ON CONFLICT on the email,
  which waits for the other transaction and reuses its row. Bodies are otherwise unchanged.
*/

set local role app_owner;
set local search_path = app, pg_temp;

create or replace function app.identity_upsert_user(p_clerk_user_id text, p_email text, p_name text)
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

  -- A concurrent invitation may be creating the same person: wait for it and link that row.
  insert into app.users (clerk_user_id, email, name)
  values (p_clerk_user_id, p_email, nullif(btrim(p_name), ''))
  on conflict ((lower(email))) do update
     set clerk_user_id = excluded.clerk_user_id, name = coalesce(excluded.name, app.users.name)
   where app.users.clerk_user_id is null
  returning id into v_id;
  if v_id is null then
    raise exception 'email % belongs to another user', p_email using errcode = 'unique_violation';
  end if;
  return v_id;
end $$;

create or replace function app.identity_invite(
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

  -- A concurrent sync (the console mirroring an invite while Clerk's webhook for it arrives) may be
  -- creating the same person: wait for it and use that row instead of failing.
  insert into app.users (email) values (p_email)
  on conflict ((lower(email))) do nothing
  returning id into v_user;
  if v_user is null then
    select id into v_user from app.users where lower(email) = lower(p_email);
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
