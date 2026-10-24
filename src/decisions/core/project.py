"""Project root discovery and well-known directories
(``docs/decision``, ``docs/api``, ``.decisions``).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

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
    return root / DECISION_DIR


def api_root(root: Path) -> Path:
    return root / API_DIR


def state_dir(root: Path) -> Path:
    return root / STATE_DIR


def head_path(root: Path) -> Path:
    return root / HEAD_FILE


def index_path(root: Path) -> Path:
    return root / INDEX_FILE


def lock_path(root: Path) -> Path:
    return root / LOCK_FILE


def draft_dir(root: Path, version: str) -> Path:
    return root / API_DIR / f"~{version}"


def version_dir(root: Path, version: str) -> Path:
    return root / API_DIR / version


def read_head(root: Path) -> list[str]:
    """Read ``docs/api/head``: one in-development version per line, first is default."""
    from .models import normalize_version

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
    from .models import normalize_version

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
    try:
        proc = subprocess.run(
            ["git", "-C", str(start), "rev-parse", "--show-toplevel"],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    out = proc.stdout.strip()
    return Path(out).resolve() if out else None
