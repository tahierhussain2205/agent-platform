import pytest
from pydantic import ValidationError

from app.settings import Settings, get_settings


def test_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("PORT", "ENV", "LOG_LEVEL"):
        monkeypatch.delenv(var, raising=False)
    s = Settings()
    assert s.port == 8000
    assert s.env == "local"


def test_port_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PORT", "10000")
    assert get_settings().port == 10000


def test_invalid_env_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENV", "prod")
    with pytest.raises(ValidationError):
        Settings()


def test_invalid_port_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PORT", "70000")
    with pytest.raises(ValidationError):
        Settings()
