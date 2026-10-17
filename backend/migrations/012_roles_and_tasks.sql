/* Role profiles, role limits, approvals routed to a role, and the purpose of each model call
   (docs/plans/demo_roles_actions_tasks.md, D-201 to D-205).

   - usage_events.purpose: which step a model call served, for the console's task timeline.
   - custom_roles.profile: how the agent answers the role's holders (detail, style, format, focus),
     validated in code; the description stays the free text part.
   - custom_roles limits, which only ever narrow: the playbooks the role's holders may use (null:
     all), operations blocked for them, writes that always ask, a cap on their mode, and the
     operations whose approval requests the role's holders decide.
   - approvals.approver_role_id with approver_rule 'role_or_admin': a holder of the role or an
     active admin decides. Who decides is fixed when the request is made.
   - app.effective_mode also caps by the requester's roles.

   Safe for the previous code version: the new columns are nullable or have defaults, the rule check
   only gains a value, and both functions keep their signatures. */

set local role app_owner;

alter table app.usage_events
  add column purpose text check (purpose is null or length(purpose) between 1 and 64);

alter table app.custom_roles
  add column profile             jsonb not null default '{}',
  add column allowed_playbooks   text[],
  add column blocked_operations  text[] not null default '{}',
  add column ask_operations      text[] not null default '{}',
  add column approves_operations text[] not null default '{}',
  add column max_mode            app.autonomy_mode,
  add constraint profile_bounded check (jsonb_typeof(profile) = 'object' and pg_column_size(profile) <= 8192),
  add constraint limits_bounded check (
    coalesce(cardinality(allowed_playbooks), 0) <= 50
    and cardinality(blocked_operations) <= 200
    and cardinality(ask_operations) <= 200
    and cardinality(approves_operations) <= 200);

alter table app.approvals
  add column approver_role_id uuid,
  drop constraint approvals_approver_rule_check,
  add constraint approvals_approver_rule_check
    check (approver_rule in ('requester', 'admin', 'requester_or_admin', 'role_or_admin')),
  add constraint approver_role_with_rule
    check ((approver_rule = 'role_or_admin') = (approver_role_id is not null)),
  add constraint approvals_approver_role_fk
    foreign key (org_id, approver_role_id) references app.custom_roles (org_id, id);

-- As in 001, plus: 'role_or_admin' lets an active holder of the approver role decide, and the
-- rule and role cannot change once the request exists.
create or replace function app.check_approval_decision() returns trigger
language plpgsql as $$
declare
  v_requester         uuid;
  v_decider_status    text;
  v_decider_privilege text;
  v_holds_role        boolean := false;
begin
  if new.approver_rule is distinct from old.approver_rule
     or new.approver_role_id is distinct from old.approver_role_id then
    raise exception 'who decides approval % is fixed', old.id using errcode = 'check_violation';
  end if;

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
    if old.approver_rule = 'role_or_admin' then
      select exists (
        select 1 from app.membership_roles mr
          join app.custom_roles r on r.id = mr.role_id and r.org_id = mr.org_id
         where mr.membership_id = new.decided_by and mr.role_id = old.approver_role_id
           and mr.org_id = old.org_id and r.status = 'active') into v_holds_role;
    end if;

    if coalesce(v_decider_status, 'removed') = 'removed' or not (
         (old.approver_rule in ('requester', 'requester_or_admin') and new.decided_by = v_requester)
      or (old.approver_rule = 'role_or_admin' and v_holds_role and v_decider_status = 'active')
      or (old.approver_rule in ('admin', 'requester_or_admin', 'role_or_admin')
          and v_decider_privilege = 'admin' and v_decider_status = 'active')) then
      raise exception 'member % may not decide approval %', new.decided_by, old.id
        using errcode = 'insufficient_privilege';
    end if;

    new.decided_at := coalesce(new.decided_at, now());
  end if;
  return new;
end $$;

-- As in 001, plus a cap from the requester's active roles: any role capped at ask makes it ask.
create or replace function app.effective_mode(p_membership uuid, p_channel uuid default null, p_repository uuid default null)
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
  ), role_cap as (
    select coalesce(bool_or(r.max_mode = 'ask'), false) as capped
      from app.membership_roles mr
      join app.custom_roles r on r.id = mr.role_id
     where mr.membership_id = p_membership and r.status = 'active'
  )
  select (case when pref.mode = 'auto' and ceiling.mode = 'auto' and not role_cap.capped
               then 'auto' else 'ask' end)::app.autonomy_mode
    from pref, ceiling, role_cap
$$;
