"""Single entry point for every process: one image, many commands (Render rule 3)."""

import argparse
import sys
from collections.abc import Callable, Sequence

import uvicorn

from app.settings import get_settings


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


def _placeholder(command: str, phase: int) -> Callable[[argparse.Namespace], int]:
    def run(_: argparse.Namespace) -> int:
        print(f"'{command}' is not implemented yet (arrives in phase {phase}).", file=sys.stderr)
        return 2

    return run


COMMANDS: dict[str, tuple[str, Callable[[argparse.Namespace], int]]] = {
    "api": ("Run the HTTP API (Slack ingress, console API, health).", run_api),
    "worker": ("Run the Temporal workers.", _placeholder("worker", 1)),
    "maintenance": (
        "Run maintenance jobs (partitions, retention, reconciliation).",
        _placeholder("maintenance", 3),
    ),
    "migrate": ("Apply database migrations.", _placeholder("migrate", 3)),
    "seed": ("Load local development data.", _placeholder("seed", 1)),
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="app", description="Agent platform backend")
    sub = parser.add_subparsers(dest="command", required=True, metavar="command")
    for name, (help_text, handler) in COMMANDS.items():
        cmd = sub.add_parser(name, help=help_text)
        cmd.set_defaults(handler=handler)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    handler: Callable[[argparse.Namespace], int] = args.handler
    return handler(args)
