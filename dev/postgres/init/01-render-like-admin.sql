-- Stand-in for Render's provisioning (runs once, on an empty data directory).
-- Render gives each database a default user that owns it and can create roles but is not a
-- superuser (working assumption, see spec/decisions_and_blockers.md, phase 3). Migrations run as
-- this user locally so Compose behaves like Render. The `postgres` superuser is kept only for
-- creating throwaway test databases.
create role agent_admin login createrole createdb password 'agent_admin';
alter database agent owner to agent_admin;
