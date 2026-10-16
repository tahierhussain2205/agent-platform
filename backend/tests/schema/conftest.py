"""Fixtures for the maintenance role's suites (lifecycle, alerts): a throwaway world and a login user
with the maintenance role."""

import uuid
from collections.abc import Iterator

import psycopg
import pytest
from psycopg import sql

from app.db import roles
from app.db.roles import LoginUser, ensure_login_user
from tests.conftest import ADMIN_URL, throwaway_database
from tests.schema.test_db_roles import PASSWORD, _url_for
from tests.world import World, create_world


@pytest.fixture
def fresh(postgres: None) -> Iterator[World]:
    with throwaway_database("lworld") as url:
        yield create_world(url)


@pytest.fixture
def maintenance(fresh: World, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    user = LoginUser("maintenance", f"t{uuid.uuid4().hex[:6]}_maintenance", "app_maintenance")
    with psycopg.connect(fresh.url, autocommit=True) as conn:
        ensure_login_user(conn, user, PASSWORD)
    monkeypatch.setitem(roles.LOGIN_USERS, "maintenance", user)
    yield _url_for(user.username, PASSWORD, fresh.url.rsplit("/", 1)[1])
    with psycopg.connect(ADMIN_URL, autocommit=True) as conn:
        conn.execute(sql.SQL("drop role if exists {}").format(sql.Identifier(user.username)))
