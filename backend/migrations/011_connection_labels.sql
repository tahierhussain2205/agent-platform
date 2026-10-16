/* A readable name for the account behind a connection (a Jira site's address, a person's name, a
   GitHub organization) next to its provider id, which stays what the code matches on. The vault sets
   it when it connects; the console shows it. Safe for the previous code version: a nullable column. */

set local role app_owner;

alter table app.connections add column external_account_label text;
grant update (external_account_label) on app.connections to app_vault;
