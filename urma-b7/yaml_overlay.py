"""Small mapping-only YAML scalar overlay used by the B7 harness.

Dragonfly's checked-in/operator configs are ordinary block mappings. This
module deliberately rejects sequences, tabs and inline mapping parents rather
than pretending to be a general YAML parser.
"""

from __future__ import annotations

import json
import re
from typing import Any


KEY_RE = re.compile(r"^(?P<indent> *)(?P<key>[A-Za-z_][A-Za-z0-9_-]*):(?P<rest>.*)$")


class OverlayError(ValueError):
    pass


def scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    raise OverlayError(f"unsupported scalar type: {type(value).__name__}")


def _mapping_entries(lines: list[str]) -> list[tuple[int, int, str, str]]:
    entries = []
    for index, line in enumerate(lines):
        if "\t" in line[: len(line) - len(line.lstrip())]:
            raise OverlayError("tabs are not supported in YAML indentation")
        match = KEY_RE.match(line)
        if match:
            entries.append((index, len(match.group("indent")), match.group("key"), match.group("rest")))
    return entries


def _find_child(
    entries: list[tuple[int, int, str, str]],
    key: str,
    indent: int,
    start: int,
    end: int,
) -> tuple[int, int, str, str] | None:
    for entry in entries:
        line_index, entry_indent, entry_key, _ = entry
        if line_index < start or line_index >= end:
            continue
        if entry_indent == indent and entry_key == key:
            return entry
    return None


def _block_end(entries: list[tuple[int, int, str, str]], line_index: int, indent: int, total: int) -> int:
    for candidate, candidate_indent, _, _ in entries:
        if candidate > line_index and candidate_indent <= indent:
            return candidate
    return total


def set_path(document: str, path: tuple[str, ...], value: Any) -> str:
    if not path:
        raise OverlayError("overlay path is empty")
    lines = document.splitlines()
    entries = _mapping_entries(lines)
    start, end, indent = 0, len(lines), 0

    for depth, key in enumerate(path):
        child = _find_child(entries, key, indent, start, end)
        is_leaf = depth == len(path) - 1
        if child is None:
            addition = []
            for offset, missing_key in enumerate(path[depth:]):
                missing_leaf = depth + offset == len(path) - 1
                suffix = f": {scalar(value)}" if missing_leaf else ":"
                addition.append(" " * (indent + offset * 2) + missing_key + suffix)
            lines[end:end] = addition
            return "\n".join(lines) + "\n"

        line_index, _, _, rest = child
        if is_leaf:
            lines[line_index] = " " * indent + f"{key}: {scalar(value)}"
            return "\n".join(lines) + "\n"
        if rest.strip() not in ("", "{}"):
            raise OverlayError(f"YAML parent {'.'.join(path[: depth + 1])} is not a block mapping")
        if rest.strip() == "{}":
            lines[line_index] = " " * indent + f"{key}:"
        start = line_index + 1
        end = _block_end(entries, line_index, indent, len(lines))
        indent += 2

    raise AssertionError("unreachable")


def apply(document: str, overlays: dict[tuple[str, ...], Any]) -> str:
    result = document
    for path, value in overlays.items():
        result = set_path(result, path, value)
    return result
