import subprocess
import sys

import pytest

from app import cli


def test_help_lists_every_command() -> None:
    out = subprocess.run(
        [sys.executable, "-m", "app", "--help"], capture_output=True, text=True, check=True
    ).stdout
    for name in ("api", "worker", "maintenance", "migrate", "seed", "check"):
        assert name in out


def test_missing_command_fails() -> None:
    result = subprocess.run([sys.executable, "-m", "app"], capture_output=True, text=True, check=False)
    assert result.returncode != 0


def test_api_binds_all_interfaces_on_port_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PORT", "12345")
    config = cli.uvicorn_config()
    assert config.host == "0.0.0.0"
    assert config.port == 12345
    assert config.factory is True


def test_migrate_requires_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MIGRATION_DATABASE_URL", raising=False)
    with pytest.raises(RuntimeError, match="MIGRATION_DATABASE_URL"):
        cli.main(["migrate"])


def test_check_reports_failure(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("TEMPORAL_ADDRESS", "127.0.0.1:1")
    assert cli.main(["check", "temporal"]) == 1
    assert "check temporal failed" in capsys.readouterr().err
