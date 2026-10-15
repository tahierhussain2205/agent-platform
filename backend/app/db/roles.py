"""Login users per component (plan phase 3).

Each component connects as its own login user, a member of exactly one of the NOLOGIN roles the
schema defines. ``app migrate`` creates or updates them. Passwords are sent to Postgres as
SCRAM-SHA-256 verifiers computed here, so the plaintext never reaches the server or its logs.
"""

import base64
import hashlib
import hmac
import secrets
from dataclasses import dataclass
from typing import Literal

import psycopg
from psycopg import sql

Component = Literal["api", "worker", "vault", "identity", "maintenance"]


@dataclass(frozen=True)
class LoginUser:
    component: Component
    username: str
    member_of: str


LOGIN_USERS: dict[Component, LoginUser] = {
    "api": LoginUser("api", "api_service", "app_user"),
    "worker": LoginUser("worker", "worker_service", "app_user"),
    "vault": LoginUser("vault", "vault_service", "app_vault"),
    "identity": LoginUser("identity", "identity_service", "app_identity_sync"),
    "maintenance": LoginUser("maintenance", "maintenance_service", "app_maintenance"),
}

_SCRAM_ITERATIONS = 4096  # PostgreSQL's default for scram-sha-256


def scram_sha256_verifier(
    password: str, salt: bytes | None = None, iterations: int = _SCRAM_ITERATIONS
) -> str:
    """The value PostgreSQL stores for a scram-sha-256 password (RFC 5802 / RFC 7677)."""
    salt = salt if salt is not None else secrets.token_bytes(16)
    salted = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    client_key = hmac.new(salted, b"Client Key", hashlib.sha256).digest()
    stored_key = hashlib.sha256(client_key).digest()
    server_key = hmac.new(salted, b"Server Key", hashlib.sha256).digest()

    def b64(data: bytes) -> str:
        return base64.b64encode(data).decode()

    return f"SCRAM-SHA-256${iterations}:{b64(salt)}${b64(stored_key)}:{b64(server_key)}"


def ensure_login_user(conn: psycopg.Connection, user: LoginUser, password: str) -> bool:
    """Create the login user, or update its password. Returns True if it was created.

    The user is a member of its role (inheriting its privileges) and nothing else; it never gets
    BYPASSRLS, CREATEROLE or CREATEDB.
    """
    if len(password) < 16:
        raise ValueError(f"password for {user.username} must be at least 16 characters")
    verifier = sql.Literal(scram_sha256_verifier(password))
    name = sql.Identifier(user.username)
    exists = conn.execute("select 1 from pg_roles where rolname = %s", (user.username,)).fetchone()
    if exists:
        # Only login and password: a CREATEROLE non superuser may not even mention SUPERUSER or
        # BYPASSRLS in ALTER ROLE. The attributes are verified below instead.
        conn.execute(sql.SQL("alter role {} with login password {}").format(name, verifier))
    else:
        conn.execute(
            sql.SQL("create role {} with login nosuperuser nocreaterole nocreatedb password {}").format(
                name, verifier
            )
        )
    conn.execute(sql.SQL("grant {} to {}").format(sql.Identifier(user.member_of), name))
    _verify(conn, user)
    return exists is None


def _verify(conn: psycopg.Connection, user: LoginUser) -> None:
    """Fail closed if the login user has any power beyond membership in its one role."""
    row = conn.execute(
        """
        select r.rolsuper, r.rolcreaterole, r.rolcreatedb, r.rolbypassrls, r.rolreplication,
               coalesce(array_agg(g.rolname::text order by g.rolname)
                          filter (where g.rolname is not null), '{}')
          from pg_roles r
          left join pg_auth_members m on m.member = r.oid
          left join pg_roles g on g.oid = m.roleid
         where r.rolname = %s
         group by r.rolsuper, r.rolcreaterole, r.rolcreatedb, r.rolbypassrls, r.rolreplication
        """,
        (user.username,),
    ).fetchone()
    assert row is not None
    *flags, member_of = row
    if any(flags) or member_of != [user.member_of]:
        raise RuntimeError(
            f"login user {user.username} has unexpected attributes or memberships "
            f"(super/createrole/createdb/bypassrls/replication={flags}, member of {member_of})"
        )
