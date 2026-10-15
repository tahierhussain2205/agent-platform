"""Adapters by key, and model tool names back to (adapter, operation)."""

from dataclasses import dataclass, field

from app.tools.base import Operation, ToolAdapter, tool_name


@dataclass
class Registry:
    adapters: dict[str, ToolAdapter] = field(default_factory=dict)

    def register(self, adapter: ToolAdapter) -> None:
        self.adapters[adapter.key] = adapter

    def lookup(self, name: str) -> tuple[ToolAdapter, Operation] | None:
        """The adapter and operation behind a model tool name such as ``slack_read_channel``."""
        for adapter in self.adapters.values():
            for operation in adapter.operations.values():
                if tool_name(adapter.key, operation.name) == name:
                    return adapter, operation
        return None
