"""Single entry point for every process: one image, many commands (Render rule 3)."""

import argparse
import asyncio
import logging
import sys
from collections.abc import Callable, Sequence

import uvicorn

from app.settings import get_settings


def _configure_logging() -> None:
    # Replaced by structured JSON logging in phase 4.
    logging.basicConfig(
        level=get_settings().log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )


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
    )


def run_api(_: argparse.Namespace) -> int:
    # uvicorn handles SIGTERM: it stops accepting connections and drains in-flight requests.
    uvicorn.Server(uvicorn_config()).run()
    return 0


def run_worker(_: argparse.Namespace) -> int:
    from app.worker import run_worker as _run

    return asyncio.run(_run(get_settings()))


def run_migrate(_: argparse.Namespace) -> int:
    from app.db.migrate import migrate

    applied = migrate(get_settings().require_migration_database_url())
    print(f"applied {len(applied)} migration(s): {', '.join(applied) or 'none pending'}")
    return 0


def run_seed(_: argparse.Namespace) -> int:
    from app.seed import seed

    if get_settings().env in ("staging", "production"):
        print("refusing to seed a staging or production database", file=sys.stderr)
        return 2
    org_ids = seed(get_settings().require_migration_database_url())
    print(f"seeded orgs: {', '.join(org_ids)}")
    return 0


def run_check(args: argparse.Namespace) -> int:
    from app import diagnostics

    try:
        if args.target == "temporal":
            print(asyncio.run(diagnostics.check_temporal(get_settings())))
    except Exception as exc:
        print(f"check {args.target} failed: {exc}", file=sys.stderr)
        return 1
    return 0


def _placeholder(command: str, phase: int) -> Callable[[argparse.Namespace], int]:
    def run(_: argparse.Namespace) -> int:
        print(f"'{command}' is not implemented yet (arrives in phase {phase}).", file=sys.stderr)
        return 2

    return run


COMMANDS: dict[str, tuple[str, Callable[[argparse.Namespace], int]]] = {
    "api": ("Run the HTTP API (Slack ingress, console API, health).", run_api),
    "worker": ("Run the Temporal workers.", run_worker),
    "maintenance": (
        "Run maintenance jobs (partitions, retention, reconciliation).",
        _placeholder("maintenance", 3),
    ),
    "migrate": ("Apply database migrations.", run_migrate),
    "seed": ("Load local development data (refused on staging and production).", run_seed),
    "check": ("Check connectivity to a dependency.", run_check),
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="app", description="Agent platform backend")
    sub = parser.add_subparsers(dest="command", required=True, metavar="command")
    for name, (help_text, handler) in COMMANDS.items():
        cmd = sub.add_parser(name, help=help_text)
        cmd.set_defaults(handler=handler)
        if name == "check":
            cmd.add_argument("target", choices=["temporal"])
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _configure_logging()
    handler: Callable[[argparse.Namespace], int] = args.handler
    return handler(args)
