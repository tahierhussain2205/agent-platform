"""render.yaml: schema validity plus the rules the schema cannot express."""

import copy
import json
import re
import shlex
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator

from app import cli
from app.api.main import create_app
from app.settings import Settings

ROOT = Path(__file__).resolve().parents[2]
BLUEPRINT: dict[str, Any] = yaml.safe_load((ROOT / "render.yaml").read_text())
SCHEMA = json.loads((ROOT / "dev" / "schemas" / "render.schema.json").read_text())
SECRET_NAME = re.compile(r"(SECRET|PASSWORD|TOKEN|API_KEY|PRIVATE|DATABASE_URL|REDIS_URL)")


def _staging() -> dict[str, Any]:
    project = next(p for p in BLUEPRINT["projects"] if p["name"] == "agent-platform-staging")
    env: dict[str, Any] = next(e for e in project["environments"] if e["name"] == "staging")
    return env


STAGING = _staging()
SERVICES = {s["name"]: s for s in STAGING["services"]}
DATABASES = {d["name"]: d for d in STAGING["databases"]}
GROUPS = {g["name"]: g for g in STAGING["envVarGroups"]}
BACKEND = [s for s in STAGING["services"] if s.get("runtime") == "docker"]


def _env_keys(service: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """All env vars a service ends up with, including those from groups."""
    out: dict[str, dict[str, Any]] = {}
    for var in service.get("envVars", []):
        if "fromGroup" in var:
            for gvar in GROUPS[var["fromGroup"]]["envVars"]:
                out[gvar["key"]] = gvar
        else:
            out[var["key"]] = var
    return out


# ------------------------------------------------------------------ schema


def test_validates_against_render_schema() -> None:
    errors = sorted(Draft202012Validator(SCHEMA).iter_errors(BLUEPRINT), key=str)
    assert errors == [], "\n".join(e.message for e in errors)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("services", 0, "plan"), "gigantic"),
        (("services", 0, "unknownField"), True),
        (("databases", 0, "postgresMajorVersion"), "9"),
        (("services", 0, "maxShutdownDelaySeconds"), 301),
    ],
)
def test_schema_catches_mistakes(path: tuple[Any, ...], value: Any) -> None:
    broken = copy.deepcopy(BLUEPRINT)
    target = next(e for p in broken["projects"] for e in p["environments"] if e["name"] == "staging")
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    assert list(Draft202012Validator(SCHEMA).iter_errors(broken)), f"schema accepted {path}={value!r}"


# ------------------------------------------------------------------ structure


def test_every_plan_resource_exists() -> None:
    assert set(SERVICES) == {
        "api-staging",
        "worker-staging",
        "maintenance-staging",
        "web-staging",
        "kv-staging",
    }
    assert set(DATABASES) == {"db-staging"}
    assert SERVICES["api-staging"]["type"] == "web"
    assert SERVICES["worker-staging"]["type"] == "worker"
    assert SERVICES["maintenance-staging"]["type"] == "cron"
    assert SERVICES["web-staging"]["runtime"] == "static"
    assert SERVICES["kv-staging"]["type"] == "keyvalue"


def test_names_are_unique_and_suffixed_with_environment() -> None:
    names = [r["name"] for r in STAGING["services"] + STAGING["databases"] + STAGING["envVarGroups"]]
    assert len(names) == len(set(names))
    assert all(n.endswith("-staging") for n in names)


def test_production_project_exists_and_is_empty() -> None:
    project = next(p for p in BLUEPRINT["projects"] if p["name"] == "agent-platform-production")
    (env,) = project["environments"]
    assert env["name"] == "production"
    assert env["permissions"]["protection"] == "enabled"
    assert not any(env.get(k) for k in ("services", "databases", "envVarGroups"))


def test_previews_are_manual_and_expire() -> None:
    assert BLUEPRINT["previews"]["generation"] == "manual"
    assert 1 <= BLUEPRINT["previews"]["expireAfterDays"] <= 7


# ------------------------------------------------------------------ backend services


@pytest.mark.parametrize("service", BACKEND, ids=lambda s: s["name"])
def test_backend_services_share_one_image(service: dict[str, Any]) -> None:
    assert service["dockerContext"] == "./backend"
    assert service["dockerfilePath"] == "./backend/Dockerfile"
    assert (ROOT / service["dockerfilePath"]).is_file()
    assert service["branch"] == "main"
    assert service["autoDeployTrigger"] == "checksPass"
    assert "backend/**" in service["buildFilter"]["paths"]


def _subcommand(command: str) -> str:
    argv = shlex.split(command)
    assert argv[0] == "app", f"{command!r} must start with the `app` entry point"
    assert len(argv) == 2, f"{command!r} should be `app <subcommand>`"
    return argv[1]


@pytest.mark.parametrize(
    ("service", "field", "expected"),
    [
        ("api-staging", "dockerCommand", "api"),
        ("api-staging", "preDeployCommand", "migrate"),
        ("worker-staging", "dockerCommand", "worker"),
        ("maintenance-staging", "dockerCommand", "maintenance"),
    ],
)
def test_commands_exist_in_cli(service: str, field: str, expected: str) -> None:
    sub = _subcommand(SERVICES[service][field])
    assert sub == expected
    assert sub in cli.COMMANDS


def test_health_check_path_is_served_by_the_app() -> None:
    path = SERVICES["api-staging"]["healthCheckPath"]
    assert TestClient(create_app()).get(path).status_code == 200


def test_shutdown_delays_cover_graceful_drain() -> None:
    worker = SERVICES["worker-staging"]
    drain = int(_env_keys(worker)["WORKER_GRACEFUL_SHUTDOWN_SECONDS"]["value"])
    assert worker["maxShutdownDelaySeconds"] >= drain + 10
    assert SERVICES["api-staging"]["maxShutdownDelaySeconds"] > cli.uvicorn_config().timeout_graceful_shutdown


SETTINGS_KEYS = {name.upper() for name in Settings.model_fields}


@pytest.mark.parametrize("service", BACKEND, ids=lambda s: s["name"])
def test_backend_env_vars_are_known_settings(service: dict[str, Any]) -> None:
    unknown = set(_env_keys(service)) - SETTINGS_KEYS
    assert unknown == set(), f"{service['name']} sets env vars the code never reads: {unknown}"


@pytest.mark.parametrize("service", BACKEND, ids=lambda s: s["name"])
def test_backend_env_is_staging(service: dict[str, Any]) -> None:
    assert _env_keys(service)["ENV"]["value"] == "staging"


@pytest.mark.parametrize("service", STAGING["services"], ids=lambda s: s["name"])
def test_secrets_are_never_literal(service: dict[str, Any]) -> None:
    for key, var in _env_keys(service).items():
        if SECRET_NAME.search(key):
            assert "value" not in var, f"{key} must be sync: false or a reference, never a literal"


def test_groups_hold_no_secrets() -> None:
    for group in GROUPS.values():
        for var in group["envVars"]:
            assert not SECRET_NAME.search(var["key"]), var["key"]


def test_references_resolve_within_the_environment() -> None:
    for service in STAGING["services"]:
        for var in service.get("envVars", []):
            if "fromDatabase" in var:
                assert var["fromDatabase"]["name"] in DATABASES
            if "fromService" in var:
                ref = var["fromService"]
                assert ref["name"] in SERVICES
                assert SERVICES[ref["name"]]["type"] == ref["type"]
            if "fromGroup" in var:
                assert var["fromGroup"] in GROUPS


def test_temporal_cloud_settings_are_prompted_secrets() -> None:
    for name in ("api-staging", "worker-staging"):
        env = _env_keys(SERVICES[name])
        for key in ("TEMPORAL_ADDRESS", "TEMPORAL_NAMESPACE", "TEMPORAL_API_KEY"):
            assert env[key].get("sync") is False, f"{name}.{key}"


def test_migrations_use_the_direct_connection_not_the_pool() -> None:
    for name in ("api-staging", "maintenance-staging"):
        ref = _env_keys(SERVICES[name])["MIGRATION_DATABASE_URL"]["fromDatabase"]
        assert ref["property"] == "connectionString"


# ------------------------------------------------------------------ data stores


def test_postgres() -> None:
    db = DATABASES["db-staging"]
    assert int(db["postgresMajorVersion"]) >= 16
    assert db["connectionPool"] == "pgbouncer"
    assert db["ipAllowList"] == []


def test_key_value_is_private_and_never_evicts() -> None:
    kv = SERVICES["kv-staging"]
    assert kv["ipAllowList"] == []
    assert kv["maxmemoryPolicy"] == "noeviction"


# ------------------------------------------------------------------ static site


def test_static_site() -> None:
    web = SERVICES["web-staging"]
    assert web["staticPublishPath"] == "./web/dist"
    assert {"type": "rewrite", "source": "/*", "destination": "/index.html"} in web["routes"]
    assert "--frozen-lockfile" in web["buildCommand"]
    headers = {(h["path"], h["name"]) for h in web["headers"]}
    for name in ("X-Frame-Options", "X-Content-Type-Options", "Referrer-Policy", "Strict-Transport-Security"):
        assert ("/*", name) in headers
    assert _env_keys(web)["VITE_API_URL"].get("sync") is False
