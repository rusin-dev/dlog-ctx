"""Data models: decision entries and API symbols."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

STATUSES = ("accepted", "superseded", "deprecated")
"""Allowed values for ``status`` in decision frontmatter."""

VERSION_RE = re.compile(r"^~?v\d+\.\d+(?:\.\d+)?$")
"""Version strings: directories stop at minor granularity, git tags may carry a patch."""

DECISION_REF_RE = re.compile(r"@decision:([A-Za-z0-9._/\-]+)")
"""Docstring back-reference marker, e.g. ``@decision:storage/use-sqlite``."""


def decision_refs(text: str | None) -> list[str]:
    """Extract unique ``@decision:<ref>`` markers from a docstring."""
    if not text:
        return []
    refs: list[str] = []
    for match in DECISION_REF_RE.finditer(text):
        ref = match.group(1).strip("/")
        if ref and ref not in refs:
            refs.append(ref)
    return refs


def strip_decision_refs(text: str | None) -> str | None:
    """Remove marker tokens from prose, dropping lines that become empty."""
    if not text:
        return text
    kept: list[str] = []
    for line in text.splitlines():
        cleaned = DECISION_REF_RE.sub("", line).rstrip()
        if cleaned.strip() in ("", "-", "*"):
            continue
        kept.append(cleaned)
    result = "\n".join(kept).strip()
    return result or None


def normalize_version(version: str) -> str:
    """Strip whitespace and a leading ``~``, keeping the ``vX.Y[.Z]`` form."""
    return version.strip().lstrip("~")


def is_version(value: str) -> bool:
    return bool(VERSION_RE.match(value.strip()))


def minor_of(version: str) -> str:
    """``v1.3.1`` / ``~v1.3`` -> ``v1.3`` (directory granularity)."""
    parts = normalize_version(version).lstrip("v").split(".")
    return "v" + ".".join(parts[:2])


def version_key(version: str) -> tuple[int, ...]:
    """Sort key so ``v1.10`` orders after ``v1.9`` (unlike plain string sort)."""
    parts = normalize_version(version).lstrip("v").split(".")
    return tuple(int(part) if part.isdigit() else 0 for part in parts)


def anchor(qualified_name: str) -> str:
    """GitHub style heading anchor: ``Database.connect`` -> ``databaseconnect``."""
    text = qualified_name.lower()
    text = re.sub(r"[^\w\s-]", "", text)
    return re.sub(r"[\s_]+", "-", text).strip("-")


@dataclass
class Decision:
    """A single decision entry (``docs/decision/<topic>/<date>-<slug>.md``)."""

    path: str  # repo-relative, posix style
    topic: str
    slug: str
    title: str
    date: str  # YYYY-MM-DD
    status: str = "accepted"
    commit_hash: str | None = None
    branch: str | None = None
    body: str = ""
    supersedes: str | None = None
    tags: list[str] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)
    symbols: list[str] = field(default_factory=list)
    version: str | None = None

    def summary(self, limit: int = 160) -> str:
        """First paragraph of the body, whitespace-collapsed and truncated."""
        text = re.sub(r"^#+\s*", "", self.body.strip())
        text = re.sub(r"\s+", " ", text).strip()
        if len(text) <= limit:
            return text
        return text[: limit - 1].rstrip() + "..."


@dataclass
class ApiSymbol:
    """One symbol section inside an API document.

    ``doc_path`` is the Markdown document that owns the section (repo-relative),
    ``source_file`` is the source file where the symbol is defined.
    """

    qualified_name: str
    kind: str  # module / class / function / method / constant
    module: str
    topic: str
    doc_path: str
    signature: str
    returns: str | None = None
    doc: str | None = None
    source_file: str | None = None
    lineno: int | None = None
    status: str = "active"
    removed_in: str | None = None
    decision_refs: list[str] = field(default_factory=list)

    def anchor(self) -> str:
        """GitHub style heading anchor: ``Database.connect`` -> ``databaseconnect``."""
        return anchor(self.qualified_name)

    def doc_link(self) -> str:
        return f"{self.doc_path}#{self.anchor()}"
