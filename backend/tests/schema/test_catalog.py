"""Service catalog guarantees (migration 008): one live service per name, proposal states, Jira links
in Jira's key format, and owners that say where they come from."""

import psycopg
import pytest

from tests.world import World, scalar


def test_one_live_service_per_name(world: World) -> None:
    with psycopg.connect(world.url) as conn:
        conn.execute("insert into app.services (org_id, name, status) values (%s, 'Cart Service', 'proposed')",
                     (world.acme.id,))  # fmt: skip
        with pytest.raises(psycopg.errors.UniqueViolation):
            conn.execute(
                "insert into app.services (org_id, name) values (%s, ' cart service ')", (world.acme.id,)
            )
        conn.rollback()
        conn.execute(
            "insert into app.services (org_id, name, status) values (%s, 'Cart', 'archived')",
            (world.acme.id,),
        )
        conn.execute(
            "insert into app.services (org_id, name, status) values (%s, 'Cart', 'archived')",
            (world.acme.id,),
        )
        conn.execute("insert into app.services (org_id, name) values (%s, 'Cart')", (world.acme.id,))
        conn.execute(
            "insert into app.services (org_id, name) values (%s, 'Cart')", (world.globex.id,)
        )  # other org
        conn.rollback()


@pytest.mark.parametrize(
    ("column", "value"),
    [("status", "draft"), ("jira_project", "pay"), ("jira_project", "P"), ("jira_component", "  ")],
)
def test_invalid_values_are_refused(world: World, column: str, value: str) -> None:
    with psycopg.connect(world.url) as conn, pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            f"update app.services set {column} = %s where id = %s",
            (value, world.acme.ids["services"]),
        )


def test_owners_record_their_source(world: World) -> None:
    with psycopg.connect(world.url) as conn:
        assert scalar(conn, "select source from app.service_owners where service_id = %s",
                      (world.acme.ids["services"],)) == "manual"  # fmt: skip
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                "update app.service_owners set source = 'guess' where service_id = %s",
                (world.acme.ids["services"],),
            )
        conn.rollback()
        conn.execute(
            "update app.service_owners set source = 'codeowners', reason = 'CODEOWNERS * in acme/api' "
            "where service_id = %s",
            (world.acme.ids["services"],),
        )
        conn.rollback()


def test_confirmed_by_must_be_a_member_of_the_same_org(world: World) -> None:
    with psycopg.connect(world.url) as conn, pytest.raises(psycopg.errors.ForeignKeyViolation):
        conn.execute(
            "update app.services set confirmed_by = %s where id = %s",
            (world.globex.admin, world.acme.ids["services"]),
        )
