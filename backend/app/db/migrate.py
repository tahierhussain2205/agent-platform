"""SQL first migration runner.

Each file in ``migrations/`` named ``NNN_description.sql`` runs once, in order, inside a single
transaction together with its bookkeeping row. Files must not contain top level ``begin`` or
``commit``. A session advisory lock serializes concurrent runs, so the connection must be direct
(not through PgBouncer in transaction mode).
"""

import hashlib
import logging
import re
from dataclasses import dataclass
from pathlib import Path

import psycopg
from psycopg import sql

log = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"
_FILE_RE = re.compile(r"^(\d{3})_([a-z0-9_]+)\.sql$")
# Top level transaction control would commit the runner's transaction early.
_TX_CONTROL_RE = re.compile(
    r"^\s*(begin|commit|rollback|start\s+transaction)\s*;", re.IGNORECASE | re.MULTILINE
)
_LOCK_KEY = 7_420_161  # arbitrary, constant across deploys


class MigrationError(RuntimeError):
    pass


@dataclass(frozen=True)
class Migration:
    version: str
    name: str
    path: Path
    sql: str

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.sql.encode()).hexdigest()


def discover(directory: Path = MIGRATIONS_DIR) -> list[Migration]:
    migrations: list[Migration] = []
    seen: set[str] = set()
    for path in sorted(directory.glob("*.sql")):
        match = _FILE_RE.match(path.name)
        if not match:
            raise MigrationError(f"bad migration file name: {path.name} (expected NNN_description.sql)")
        version, name = match.groups()
        if version in seen:
            raise MigrationError(f"duplicate migration version {version}")
        seen.add(version)
        text = path.read_text()
        if _TX_CONTROL_RE.search(text):
            raise MigrationError(f"{path.name} contains top level transaction control; the runner owns it")
        migrations.append(Migration(version=version, name=name, path=path, sql=text))
    return migrations


def _ensure_bookkeeping(conn: psycopg.Connection) -> None:
    conn.execute(
        """
        create table if not exists public.schema_migrations (
          version     text primary key,
          name        text not null,
          checksum    text not null,
          applied_at  timestamptz not null default now()
        )
        """
    )


def applied_versions(conn: psycopg.Connection) -> dict[str, str]:
    rows = conn.execute("select version, checksum from public.schema_migrations").fetchall()
    return dict(rows)


def migrate(database_url: str, directory: Path = MIGRATIONS_DIR) -> list[str]:
    """Apply pending migrations. Returns the versions applied by this run."""
    migrations = discover(directory)
    applied_now: list[str] = []
    with psycopg.connect(database_url, autocommit=True) as conn:
        conn.execute("select pg_advisory_lock(%s)", (_LOCK_KEY,))
        try:
            _ensure_bookkeeping(conn)
            done = applied_versions(conn)
            for migration in migrations:
                if migration.version in done:
                    if done[migration.version] != migration.checksum:
                        raise MigrationError(
                            f"migration {migration.version}_{migration.name} was edited after being applied"
                        )
                    continue
                log.info("applying migration %s_%s", migration.version, migration.name)
                with conn.transaction():
                    conn.execute(sql.SQL(migration.sql))  # trusted file content, run as a script
                    # The migration may have switched role or search_path with `set local`.
                    conn.execute("reset role")
                    conn.execute(
                        "insert into public.schema_migrations (version, name, checksum) values (%s, %s, %s)",
                        (migration.version, migration.name, migration.checksum),
                    )
                applied_now.append(migration.version)
        finally:
            conn.execute("select pg_advisory_unlock(%s)", (_LOCK_KEY,))
    return applied_now
