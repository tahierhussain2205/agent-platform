"""A small JSON document store: in memory, persisted to a file after every change."""

import json
import threading
from pathlib import Path
from typing import Any


class Store:
    def __init__(self, path: Path | None) -> None:
        self.path = path
        self.lock = threading.RLock()
        self.data: dict[str, Any] = {}

    def load(self) -> bool:
        if self.path and self.path.exists():
            self.data = json.loads(self.path.read_text())
            return True
        return False

    def save(self) -> None:
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, indent=1, default=str))
            tmp.replace(self.path)
