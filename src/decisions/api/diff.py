"""In-memory unified diff generation (``git apply`` compatible).

``sync`` never edits ``docs/api/<version>/`` directly: it renders the target
tree in memory, diffs it against the files on disk, and writes a single plain
diff to ``docs/api/~<version>/changes.patch``.
"""

from __future__ import annotations

import difflib

MODIFIED, ADDED, DELETED = "modified", "added", "deleted"


def unified_diff(path: str, old: str | None, new: str | None, *, context: int = 3) -> str:
    """Return a unified diff for one file (``""`` when unchanged)."""
    if old == new:
        return ""
    if old is None:
        kind, old_text, new_text = ADDED, "", new or ""
    elif new is None:
        kind, old_text, new_text = DELETED, old, ""
    else:
        kind, old_text, new_text = MODIFIED, old, new

    from_file = "/dev/null" if kind == ADDED else f"a/{path}"
    to_file = "/dev/null" if kind == DELETED else f"b/{path}"
    chunks = list(
        difflib.unified_diff(
            _lines(old_text),
            _lines(new_text),
            fromfile=from_file,
            tofile=to_file,
            n=context,
            lineterm="",
        )
    )
    if not chunks:
        return ""

    header = [f"diff --git a/{path} b/{path}"]
    if kind == ADDED:
        header.append("new file mode 100644")
    elif kind == DELETED:
        header.append("deleted file mode 100644")
    return "\n".join([*header, *chunks]) + "\n"


def diff_tree(
    current: dict[str, str | None],
    target: dict[str, str | None],
) -> str:
    """Diff two document trees (``None`` means "absent").

    Paths missing from ``target`` are untouched; paths missing from ``current``
    are new files.
    """
    parts: list[str] = []
    for path in sorted(set(current) | set(target)):
        old = current.get(path)
        new = target.get(path)
        if old is None and new is None:
            continue
        parts.append(unified_diff(path, old, new))
    return "".join(parts)


def _lines(text: str) -> list[str]:
    if not text:
        return []
    return text.splitlines()
