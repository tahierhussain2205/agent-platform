from collections.abc import Iterator
from typing import Any

import psycopg
import pytest

from app.db.migrate import migrate
from app.seed import load_fixture, seed
from tests.conftest import throwaway_database

FIXTURE = load_fixture()
# Tables whose contents must be identical after a second seed run (audit_log is append only).
IDEMPOTENT_TABLES = [
    "orgs", "org_settings", "users", "org_memberships", "custom_roles", "membership_roles",
    "workspaces", "platform_identities", "channels", "channel_members", "user_preferences",
    "autonomy_policies",
]  # fmt: skip


@pytest.fixture(scope="module")
def seeded_db(postgres: None) -> Iterator[str]:
    with throwaway_database("seed") as url:
        migrate(url)
        seed(url)
        yield url


def _snapshot(url: str) -> dict[str, list[Any]]:
    out: dict[str, list[Any]] = {}
    with psycopg.connect(url) as conn:
        for table in IDEMPOTENT_TABLES:
            # updated_at / synced_at legitimately move; compare everything else.
            cols = [
                r[0]
                for r in conn.execute(
                    "select column_name from information_schema.columns where table_schema = 'app' "
                    "and table_name = %s and column_name not in ('updated_at', 'synced_at') "
                    "order by ordinal_position",
                    (table,),
                )
            ]
            col_list = ", ".join(cols)
            out[table] = conn.execute(f"select {col_list} from app.{table} order by 1, 2").fetchall()
    return out


def _as_app_user(url: str, org_id: Any) -> psycopg.Connection[Any]:
    conn = psycopg.connect(url)
    conn.execute("set role app_user")
    if org_id is not None:
        conn.execute("select set_config('app.org_id', %s, true)", (str(org_id),))
    return conn


def _org_id(url: str, clerk_org_id: str) -> Any:
    with psycopg.connect(url) as conn:
        row = conn.execute("select id from app.orgs where clerk_org_id = %s", (clerk_org_id,)).fetchone()
        assert row is not None
        return row[0]


def test_counts_match_fixture(seeded_db: str) -> None:
    with psycopg.connect(seeded_db) as conn:

        def count(q: str) -> int:
            row = conn.execute(q).fetchone()
            assert row is not None
            return int(row[0])

        assert count("select count(*) from app.orgs") == len(FIXTURE["orgs"])
        assert count("select count(*) from app.users") == len(FIXTURE["users"])
        assert count("select count(*) from app.org_memberships") == sum(
            len(o["members"]) for o in FIXTURE["orgs"]
        )
        assert count("select count(*) from app.channels") == sum(len(o["channels"]) for o in FIXTURE["orgs"])
        assert count("select count(*) from app.workspaces") == len(FIXTURE["orgs"])
        assert count("select count(*) from app.org_settings") == len(FIXTURE["orgs"])


def test_second_run_changes_nothing(seeded_db: str) -> None:
    before = _snapshot(seeded_db)
    seed(seeded_db)
    assert _snapshot(seeded_db) == before


def test_invited_user_has_no_clerk_link(seeded_db: str) -> None:
    with psycopg.connect(seeded_db) as conn:
        row = conn.execute(
            "select u.clerk_user_id, m.status, m.joined_at from app.users u "
            "join app.org_memberships m on m.user_id = u.id where u.email = 'carol@acme.test'"
        ).fetchone()
    assert row == (None, "invited", None)


def test_person_in_two_orgs_has_two_memberships(seeded_db: str) -> None:
    with psycopg.connect(seeded_db) as conn:
        orgs = conn.execute(
            "select o.clerk_org_id from app.users u join app.org_memberships m on m.user_id = u.id "
            "join app.orgs o on o.id = m.org_id where u.email = 'frank@both.test' order by 1"
        ).fetchall()
    assert orgs == [("org_acme",), ("org_globex",)]


def test_every_role_holder_has_exactly_one_primary(seeded_db: str) -> None:
    with psycopg.connect(seeded_db) as conn:
        bad = conn.execute(
            "select membership_id from app.membership_roles group by membership_id "
            "having count(*) filter (where is_primary) <> 1"
        ).fetchall()
        primaries: dict[str, str] = dict(
            conn.execute(
                "select u.email || '@' || o.clerk_org_id, r.title from app.membership_roles mr "
                "join app.org_memberships m on m.id = mr.membership_id join app.users u on u.id = m.user_id "
                "join app.orgs o on o.id = m.org_id join app.custom_roles r on r.id = mr.role_id "
                "where mr.is_primary"
            ).fetchall()
        )
    assert bad == []
    assert primaries["alice@acme.test@org_acme"] == "Manager"
    assert primaries["erin@globex.test@org_globex"] == "Engineer"


def test_channel_rule_reflects_fixture(seeded_db: str) -> None:
    acme = _org_id(seeded_db, "org_acme")
    with _as_app_user(seeded_db, acme) as conn:

        def can_read(email: str, channel: str) -> bool:
            row = conn.execute(
                "select app.can_read_channel(m.id, c.id) from app.org_memberships m "
                "join app.users u on u.id = m.user_id, app.channels c where u.email = %s and c.name = %s",
                (email, channel),
            ).fetchone()
            assert row is not None
            return bool(row[0])

        assert can_read("bob@acme.test", "bugs")
        assert not can_read("bob@acme.test", "leads-private")  # allowlisted but not a member
        assert can_read("alice@acme.test", "leads-private")
        assert not can_read("alice@acme.test", "random")  # member but not allowlisted
        assert not can_read("alice@acme.test", "old-project")  # archived


def test_seeded_orgs_are_isolated_for_app_user(seeded_db: str) -> None:
    acme = _org_id(seeded_db, "org_acme")
    globex = _org_id(seeded_db, "org_globex")
    with _as_app_user(seeded_db, acme) as conn:
        assert conn.execute("select name from app.orgs").fetchall() == [("Acme Corp",)]
        names = {r[0] for r in conn.execute("select name from app.channels")}
        assert "incidents" not in names
        assert "bugs" in names
        emails = {r[0] for r in conn.execute("select email from app.users")}
        assert "dave@globex.test" not in emails
        assert "frank@both.test" in emails
        # Guessing another org's id does not help.
        assert conn.execute("select count(*) from app.channels where org_id = %s", (globex,)).fetchone() == (
            0,
        )
    with _as_app_user(seeded_db, None) as conn:
        assert conn.execute("select count(*) from app.channels").fetchone() == (0,)
        assert conn.execute("select count(*) from app.users").fetchone() == (0,)


def test_effective_mode_from_fixture(seeded_db: str) -> None:
    def mode(clerk_org: str, email: str) -> str:
        org = _org_id(seeded_db, clerk_org)
        with _as_app_user(seeded_db, org) as conn:
            row = conn.execute(
                "select app.effective_mode(m.id) from app.org_memberships m "
                "join app.users u on u.id = m.user_id where u.email = %s",
                (email,),
            ).fetchone()
            assert row is not None
            return str(row[0])

    assert mode("org_acme", "alice@acme.test") == "auto"  # chose auto, ceiling auto
    assert mode("org_acme", "bob@acme.test") == "ask"  # chose ask
    assert mode("org_globex", "erin@globex.test") == "ask"  # chose auto, ceiling ask


def test_seed_refused_outside_local(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from app import cli

    monkeypatch.setenv("ENV", "production")
    monkeypatch.setenv("MIGRATION_DATABASE_URL", "postgresql://nobody@127.0.0.1:1/none")
    assert cli.main(["seed"]) == 2
    assert "refusing" in capsys.readouterr().err


def test_reseed_puts_fixture_orgs_back(seeded_db: str) -> None:
    """People invited since the last seed are removed, open access requests are dismissed,
    integrations other than Slack are removed and token usage is cleared, so end to end runs start
    from the fixture. Declared members are untouched. (Runs last: it adds rows.)"""
    acme = _org_id(seeded_db, "org_acme")
    with psycopg.connect(seeded_db) as conn:
        conn.execute("set role app_owner")
        user = conn.execute(
            "insert into app.users (email, name) values ('extra@acme.test', 'Extra') returning id"
        ).fetchone()
        assert user is not None
        conn.execute(
            "insert into app.org_memberships (org_id, user_id, status) values (%s, %s, 'invited')",
            (acme, user[0]),
        )
        workspace = conn.execute("select id from app.workspaces where org_id = %s", (acme,)).fetchone()
        assert workspace is not None
        conn.execute(
            "insert into app.access_requests (org_id, workspace_id, external_user_id, email) "
            "values (%s, %s, 'U0MALLORY', 'mallory@acme.test')",
            (acme, workspace[0]),
        )
        conn.execute(
            "insert into app.org_integrations (org_id, integration_key, kind, enabled) "
            "values (%s, 'jira', 'native', true)",
            (acme,),
        )
        conn.execute(
            "insert into app.connections (org_id, integration_key, scope) values (%s, 'jira', 'org')", (acme,)
        )
        conn.execute(
            "insert into app.usage_events (org_id, kind, model, input_tokens, output_tokens) "
            "values (%s, 'llm', 'claude-sonnet-5', 1000, 100)",
            (acme,),
        )
        conn.execute(
            "insert into app.services (org_id, name, status) values (%s, 'cart-service', 'proposed')", (acme,)
        )
        conn.commit()
    seed(seeded_db)
    with psycopg.connect(seeded_db) as conn:
        statuses: dict[str, str] = dict(
            conn.execute(
                "select u.email, m.status from app.org_memberships m join app.users u on u.id = m.user_id "
                "where m.org_id = %s",
                (acme,),
            ).fetchall()
        )
        pending = conn.execute("select count(*) from app.access_requests where status = 'pending'").fetchone()
        left = conn.execute(
            "select (select count(*) from app.org_integrations where org_id = %s and integration_key <> 'slack'), "
            "(select count(*) from app.connections where org_id = %s and integration_key <> 'slack'), "
            "(select count(*) from app.usage_daily where org_id = %s), "
            "(select count(*) from app.schedules where org_id = %s and status <> 'deleted'), "
            "(select count(*) from app.services where org_id = %s)",
            (acme, acme, acme, acme, acme),
        ).fetchone()
    assert left == (0, 0, 0, 0, 0)  # integrations unconnected, budgets unspent, schedules retired, no catalog
    assert statuses.pop("extra@acme.test") == "removed"
    assert statuses == {m["email"]: m["status"] for m in FIXTURE["orgs"][0]["members"]}
    assert pending == (0,)
