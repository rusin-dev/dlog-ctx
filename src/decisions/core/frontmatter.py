"""Minimal YAML frontmatter reader/writer with no third-party dependency.

Only the YAML subset used by this project is supported:

    ---
    title: SQLite instead of Postgres
    date: 2026-10-24
    status: accepted           # trailing comment
    files:
      - src/storage.py
      - "src/a b.py"
    tags: [storage, database]  # inline list
    rejected:
      - Postgres (deployment cost)
    ---

    Body text...

Serialisation is normalised (block lists, quotes added only when required) so
that rewriting a file is idempotent.
"""

from __future__ import annotations

import datetime as _dt
import re
from typing import Any

DELIM = "---"

_KEY_RE = re.compile(r"^([A-Za-z0-9_.-]+)\s*:\s*(.*)$")
_ITEM_RE = re.compile(r"^\s*-\s+(.*)$")

#: Serialisation order for known fields; unknown fields are appended afterwards.
FIELD_ORDER = [
    "title",
    "date",
    "status",
    "commit",
    "branch",
    "version",
    "files",
    "symbols",
    "tags",
    "rejected",
    "supersedes",
]


class FrontmatterError(ValueError):
    """Raised when the frontmatter block cannot be parsed."""


def split(text: str) -> tuple[str | None, str]:
    """Split raw frontmatter from the body; returns ``(None, text)`` if absent."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != DELIM:
        return None, text
    for idx in range(1, len(lines)):
        if lines[idx].strip() == DELIM:
            return "\n".join(lines[1:idx]), "\n".join(lines[idx + 1 :])
    return None, text


def parse(text: str) -> tuple[dict[str, Any], str]:
    """Parse a Markdown document with frontmatter into ``(meta, body)``."""
    head, body = split(text)
    if head is None:
        return {}, text
    return parse_block(head), body.lstrip("\n")


def parse_block(head: str) -> dict[str, Any]:
    meta: dict[str, Any] = {}
    current: str | None = None
    for raw in head.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        stripped = raw.lstrip()
        if not raw.startswith((" ", "\t")) and not stripped.startswith("-"):
            match = _KEY_RE.match(raw)
            if not match:
                raise FrontmatterError(f"cannot parse frontmatter line: {raw!r}")
            current = match.group(1)
            raw_value = match.group(2)
            if not _strip_comment(raw_value).strip():
                meta[current] = None  # may become a block list
            else:
                meta[current] = _scalar(raw_value)
            continue
        item = _ITEM_RE.match(raw)
        if item is None or current is None:
            raise FrontmatterError(f"cannot parse frontmatter line: {raw!r}")
        if meta.get(current) is None:
            meta[current] = []
        if not isinstance(meta[current], list):
            raise FrontmatterError(f"field {current!r} is both a scalar and a list")
        meta[current].append(_scalar(item.group(1)))
    return {key: ("" if value is None else value) for key, value in meta.items()}


def dump(meta: dict[str, Any], body: str) -> str:
    """Serialise ``meta``/``body`` into Markdown (result ends with a newline)."""
    lines: list[str] = [DELIM]
    keys = [k for k in FIELD_ORDER if k in meta]
    keys += sorted(k for k in meta if k not in FIELD_ORDER)
    for key in keys:
        value = meta[key]
        if value is None or value == "" or value == []:
            continue
        if isinstance(value, (list, tuple)):
            lines.append(f"{key}:")
            for item in value:
                lines.append(f"  - {_quote(str(item))}")
        else:
            lines.append(f"{key}: {_quote(_as_text(value))}")
    lines.append(DELIM)
    text = "\n".join(lines) + "\n\n"
    body = body.strip("\n")
    return text + (body + "\n" if body else "")


def _as_text(value: Any) -> str:
    if isinstance(value, (_dt.date, _dt.datetime)):
        return value.isoformat()[:10]
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _quote(value: str) -> str:
    """Quote only values that could be misparsed."""
    plain = (
        bool(value)
        and not any(ch in value for ch in ":#[]{}&*!|>'\"%@`")
        and value.strip() == value
        and not value.startswith("-")
    )
    if plain:
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _strip_comment(raw: str) -> str:
    """Drop a trailing `` #`` comment, ignoring text inside quotes."""
    quote: str | None = None
    for idx, ch in enumerate(raw):
        if quote:
            if ch == quote:
                quote = None
            continue
        if ch in "\"'":
            quote = ch
            continue
        if ch == "#" and (idx == 0 or raw[idx - 1] in " \t"):
            return raw[:idx]
    return raw


def _scalar(raw: str) -> Any:
    value = _strip_comment(raw).strip()
    if not value:
        return ""
    if len(value) > 1 and value[0] == value[-1] and value[0] in "\"'":
        inner = value[1:-1]
        if value[0] == '"':
            return inner.replace('\\"', '"').replace("\\\\", "\\")
        return inner.replace("''", "'")
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        return [_scalar(part) for part in _split_inline(inner)] if inner else []
    lowered = value.lower()
    if lowered in ("true", "false"):
        return lowered == "true"
    if lowered in ("null", "~"):
        return None
    return value


def _split_inline(inner: str) -> list[str]:
    parts: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    for ch in inner:
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in "\"'":
            quote = ch
            buf.append(ch)
        elif ch == ",":
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    if buf:
        parts.append("".join(buf))
    return [p.strip() for p in parts if p.strip()]
