"""A database with two fully populated orgs: at least one row in every tenant table.

Tests run inside transactions that are rolled back (``tx``), so they can mutate freely. Tests
that need commits (deferred constraints, concurrency) open their own connections.
"""

import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import psycopg
import pytest
from psycopg import sql
from psycopg.types.json import Jsonb

from app.db.migrate import migrate
from app.seed import seed
from tests.conftest import throwaway_database

# Every table with an org_id column. A meta test fails if a new tenant table is not listed here,
# which forces it into the generic isolation suite.
TENANT_TABLES = [
    "org_settings", "org_memberships", "custom_roles", "membership_roles", "workspaces",
    "platform_identities", "access_requests", "channels", "channel_members", "org_integrations",
    "connections", "connection_secrets", "operation_policies", "repositories", "services",
    "service_owners", "service_channels", "autonomy_policies", "user_preferences", "playbooks",
    "sessions", "schedules", "tasks", "task_steps", "approvals", "external_actions", "artifacts",
    "task_links", "usage_events", "usage_daily", "audit_log",
]  # fmt: skip


@dataclass
class Org:
    key: str
    id: UUID
    admin: UUID  # membership ids
    user: UUID
    workspace: UUID
    channel: UUID  # allowlisted channel the user belongs to
    ids: dict[str, UUID] = field(default_factory=dict)  # one row id per table, where it has one


@dataclass
class World:
    url: str
    acme: Org
    globex: Org
    builtin_playbook: UUID


def _one(
    conn: psycopg.Connection[Any], query: str | sql.SQL | sql.Composed, params: tuple[Any, ...] = ()
) -> Any:
    row = conn.execute(query, params).fetchone()
    assert row is not None, query
    return row[0]


def _membership(conn: psycopg.Connection[Any], org: UUID, email: str) -> UUID:
    member: UUID = _one(
        conn,
        "select m.id from app.org_memberships m join app.users u on u.id = m.user_id "
        "where m.org_id = %s and u.email = %s",
        (org, email),
    )
    return member


def _populate(conn: psycopg.Connection[Any], o: Org) -> None:
    """Rows for the tables the seed does not fill. Runs as superuser (no RLS)."""
    q = conn.execute
    o.ids["access_requests"] = _one(
        conn,
        "insert into app.access_requests (org_id, workspace_id, external_user_id, email) "
        "values (%s, %s, %s, %s) returning id",
        (o.id, o.workspace, f"U0OUT{o.key.upper()}", f"outsider@{o.key}.test"),
    )
    for key in ("jira", "github"):
        q(
            "insert into app.org_integrations (org_id, integration_key, kind, enabled) values (%s, %s, 'native', true)",
            (o.id, key),
        )
    o.ids["org_integrations"] = _one(
        conn, "select id from app.org_integrations where org_id = %s limit 1", (o.id,)
    )
    o.ids["connections"] = _one(
        conn,
        "insert into app.connections (org_id, integration_key, scope) values (%s, 'jira', 'org') returning id",
        (o.id,),
    )
    o.ids["user_connection"] = _one(
        conn,
        "insert into app.connections (org_id, integration_key, scope, membership_id) "
        "values (%s, 'jira', 'user', %s) returning id",
        (o.id, o.user),
    )
    q(
        "insert into app.connection_secrets (connection_id, org_id, ciphertext, kms_key_id) values (%s, %s, %s, 'k1')",
        (o.ids["connections"], o.id, f"secret-of-{o.key}".encode()),
    )
    q(
        "insert into app.operation_policies (org_id, integration_key, operation, credential_mode) "
        "values (%s, 'jira', 'search_issues', 'org_with_check')",
        (o.id,),
    )
    o.ids["repositories"] = _one(
        conn,
        "insert into app.repositories (org_id, external_id, full_name) values (%s, %s, %s) returning id",
        (o.id, f"R_{o.key}", f"{o.key}/api"),
    )
    o.ids["services"] = _one(
        conn,
        "insert into app.services (org_id, name, repository_id) values (%s, 'api', %s) returning id",
        (o.id, o.ids["repositories"]),
    )
    q(
        "insert into app.service_owners (org_id, service_id, membership_id) values (%s, %s, %s)",
        (o.id, o.ids["services"], o.user),
    )
    q(
        "insert into app.service_channels (org_id, service_id, channel_id) values (%s, %s, %s)",
        (o.id, o.ids["services"], o.channel),
    )
    q(
        "insert into app.autonomy_policies (org_id, scope_type, channel_id, max_mode) values (%s, 'channel', %s, 'auto')",
        (o.id, o.channel),
    )
    q(
        "insert into app.autonomy_policies (org_id, scope_type, repository_id, max_mode) values (%s, 'repo', %s, 'ask')",
        (o.id, o.ids["repositories"]),
    )
    o.ids["playbooks"] = _one(
        conn,
        "insert into app.playbooks (org_id, key, version, title, instructions) "
        "values (%s, 'runbook', 1, 'Org runbook', 'Do the thing') returning id",
        (o.id,),
    )
    o.ids["sessions"] = _one(
        conn,
        "insert into app.sessions (org_id, workspace_id, channel_id, external_channel_id, external_thread_id, "
        "started_by) values (%s, %s, %s, 'C0X', %s, %s) returning id",
        (o.id, o.workspace, o.channel, f"1700000000.{o.key}", o.user),
    )
    o.ids["schedules"] = _one(
        conn,
        "insert into app.schedules (org_id, owner_membership_id, name, instruction, cron, timezone, "
        "delivery_type, delivery_channel_id) values (%s, %s, 'Daily', 'Summarize', '0 9 * * *', "
        "'Europe/Berlin', 'channel', %s) returning id",
        (o.id, o.user, o.channel),
    )
    o.ids["tasks"] = _one(
        conn,
        "insert into app.tasks (org_id, session_id, requested_by, trigger, playbook_id, model_tier, status, "
        "finished_at) values (%s, %s, %s, 'mention', %s, 'standard', 'completed', now()) returning id",
        (o.id, o.ids["sessions"], o.user, o.ids["playbooks"]),
    )
    o.ids["task_steps"] = _one(
        conn,
        "insert into app.task_steps (org_id, task_id, seq, kind) values (%s, %s, 1, 'gather') returning id",
        (o.id, o.ids["tasks"]),
    )
    o.ids["approvals"] = _one(
        conn,
        "insert into app.approvals (org_id, task_id, step_id, action_summary, action_tier, expires_at) "
        "values (%s, %s, %s, 'Create ticket', 'reversible_write', now() + interval '1 day') returning id",
        (o.id, o.ids["tasks"], o.ids["task_steps"]),
    )
    o.ids["external_actions"] = _one(
        conn,
        "insert into app.external_actions (org_id, task_id, integration_key, operation, idempotency_key) "
        "values (%s, %s, 'jira', 'create_issue', 'idem-key-0001') returning id",
        (o.id, o.ids["tasks"]),
    )
    o.ids["artifacts"] = _one(
        conn,
        "insert into app.artifacts (org_id, task_id, kind, content) values (%s, %s, 'brief', %s) returning id",
        (o.id, o.ids["tasks"], Jsonb({"goal": f"{o.key} goal"})),
    )
    q(
        "insert into app.task_links (org_id, task_id, kind, external_id) values (%s, %s, 'pull_request', %s)",
        (o.id, o.ids["tasks"], f"{o.key}/api#1"),
    )
    q(
        "insert into app.usage_events (org_id, membership_id, task_id, kind, model, input_tokens, output_tokens) "
        "values (%s, %s, %s, 'llm', 'claude', 100, 50)",
        (o.id, o.user, o.ids["tasks"]),
    )


def _org(conn: psycopg.Connection[Any], key: str, clerk: str, admin: str, user: str, channel: str) -> Org:
    org_id = _one(conn, "select id from app.orgs where clerk_org_id = %s", (clerk,))
    return Org(
        key=key,
        id=org_id,
        admin=_membership(conn, org_id, admin),
        user=_membership(conn, org_id, user),
        workspace=_one(conn, "select id from app.workspaces where org_id = %s", (org_id,)),
        channel=_one(conn, "select id from app.channels where org_id = %s and name = %s", (org_id, channel)),
    )


@pytest.fixture(scope="session")
def world(postgres: None) -> Iterator[World]:
    with throwaway_database("world") as url:
        migrate(url)
        if mutation := os.environ.get("SCHEMA_MUTATION"):
            # Used by scripts/schema_mutations.sh to prove the suite fails when a guarantee breaks.
            with psycopg.connect(url) as conn:
                conn.execute(mutation)
        seed(url)
        with psycopg.connect(url) as conn:
            acme = _org(conn, "acme", "org_acme", "alice@acme.test", "bob@acme.test", "bugs")
            globex = _org(conn, "globex", "org_globex", "dave@globex.test", "erin@globex.test", "general")
            builtin = _one(
                conn,
                "insert into app.playbooks (org_id, key, version, title, instructions) "
                "values (null, 'qa_summary', 1, 'Q&A and summaries', 'Answer with sources') returning id",
            )
            _populate(conn, acme)
            _populate(conn, globex)
            conn.commit()
        yield World(url=url, acme=acme, globex=globex, builtin_playbook=builtin)


@contextmanager
def tx(url: str, role: str | None = None, org: UUID | None = None) -> Iterator[psycopg.Connection[Any]]:
    """A connection in one transaction, optionally as a role and bound to an org; always rolled back."""
    with psycopg.connect(url) as conn:
        try:
            if role:
                conn.execute(sql.SQL("set local role {}").format(sql.Identifier(role)))
            if org:
                conn.execute("select set_config('app.org_id', %s, true)", (str(org),))
            yield conn
        finally:
            conn.rollback()


def scalar(
    conn: psycopg.Connection[Any], query: str | sql.SQL | sql.Composed, params: tuple[Any, ...] = ()
) -> Any:
    return _one(conn, query, params)


def soon(minutes: int = 60) -> datetime:
    return datetime.now(UTC) + timedelta(minutes=minutes)
