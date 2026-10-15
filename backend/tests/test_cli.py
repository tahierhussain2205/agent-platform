import subprocess
import sys

import pytest

from app import cli


def test_help_lists_every_command() -> None:
    out = subprocess.run(
        [sys.executable, "-m", "app", "--help"], capture_output=True, text=True, check=True
    ).stdout
    for name in ("api", "worker", "maintenance", "migrate", "seed"):
        assert name in out


def test_missing_command_fails() -> None:
    result = subprocess.run([sys.executable, "-m", "app"], capture_output=True, text=True, check=False)
    assert result.returncode != 0


@pytest.mark.parametrize("command", ["worker", "maintenance", "migrate", "seed"])
def test_placeholders_exit_nonzero_with_message(command: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main([command]) == 2
    assert "not implemented yet" in capsys.readouterr().err


def test_api_binds_all_interfaces_on_port_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PORT", "12345")
    config = cli.uvicorn_config()
    assert config.host == "0.0.0.0"
    assert config.port == 12345
    assert config.factory is True
