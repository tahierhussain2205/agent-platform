"""Built in playbooks: versioned Markdown instructions plus metadata in code (spec 7.1).

Each ``<key>.v<version>.md`` file has a small front matter block (key, version, title,
description, and the operations the playbook may load as ``adapter.operation`` lists) followed by
the instructions. Old versions stay in this directory: a task pins the version it started with.

``sync_builtin_playbooks`` runs at deploy time (``app migrate``) as the table owner and inserts
versions the database does not have yet. Versions are immutable: changing a released file without
bumping its version is refused.
"""

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg

log = logging.getLogger(__name__)
DIRECTORY = Path(__file__).parent


class PlaybookError(Exception):
    pass


@dataclass(frozen=True)
class PlaybookSpec:
    key: str
    version: int
    title: str
    description: str
    instructions: str
    read: tuple[str, ...]
    write: tuple[str, ...]

    def allows(self, adapter: str, operation: str, tier: str, is_mcp: bool = False) -> bool:
        """Whether an operation may be loaded: ``adapter.operation``, ``adapter.*`` (every operation of
        that tier), or ``mcp.*`` (any MCP server's). Reads come from ``read``, writes from ``write``."""
        for entry in self.read if tier == "read" else self.write:
            name, _, op = entry.partition(".")
            if (name == adapter or (name == "mcp" and is_mcp)) and op in ("*", operation):
                return True
        return False

    def operations(self, adapter: str, kind: str = "read") -> list[str]:
        """Operation names of one adapter the playbook may load (``read`` or ``write``)."""
        wanted = self.read if kind == "read" else self.write
        return [op.split(".", 1)[1] for op in wanted if op.split(".", 1)[0] == adapter]


def parse(text: str) -> PlaybookSpec:
    if not text.startswith("---\n"):
        raise PlaybookError("missing front matter")
    header, _, body = text[4:].partition("\n---\n")
    meta: dict[str, str] = {}
    for line in header.splitlines():
        if line.strip():
            name, _, value = line.partition(":")
            meta[name.strip()] = value.strip()
    try:
        return PlaybookSpec(
            key=meta["key"],
            version=int(meta["version"]),
            title=meta["title"],
            description=meta.get("description", ""),
            instructions=body.strip(),
            read=tuple(v.strip() for v in meta.get("read", "").split(",") if v.strip()),
            write=tuple(v.strip() for v in meta.get("write", "").split(",") if v.strip()),
        )
    except (KeyError, ValueError) as exc:
        raise PlaybookError(f"bad front matter: {exc}") from exc


def load(directory: Path = DIRECTORY) -> dict[tuple[str, int], PlaybookSpec]:
    specs: dict[tuple[str, int], PlaybookSpec] = {}
    for path in sorted(directory.glob("*.v*.md")):
        spec = parse(path.read_text())
        if path.name != f"{spec.key}.v{spec.version}.md":
            raise PlaybookError(f"{path.name} does not match its front matter ({spec.key} v{spec.version})")
        specs[(spec.key, spec.version)] = spec
    return specs


BUILTIN = load()


def latest(key: str, specs: dict[tuple[str, int], PlaybookSpec] = BUILTIN) -> PlaybookSpec:
    versions = [s for (k, _), s in specs.items() if k == key]
    if not versions:
        raise PlaybookError(f"no playbook {key}")
    return max(versions, key=lambda s: s.version)


def keys(specs: dict[tuple[str, int], PlaybookSpec] = BUILTIN) -> list[str]:
    return sorted({k for k, _ in specs})


def sync_builtin_playbooks(
    database_url: str, specs: dict[tuple[str, int], PlaybookSpec] | None = None
) -> list[str]:
    """Insert built in playbook versions the database does not have. Returns ``key@version``s added."""
    added: list[str] = []
    with psycopg.connect(database_url) as conn:
        conn.execute("set local role app_owner")
        for spec in (specs or BUILTIN).values():
            existing: Any = conn.execute(
                "select title, description, instructions from app.playbooks "
                "where org_id is null and key = %s and version = %s",
                (spec.key, spec.version),
            ).fetchone()
            if existing is not None:
                if tuple(existing) != (spec.title, spec.description, spec.instructions):
                    raise PlaybookError(
                        f"built in playbook {spec.key} v{spec.version} changed; release it as a new version"
                    )
                continue
            conn.execute(
                "insert into app.playbooks (org_id, key, version, title, description, instructions) "
                "values (null, %s, %s, %s, %s, %s)",
                (spec.key, spec.version, spec.title, spec.description, spec.instructions),
            )
            added.append(f"{spec.key}@{spec.version}")
        conn.commit()
    return added
