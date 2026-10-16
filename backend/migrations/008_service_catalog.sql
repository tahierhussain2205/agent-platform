/*
  008: the service catalog grows proposals and Jira links.

  The agent proposes services from repositories, CODEOWNERS and Jira components; admins confirm
  or edit them. A proposal keeps its evidence (what it is based on, including owners that map to
  no member) and each owner keeps where it comes from and why, so assignee suggestions can explain
  themselves.
*/

set local role app_owner;
set local search_path = app, pg_temp;

alter table app.services drop constraint services_status_check;
alter table app.services
  add constraint services_status_check check (status in ('proposed', 'active', 'archived')),
  add column jira_project   text check (jira_project ~ '^[A-Z][A-Z0-9]{1,9}$'),
  add column jira_component text check (length(btrim(jira_component)) between 1 and 255),
  add column paths          text[] not null default '{}',
  add column evidence       jsonb not null default '[]' check (jsonb_typeof(evidence) = 'array'),
  add column confirmed_by   uuid,
  add column confirmed_at   timestamptz,
  add constraint services_confirmed_by_fk foreign key (org_id, confirmed_by)
    references app.org_memberships (org_id, id) on delete set null (confirmed_by);

-- One live service per name in an org (archived ones may repeat).
create unique index services_live_name on app.services (org_id, lower(btrim(name))) where status <> 'archived';
create index services_component on app.services (org_id, jira_project, jira_component);

alter table app.service_owners
  add column source text not null default 'manual' check (source in ('manual', 'codeowners', 'component_lead')),
  add column reason text check (length(reason) <= 500);
