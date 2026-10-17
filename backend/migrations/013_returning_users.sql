/* Someone deleted in Clerk can sign up again with the same email (D-206).

   identity_disable_user keeps the deleted person's row (disabled, with the old Clerk id) so the
   audit trail still names them. A new Clerk account with the same email then failed every sync
   ("email belongs to another user"): the person could never come back. Clerk verifies the email of
   a new account, so the disabled row is theirs again: it takes the new Clerk id and is active.
   Memberships removed with the deletion stay removed; an active user's email still cannot be taken.

   Safe for the previous code version: the function keeps its signature. */

set local role app_owner;

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

  -- Deleted in Clerk and back with a new account for the same email.
  update app.users
     set clerk_user_id = p_clerk_user_id, status = 'active', name = coalesce(nullif(btrim(p_name), ''), name)
   where lower(email) = lower(p_email) and status = 'disabled'
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
