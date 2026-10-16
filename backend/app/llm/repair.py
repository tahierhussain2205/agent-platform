"""Tool inputs as their schema says. A model's tool call sometimes comes back with an array or an object
written as a JSON string, or with the markup of the call itself left at the end of a string
("…the answer.</answer>\n</invoke>"). Both are put right here, guided by the tool's input schema, so a
step never reads a string where it expects a list (and finds nothing in it) and people never see the
tags. What cannot be put right is left as it came: the step that reads it decides."""

import json
import re
from typing import Any

# The model's own tool call markup: never part of what it meant to write.
MARKUP = r"</?(?:invoke|parameter|function_calls|antml:[\w-]+)\b[^>]*>"
DECODE = json.JSONDecoder()


def repair_content(
    content: list[dict[str, Any]], tools: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[str]]:
    """The content with every tool call's input repaired, and the fields that were (names only)."""
    schemas = {t.get("name"): t.get("input_schema") or {} for t in tools}
    fixed: list[str] = []
    out = []
    for block in content:
        name = block.get("name")
        if block.get("type") == "tool_use" and name in schemas and isinstance(block.get("input"), dict):
            block = {**block, "input": _repair(block["input"], schemas[name], "", fixed, str(name))}
        out.append(block)
    return out, fixed


def cut_markup(text: str, field: str = "") -> str:
    """``text`` up to the first tag of tool call markup (or the closing tag of its own field)."""
    own = rf"|</{re.escape(field)}>" if field else ""
    match = re.search(MARKUP + own, text, re.IGNORECASE)
    return text[: match.start()].rstrip() if match else text


def _repair(value: Any, schema: dict[str, Any], field: str, fixed: list[str], path: str) -> Any:
    kind = schema.get("type")
    if isinstance(value, str):
        if kind in ("array", "object"):
            decoded = _decode(value, kind)
            if decoded is None:
                return value
            fixed.append(path)
            value = decoded
        elif kind in ("number", "integer", "boolean"):
            return _scalar(value, kind, fixed, path)
        else:
            cut = cut_markup(value, field)
            if cut != value:
                fixed.append(path)
            return cut
    if kind == "array" and isinstance(value, dict) and (schema.get("items") or {}).get("type") == "object":
        fixed.append(path)  # one item given on its own
        value = [value]
    if isinstance(value, list):
        items = schema.get("items") or {}
        return [_repair(v, items, field, fixed, f"{path}[]") for v in value]
    if isinstance(value, dict):
        props = schema.get("properties") or {}
        return {k: _repair(v, props.get(k) or {}, k, fixed, f"{path}.{k}") for k, v in value.items()}
    return value


def _decode(text: str, kind: str) -> Any:
    try:
        value, _ = DECODE.raw_decode(text.strip())  # what follows the value is markup at best
    except ValueError:
        return None
    if kind == "array" and isinstance(value, dict):
        return [value]
    return value if isinstance(value, list if kind == "array" else dict) else None


def _scalar(text: str, kind: str, fixed: list[str], path: str) -> Any:
    word = cut_markup(text).strip()
    try:
        if kind == "boolean":
            if word.lower() not in ("true", "false"):
                return text
            value: Any = word.lower() == "true"
        else:
            value = int(word) if kind == "integer" else float(word)
    except ValueError:
        return text
    fixed.append(path)
    return value
