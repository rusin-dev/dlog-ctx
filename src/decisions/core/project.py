"""Project root discovery and well-known directories
(``docs/decision``, ``docs/api``, ``.decisions``).
"""

from __future__ import annotations

import os
from pathlib import Path

from . import gitutils
from .models import normalize_version

DECISION_DIR = "docs/decision"
API_DIR = "docs/api"
STATE_DIR = ".decisions"
HEAD_FILE = "docs/api/head"
LOCK_FILE = "docs/api/.lock"
CONFIG_FILE = ".decisions/config.toml"
INDEX_FILE = ".decisions/index.db"

MARKER_DIRS = (".git", STATE_DIR, DECISION_DIR, API_DIR)


def find_root(start: str | Path | None = None) -> Path:
    """Locate the project root.

    Order: ``DECISIONS_ROOT`` env var > git toplevel > nearest parent with a
    well-known directory > current directory.
    """
    env = os.environ.get("DECISIONS_ROOT")
    if env:
        return Path(env).expanduser().resolve()
    start_path = Path(start or Path.cwd()).resolve()
    git_root = _git_toplevel(start_path)
    if git_root is not None:
        return git_root
    for candidate in (start_path, *start_path.parents):
        if any((candidate / marker).exists() for marker in MARKER_DIRS):
            return candidate
    return start_path


def decision_root(root: Path) -> Path:
    """``<root>/docs/decision``: one directory per topic."""
    return root / DECISION_DIR


def api_root(root: Path) -> Path:
    """``<root>/docs/api``: one directory per version."""
    return root / API_DIR


def state_dir(root: Path) -> Path:
    """``<root>/.decisions``: gitignored local state (index, config)."""
    return root / STATE_DIR


def head_path(root: Path) -> Path:
    """``<root>/docs/api/head``: the list of in-development versions."""
    return root / HEAD_FILE


def index_path(root: Path) -> Path:
    """``<root>/.decisions/index.db``: the derived SQLite index."""
    return root / INDEX_FILE


def lock_path(root: Path) -> Path:
    """``<root>/docs/api/.lock``: advisory lock for sync/release."""
    return root / LOCK_FILE


def draft_dir(root: Path, version: str) -> Path:
    """``<root>/docs/api/~<version>``: draft workspace of one version."""
    return root / API_DIR / f"~{version}"


def version_dir(root: Path, version: str) -> Path:
    """``<root>/docs/api/<version>``: confirmed baseline of one version."""
    return root / API_DIR / version


def read_head(root: Path) -> list[str]:
    """Read ``docs/api/head``: one in-development version per line, first is default."""
    path = head_path(root)
    if not path.exists():
        return []
    versions: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        token = line.split("#", 1)[0].strip()
        if token:
            versions.append(normalize_version(token))
    return versions


def write_head(root: Path, versions: list[str]) -> None:
    """Rewrite ``docs/api/head`` with ``versions`` (first one is the default)."""
    path = head_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"{normalize_version(v)}\n" for v in versions]
    path.write_text("".join(lines), encoding="utf-8")


def rel(root: Path, path: str | Path) -> str:
    """Return a repo-relative posix path; fall back to the absolute path if outside.

    Relative inputs resolve against the project root first, then against the
    current directory (so ``decisions context app.py`` works from ``src/`` too).
    """
    root = root.resolve()
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        from_root = root / candidate
        from_cwd = Path.cwd() / candidate
        candidate = from_root if from_root.exists() or not from_cwd.exists() else from_cwd
    candidate = candidate.resolve()
    try:
        return candidate.relative_to(root).as_posix()
    except ValueError:
        return candidate.as_posix()


def _git_toplevel(start: Path) -> Path | None:
    """Git worktree root containing ``start``, or ``None`` outside a repository."""
    try:
        toplevel = gitutils.out(start, "rev-parse", "--show-toplevel")
    except gitutils.GitError:  # pragma: no cover - git binary missing
        return None
    return Path(toplevel).resolve() if toplevel else None
