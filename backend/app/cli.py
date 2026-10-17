"""Single entry point for every process: one image, many commands (Render rule 3)."""

import argparse
import asyncio
import sys
from collections.abc import Callable, Sequence

import uvicorn

from app.settings import get_settings


def _configure_logging(command: str) -> None:
    from app.logging import configure_logging

    settings = get_settings()
    configure_logging(settings.log_level, service=f"agent-{command}", json_output=settings.log_json)


def uvicorn_config() -> uvicorn.Config:
    settings = get_settings()
    return uvicorn.Config(
        "app.api.main:create_app",
        factory=True,
        host="0.0.0.0",
        port=settings.port,
        log_level=settings.log_level.lower(),
        proxy_headers=True,
        forwarded_allow_ips="*",
        timeout_graceful_shutdown=25,
        access_log=False,  # the request context middleware writes one structured line per request
        log_config=None,  # keep our JSON logging configuration
    )


def run_api(_: argparse.Namespace) -> int:
    # uvicorn handles SIGTERM: it stops accepting connections and drains in-flight requests.
    uvicorn.Server(uvicorn_config()).run()
    return 0


def run_vault(_: argparse.Namespace) -> int:
    """The token vault service, on the private network only."""
    from app.vault_service.app import create_vault_app

    settings = get_settings()
    if settings.vault_service_token is None:
        print("VAULT_SERVICE_TOKEN is required", file=sys.stderr)
        return 2
    config = uvicorn.Config(
        create_vault_app(settings),
        host="0.0.0.0",
        port=settings.port,
        log_level=settings.log_level.lower(),
        timeout_graceful_shutdown=25,
        access_log=False,
        log_config=None,
    )
    uvicorn.Server(config).run()
    return 0


def run_worker(_: argparse.Namespace) -> int:
    from app.worker import run_worker as _run

    return asyncio.run(_run(get_settings()))


def run_migrate(_: argparse.Namespace) -> int:
    from app.db.migrate import ensure_login_users, migrate

    settings = get_settings()
    url = settings.require_migration_database_url()
    applied = migrate(url)
    print(f"applied {len(applied)} migration(s): {', '.join(applied) or 'none pending'}")
    users = ensure_login_users(url, settings)
    print(f"login users ensured: {', '.join(users) or 'none configured'}")
    from app.agent.playbooks import sync_builtin_playbooks

    added = sync_builtin_playbooks(url)
    print(f"built in playbooks added: {', '.join(added) or 'none'}")
    return 0


def run_maintenance(_: argparse.Namespace) -> int:
    from app.maintenance import run_jobs

    return run_jobs(get_settings())


def run_seed(_: argparse.Namespace) -> int:
    from app.seed import seed

    if get_settings().env in ("staging", "production"):
        print("refusing to seed a staging or production database", file=sys.stderr)
        return 2
    settings = get_settings()
    envelope = None
    if settings.encryption_keys and settings.encryption_active_key:
        from app.crypto import Envelope, LocalKeyProvider

        envelope = Envelope(
            LocalKeyProvider.from_config(
                settings.encryption_keys.get_secret_value(), settings.encryption_active_key
            )
        )
    org_ids = seed(settings.require_migration_database_url(), envelope=envelope)
    print(f"seeded orgs: {', '.join(org_ids)}")
    return 0


def run_check(args: argparse.Namespace) -> int:
    from app import diagnostics

    try:
        if args.target == "temporal":
            print(asyncio.run(diagnostics.check_temporal(get_settings())))
        elif args.target == "vault":
            print(asyncio.run(diagnostics.check_vault(get_settings())))
        elif args.target == "model":
            print(asyncio.run(diagnostics.check_model(get_settings())))
        elif args.target == "storage":
            print(asyncio.run(diagnostics.check_storage(get_settings())))
        elif args.target == "sandbox":
            print(asyncio.run(diagnostics.check_sandbox(get_settings())))
    except Exception as exc:
        print(f"check {args.target} failed: {exc}", file=sys.stderr)
        return 1
    return 0


def run_offboard(args: argparse.Namespace) -> int:
    """Offboard an org (operators): it is no longer served; its data is deleted after the grace
    period by the maintenance job, or now with ``--now``."""
    from datetime import timedelta
    from uuid import UUID

    import psycopg

    from app.lifecycle import offboard

    settings = get_settings()
    org = UUID(args.org)
    with psycopg.connect(
        settings.component_dsn("maintenance"), autocommit=True, prepare_threshold=None
    ) as conn:
        with conn.transaction():
            row = conn.execute("select app.maintenance_begin_offboarding(%s)", (org,)).fetchone()
        print(f"org {org}: {'now offboarding' if row and row[0] else 'already offboarding or unknown'}")
        if not args.now:
            print(f"its data is deleted {settings.offboarding_grace_days} day(s) after offboarding began")
            return 0
        with conn.transaction():
            result = offboard(conn, settings, org, timedelta(0))
    print(
        f"deleted: {result.deleted}; schedules {result.schedules}, objects {result.objects}, "
        f"keys {result.keys}" + (f"; {', '.join(result.notes)}" if result.notes else "")
    )
    return 0 if result.deleted else 1


def run_org(args: argparse.Namespace) -> int:
    """Operator changes to an org: ``app org plan --org <id> --plan <key>`` moves it to another plan
    and brings its settings within the new plan's ceilings (audited)."""
    from uuid import UUID

    import psycopg

    settings = get_settings()
    org = UUID(args.org)
    with psycopg.connect(settings.component_dsn("maintenance"), prepare_threshold=None) as conn:
        plans = [r[0] for r in conn.execute("select key from app.plans order by key").fetchall()]
        if args.plan not in plans:
            print(f"unknown plan {args.plan!r}; plans: {', '.join(plans)}", file=sys.stderr)
            return 1
        row = conn.execute("select app.maintenance_set_plan(%s, %s)", (org, args.plan)).fetchone()
        conn.commit()
    if not row or not row[0]:
        print(f"org {org}: unknown", file=sys.stderr)
        return 1
    print(f"org {org}: now on the {args.plan} plan; settings above its ceilings were lowered")
    return 0


def run_eval(args: argparse.Namespace) -> int:
    from pathlib import Path

    from app.eval import CASES
    from app.eval import run_eval as evaluate

    cases = Path(args.cases) if args.cases else CASES
    return evaluate(get_settings(), cases, Path(args.report) if args.report else None, args.min_score)


COMMANDS: dict[str, tuple[str, Callable[[argparse.Namespace], int]]] = {
    "api": ("Run the HTTP API (Slack ingress, console API, health).", run_api),
    "worker": ("Run the Temporal workers.", run_worker),
    "vault": ("Run the token vault service (private network only).", run_vault),
    "maintenance": ("Run maintenance jobs (partitions, retention, reconciliation).", run_maintenance),
    "migrate": ("Apply database migrations.", run_migrate),
    "seed": ("Load local development data (refused on staging and production).", run_seed),
    "check": ("Check connectivity to a dependency.", run_check),
    "eval": ("Replay the eval cases through the agent and score them.", run_eval),
    "offboard": (
        "Offboard an org: stop serving it, delete its data (after the grace period, or --now).",
        run_offboard,
    ),
    "org": ("Operator changes to an org (plan).", run_org),
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="app", description="Agent platform backend")
    sub = parser.add_subparsers(dest="command", required=True, metavar="command")
    for name, (help_text, handler) in COMMANDS.items():
        cmd = sub.add_parser(name, help=help_text)
        cmd.set_defaults(handler=handler)
        if name == "check":
            cmd.add_argument("target", choices=["temporal", "vault", "model", "storage", "sandbox"])
        if name == "offboard":
            cmd.add_argument("--org", required=True, help="the org's id")
            cmd.add_argument(
                "--now", action="store_true", help="delete its data now instead of after the grace period"
            )
        if name == "org":
            cmd.add_argument("action", choices=["plan"])
            cmd.add_argument("--org", required=True, help="the org's id")
            cmd.add_argument("--plan", required=True, help="the plan's key (free, team, enterprise)")
        if name == "eval":
            cmd.add_argument("--cases", help="directory of case files (default: the built in cases)")
            cmd.add_argument("--report", help="write a JSON report to this file")
            cmd.add_argument("--min-score", type=float, default=0.8, help="mean score needed to pass")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _configure_logging(args.command)
    handler: Callable[[argparse.Namespace], int] = args.handler
    return handler(args)
