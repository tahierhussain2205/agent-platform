#!/usr/bin/env bash
# Mutation check for the schema test suite: weaken one database guarantee at a time and confirm
# the suite notices. Every mutation must make at least one test fail.
set -uo pipefail
cd "$(dirname "$0")/../backend"
unset VIRTUAL_ENV

declare -a MUTATIONS=(
  "alter table app.channels disable row level security"
  "drop policy tenant_isolation on app.tasks; create policy tenant_isolation on app.tasks using (true) with check (true)"
  "grant select on app.connection_secrets to app_user"
  "drop trigger org_memberships_guard_last_admin on app.org_memberships"
  "alter table app.channel_members drop constraint channel_members_org_id_channel_id_fkey, add foreign key (channel_id) references app.channels (id)"
  "drop trigger approvals_decision on app.approvals"
  "grant update, delete on app.audit_log to app_user"
  "drop trigger channels_access_change on app.channels"
  "grant select on app.audit_log_default to app_user"
  "drop policy users_in_current_org on app.users; create policy users_in_current_org on app.users for select using (true)"
  "grant execute on function app.maintenance_active_workspaces() to app_user"
  "grant execute on function app.maintenance_fail_task(uuid, text) to app_user"
  "grant select on app.tasks to app_vault"
  "grant select on app.task_steps to app_vault"
  "drop trigger schedules_track on app.schedules"
  "grant execute on function app.maintenance_schedule_orgs() to app_user"
  "drop index app.services_live_name"
  "alter table app.sandboxes drop constraint sandboxes_task_id_repository_key"
  "drop trigger orgs_offboarding_since on app.orgs"
  "grant execute on function app.maintenance_offboard_org(uuid, interval) to app_user"
)

failed=0
for m in "${MUTATIONS[@]}"; do
  out=$(SCHEMA_MUTATION="$m" uv run pytest -q -x -p no:randomly tests/schema 2>&1)
  summary=$(echo "$out" | tail -1)
  if [[ $summary == *" error"* ]]; then
    # The mutation itself failed to apply (fixture error), so it proves nothing.
    printf '  \033[31mBROKEN\033[0m   %s\n           %s\n' "$m" "$summary"; failed=1
  elif [[ $summary == *" failed"* ]]; then
    test=$(echo "$out" | grep -m1 '^FAILED' | cut -d' ' -f2)
    printf '  \033[32mcaught\033[0m   %s\n           by %s\n' "$m" "$test"
  else
    printf '  \033[31mSURVIVED\033[0m %s\n' "$m"; failed=1
  fi
done
exit $failed
