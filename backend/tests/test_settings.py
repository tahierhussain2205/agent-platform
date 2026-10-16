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


@pytest.mark.parametrize("var", ["TEMPORAL_API_KEY", "MIGRATION_DATABASE_URL", "REDIS_URL"])
def test_empty_optional_secrets_are_unset(monkeypatch: pytest.MonkeyPatch, var: str) -> None:
    monkeypatch.setenv(var, "  ")
    assert getattr(Settings(), var.lower()) is None


def test_secrets_are_not_in_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEMPORAL_API_KEY", "tk-very-secret")
    monkeypatch.setenv("MIGRATION_DATABASE_URL", "postgresql://u:hunter2@h/db")
    text = repr(Settings()) + str(Settings().model_dump())
    assert "tk-very-secret" not in text
    assert "hunter2" not in text


def test_every_setting_is_documented_in_env_example() -> None:
    """`.env.example` is the reference for operators and Render's dashboard values."""
    import re
    from pathlib import Path

    example = (Path(__file__).parents[2] / ".env.example").read_text()
    documented = set(re.findall(r"^#? ?([A-Z][A-Z0-9_]+)=", example, re.MULTILINE))
    missing = {name.upper() for name in Settings.model_fields} - documented
    assert not missing, f"add to .env.example: {sorted(missing)}"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("vault-staging-abcd:10000", "http://vault-staging-abcd:10000"),  # Render's hostport
        ("http://vault:8100", "http://vault:8100"),
        ("https://vault.internal", "https://vault.internal"),
        ("", None),
    ],
)
def test_vault_url_accepts_a_host_and_port(
    monkeypatch: pytest.MonkeyPatch, value: str, expected: str
) -> None:
    monkeypatch.setenv("VAULT_URL", value)
    assert Settings().vault_url == expected
