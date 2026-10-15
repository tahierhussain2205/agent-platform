import threading
from pathlib import Path

import psycopg
import pytest

from app.db.migrate import MIGRATIONS_DIR, MigrationError, discover, migrate


def _write(directory: Path, name: str, body: str) -> None:
    (directory / name).write_text(body)


def _versions(url: str) -> list[str]:
    with psycopg.connect(url) as conn:
        return [r[0] for r in conn.execute("select version from public.schema_migrations order by version")]


# ------------------------------------------------------------------ discovery (no database)


def test_repository_migrations_are_valid() -> None:
    migrations = discover()
    assert migrations[0].version == "001"
    assert [m.version for m in migrations] == sorted(m.version for m in migrations)


def test_initial_migration_matches_spec_schema_apart_from_documented_patches() -> None:
    """001 is spec/schema.sql verbatim except: the top level begin/commit (the runner owns the
    transaction) and the role attribute block marked render-compat (managed Postgres)."""
    spec = (MIGRATIONS_DIR.parents[1] / "spec" / "schema.sql").read_text()
    ours = (MIGRATIONS_DIR / "001_initial_schema.sql").read_text()
    start, end = ours.index("-- [render-compat begin]"), ours.index("-- [render-compat end]\n")
    original_roles = (
        "alter role app_user          nobypassrls;\n"
        "alter role app_vault         nobypassrls;\n"
        "alter role app_identity_sync bypassrls;\n"
        "alter role app_maintenance   bypassrls;\n"
    )
    restored = ours[:start] + original_roles + ours[end + len("-- [render-compat end]\n") :]
    restored = restored.replace(
        "-- (transaction managed by the migration runner; see app/db/migrate.py)", "begin;"
    ).replace("-- (end of transaction)", "commit;")
    assert restored == spec


@pytest.mark.parametrize("name", ["1_short.sql", "001-dash.sql", "001_Upper.sql", "abc_name.sql"])
def test_bad_file_names_rejected(migrations_dir: Path, name: str) -> None:
    _write(migrations_dir, name, "select 1;")
    with pytest.raises(MigrationError, match="bad migration file name"):
        discover(migrations_dir)


def test_duplicate_versions_rejected(migrations_dir: Path) -> None:
    _write(migrations_dir, "001_a.sql", "select 1;")
    _write(migrations_dir, "001_b.sql", "select 1;")
    with pytest.raises(MigrationError, match="duplicate"):
        discover(migrations_dir)


@pytest.mark.parametrize("statement", ["begin;", "COMMIT;", "  rollback ;", "start transaction;"])
def test_top_level_transaction_control_rejected(migrations_dir: Path, statement: str) -> None:
    _write(migrations_dir, "001_a.sql", f"create table t (id int);\n{statement}\n")
    with pytest.raises(MigrationError, match="transaction control"):
        discover(migrations_dir)


def test_transaction_words_inside_plpgsql_are_allowed(migrations_dir: Path) -> None:
    # `begin` opening a plpgsql block is not followed by `;`.
    _write(migrations_dir, "001_a.sql", "do $$\nbegin\n  perform 1;\nend $$;\n")
    assert len(discover(migrations_dir)) == 1


# ------------------------------------------------------------------ applying (real Postgres)


def test_applies_initial_schema_then_is_a_no_op(empty_db: str) -> None:
    assert migrate(empty_db) == [m.version for m in discover()]
    assert migrate(empty_db) == []
    with psycopg.connect(empty_db) as conn:
        tables = conn.execute(
            "select count(*) from information_schema.tables where table_schema = 'app'"
        ).fetchone()
        assert tables is not None
        assert tables[0] > 30
        owner = conn.execute(
            "select nspowner::regrole::text from pg_namespace where nspname = 'app'"
        ).fetchone()
        assert owner == ("app_owner",)
        # The migration's `set local role` must not leak into the bookkeeping row's ownership.
        assert conn.execute("select current_user").fetchone() == ("postgres",)


def test_edited_migration_aborts(empty_db: str, migrations_dir: Path) -> None:
    _write(migrations_dir, "001_a.sql", "create table a (id int);")
    migrate(empty_db, migrations_dir)
    _write(migrations_dir, "001_a.sql", "create table a (id int, extra text);")
    with pytest.raises(MigrationError, match="edited after being applied"):
        migrate(empty_db, migrations_dir)


def test_failing_migration_rolls_back_and_is_not_recorded(empty_db: str, migrations_dir: Path) -> None:
    _write(migrations_dir, "001_ok.sql", "create table ok (id int);")
    _write(migrations_dir, "002_bad.sql", "create table half (id int);\nselect * from does_not_exist;")
    with pytest.raises(psycopg.errors.UndefinedTable):
        migrate(empty_db, migrations_dir)
    assert _versions(empty_db) == ["001"]
    with psycopg.connect(empty_db) as conn:
        assert conn.execute("select to_regclass('public.half')").fetchone() == (None,)
    # Fixing the file lets the next run apply it.
    _write(migrations_dir, "002_bad.sql", "create table half (id int);")
    assert migrate(empty_db, migrations_dir) == ["002"]


def test_pending_migrations_apply_in_order(empty_db: str, migrations_dir: Path) -> None:
    _write(migrations_dir, "001_a.sql", "create table a (id int);")
    migrate(empty_db, migrations_dir)
    _write(migrations_dir, "003_c.sql", "create table c (a_id int);")
    _write(migrations_dir, "002_b.sql", "alter table a add column name text;")
    assert migrate(empty_db, migrations_dir) == ["002", "003"]


def test_percent_signs_in_sql_are_not_treated_as_parameters(empty_db: str, migrations_dir: Path) -> None:
    _write(migrations_dir, "001_a.sql", "create table p as select format('%s-%L', 'x', 'y') as v;")
    migrate(empty_db, migrations_dir)
    with psycopg.connect(empty_db) as conn:
        assert conn.execute("select v from p").fetchone() == ("x-'y'",)


def test_concurrent_runs_serialize(empty_db: str, migrations_dir: Path) -> None:
    _write(migrations_dir, "001_slow.sql", "select pg_sleep(1);\ncreate table slow (id int);")
    results: list[list[str]] = []
    errors: list[BaseException] = []

    def run() -> None:
        try:
            results.append(migrate(empty_db, migrations_dir))
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=run) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert errors == []
    assert sorted(results) == [[], ["001"]]
    assert _versions(empty_db) == ["001"]
