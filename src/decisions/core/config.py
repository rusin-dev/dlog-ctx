"""``.decisions/config.toml`` loading and defaults.

The config is optional: when the file is missing every field falls back to a
default, so the first run stays frictionless.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

CONFIG_REL = ".decisions/config.toml"
SHARED_CONFIG_REL = "decisions.toml"
"""Committed, team-shared configuration at the repository root.

The default location (``.decisions/config.toml``) is gitignored because the index
database lives next to it; the shared file exists so topic mappings and language
settings travel with the repo. The local file is merged on top of the shared one,
so a developer can still override settings without touching the repo.
"""

DEFAULT_EXCLUDE = [
    "tests",
    "test",
    "test_*.py",
    "*_test.py",
    "conftest.py",
    "setup.py",
    "docs",
]

DEFAULT_CONFIG_TOML = """\
# decisions-ctx configuration (safe to delete, every field has a default)

[api]
# Where API symbols are extracted from (repo-relative)
source_roots = ["src"]

# Globs that exclude a file or directory from extraction
exclude = ["tests", "test", "test_*.py", "*_test.py", "conftest.py", "setup.py", "docs"]

# Keep a tombstone section (with a removal callout) for deleted symbols so that
# anchors stay valid forever
tombstones = true

# Python is always parsed precisely; other languages use a best-effort regex
# backend. Restrict with e.g. languages = ["python"] or add plugin backends.
languages = "auto"

# Optional: explicit topic ownership, key = topic, value = list of globs
# [api.topics]
# storage = ["src/storage/**", "src/storage.py"]
"""


@dataclass
class Config:
    root: Path
    data: dict[str, Any] = field(default_factory=dict)

    # -- loading --------------------------------------------------------
    @classmethod
    def load(cls, root: Path) -> Config:
        data: dict[str, Any] = {}
        shared = root / SHARED_CONFIG_REL
        if shared.exists():
            data = _read_toml(shared, SHARED_CONFIG_REL)
        local = root / CONFIG_REL
        if local.exists():
            data = _merge(data, _read_toml(local, CONFIG_REL))
        return cls(root=root, data=data)

    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self.data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    # -- derived settings -----------------------------------------------
    @property
    def index_path(self) -> Path:
        return self.root / self.get("index.path", ".decisions/index.db")

    @property
    def source_roots(self) -> list[str]:
        roots = self.get("api.source_roots", ["src"])
        if isinstance(roots, str):
            roots = [roots]
        return [str(r) for r in roots] or ["src"]

    @property
    def exclude(self) -> list[str]:
        patterns = self.get("api.exclude", DEFAULT_EXCLUDE)
        if isinstance(patterns, str):
            patterns = [patterns]
        return [str(p) for p in patterns]

    @property
    def topic_patterns(self) -> dict[str, list[str]]:
        raw = self.get("api.topics", {}) or {}
        result: dict[str, list[str]] = {}
        if isinstance(raw, dict):
            for topic, patterns in raw.items():
                if isinstance(patterns, str):
                    patterns = [patterns]
                result[str(topic)] = [str(p) for p in patterns]
        return result

    @property
    def tombstones(self) -> bool:
        return bool(self.get("api.tombstones", True))

    @property
    def languages(self) -> list[str] | None:
        """``None`` means auto (every registered backend); else backend names."""
        value = self.get("api.languages", "auto")
        if value is None or value == "" or value == []:
            return None
        if isinstance(value, str):
            value = [value]
        tokens = [str(item).strip().lower() for item in value if str(item).strip()]
        if not tokens or tokens == ["auto"]:
            return None
        return tokens


def write_default(root: Path, *, overwrite: bool = False) -> Path:
    path = root / CONFIG_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not overwrite:
        return path
    path.write_text(DEFAULT_CONFIG_TOML, encoding="utf-8")
    return path


def _read_toml(path: Path, label: str) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"cannot read {label}: {exc}") from exc


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge ``override`` on top of ``base``."""
    merged = dict(base)
    for key, value in override.items():
        current = merged.get(key)
        if isinstance(value, dict) and isinstance(current, dict):
            merged[key] = _merge(current, value)
        else:
            merged[key] = value
    return merged
