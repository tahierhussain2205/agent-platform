import os
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import psycopg
import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from psycopg import sql

from app.settings import get_settings
from app.telemetry import configure_tracing

# Tracing is configured once per process; do it before any test builds an app so every span is
# captured here (tests/api/test_observability.py reads them).
SPANS = InMemorySpanExporter()
configure_tracing("agent-test", None, "test", extra_exporters=[SPANS])

# The populated two-org database (`world`) and helpers, shared by every database-backed test.
pytest_plugins = ["tests.world"]

# Admin connection to a Postgres server used for throwaway test databases.
# Local: the Compose Postgres (`make infra`). CI: the Postgres service container.
ADMIN_URL = os.environ.get(
    "TEST_DATABASE_ADMIN_URL", "postgresql://postgres:postgres@localhost:55432/postgres"
)


@pytest.fixture(autouse=True)
def _fresh_settings() -> Iterator[None]:
    """Settings are cached per process; clear the cache so env changes in a test apply."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _postgres_available() -> bool:
    try:
        with psycopg.connect(ADMIN_URL, connect_timeout=2):
            return True
    except psycopg.OperationalError:
        return False


def database_url(name: str) -> str:
    """ADMIN_URL with the database name replaced."""
    base, _, _ = ADMIN_URL.rpartition("/")
    return f"{base}/{name}"


@contextmanager
def throwaway_database(prefix: str = "test") -> Iterator[str]:
    """Create an empty database, yield its URL, drop it afterwards."""
    name = f"{prefix}_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(ADMIN_URL, autocommit=True) as conn:
        conn.execute(sql.SQL("create database {}").format(sql.Identifier(name)))
    try:
        yield database_url(name)
    finally:
        with psycopg.connect(ADMIN_URL, autocommit=True) as conn:
            conn.execute(sql.SQL("drop database if exists {} with (force)").format(sql.Identifier(name)))


@pytest.fixture(scope="session")
def postgres() -> None:
    if not _postgres_available():
        pytest.skip(f"Postgres not reachable at {ADMIN_URL} (run `make infra`)")


@pytest.fixture
def empty_db(postgres: None) -> Iterator[str]:
    with throwaway_database() as url:
        yield url


@pytest.fixture(scope="session")
def migrated_db(postgres: None) -> Iterator[str]:
    """One database per test session with every migration applied (read mostly tests)."""
    from app.db.migrate import migrate

    with throwaway_database("migrated") as url:
        migrate(url)
        yield url


@pytest.fixture
def migrations_dir(tmp_path: Path) -> Path:
    path = tmp_path / "migrations"
    path.mkdir()
    return path
