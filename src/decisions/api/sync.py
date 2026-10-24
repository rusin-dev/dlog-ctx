"""``sync``: compute the next baseline in memory, write only a draft patch.

Hard rule: ``sync`` writes exactly two places — ``docs/api/~<version>/changes.patch``
and the index database. It never modifies ``docs/api/<version>/`` directly, so
the confirmed baseline stays clean in ``git status`` for the whole ``indev``
cycle and the commit/merge boundary in ``indev finish`` stays deterministic.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from ..core import config as config_mod
from ..core import db, gitutils, project
from ..core.models import normalize_version
from . import diff as diff_mod
from . import extractor, render
from .lock import FileLock

DRAFT_GITIGNORE = "*\n!.gitignore\n"
PATCH_NAME = "changes.patch"


@dataclass
class SyncResult:
    version: str
    patch_path: Path | None = None
    patch_text: str = ""
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)
    base_count: int = 0
    now_count: int = 0
    skipped: bool = False
    reason: str = ""
    warnings: list[str] = field(default_factory=list)

    @property
    def has_changes(self) -> bool:
        return bool(self.patch_text)


class SyncError(RuntimeError):
    """Raised for unrecoverable sync preconditions."""


# ---------------------------------------------------------------------------
# Draft workspace helpers
# ---------------------------------------------------------------------------


def ensure_draft_dir(root: Path, version: str) -> Path:
    """Create ``docs/api/~V/`` with a self-contained .gitignore."""
    version = normalize_version(version)
    directory = project.draft_dir(root, version)
    directory.mkdir(parents=True, exist_ok=True)
    ignore = directory / ".gitignore"
    expected = DRAFT_GITIGNORE
    if not ignore.exists() or ignore.read_text(encoding="utf-8") != expected:
        ignore.write_text(expected, encoding="utf-8", newline="\n")
    return directory


def patch_path(root: Path, version: str) -> Path:
    return project.draft_dir(root, version) / PATCH_NAME


def read_patch(root: Path, version: str) -> str:
    path = patch_path(root, version)
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def _version_from_branch(root: Path) -> str | None:
    """``indev-v1.3`` -> ``v1.3`` so a timer on that branch syncs the right version."""
    branch = gitutils.current_branch(root) or ""
    if not branch.startswith("indev-"):
        return None
    version = normalize_version(branch[len("indev-") :])
    return version if version in project.read_head(root) else None


def load_docs(root: Path, version: str) -> dict[str, str]:
    """Read every Markdown document of one version directory."""
    base = project.version_dir(root, normalize_version(version))
    docs: dict[str, str] = {}
    if not base.is_dir():
        return docs
    for path in sorted(base.rglob("*.md")):
        rel = path.relative_to(root).as_posix()
        docs[rel] = path.read_text(encoding="utf-8")
    return docs


def remove_empty_dirs(base: Path) -> None:
    """Prune directories left behind after files are deleted by a patch."""
    if not base.is_dir():
        return
    for path in sorted(base.rglob("*"), reverse=True):
        if path.is_dir() and not any(path.iterdir()):
            path.rmdir()


# ---------------------------------------------------------------------------
# Core computation
# ---------------------------------------------------------------------------


def plan(
    root: Path,
    version: str,
    config: config_mod.Config | None = None,
) -> tuple[dict[str, str], dict[str, str | None], list, list[str]]:
    """Return ``(base_docs, target_tree, symbols, warnings)`` for one version."""
    config = config or config_mod.Config.load(root)
    version = normalize_version(version)
    base_docs = load_docs(root, version)
    symbols, warnings = extractor.extract_symbols(root, config, version)
    target = render.render_target(base_docs, symbols, version, tombstones=config.tombstones)
    return base_docs, target, symbols, warnings


def current_tree(root: Path, target: dict[str, str | None]) -> dict[str, str | None]:
    """Read on-disk content for every path the target touches."""
    tree: dict[str, str | None] = {}
    for path in target:
        file_path = root / path
        tree[path] = file_path.read_text(encoding="utf-8") if file_path.exists() else None
    return tree


def compute_patch(
    root: Path, version: str, config: config_mod.Config | None = None
) -> tuple[str, dict[str, str], dict[str, str | None], list, list[str]]:
    base_docs, target, symbols, warnings = plan(root, version, config)
    patch = diff_mod.diff_tree(current_tree(root, target), target)
    return patch, base_docs, target, symbols, warnings


def summarize(
    base_docs: dict[str, str], target: dict[str, str | None]
) -> tuple[list[str], list[str], list[str]]:
    """Compare baseline sections with the target tree for human reporting."""
    before = {s.qualified_name: s for s in render.symbols_from_docs(base_docs)}
    after_docs = {path: text for path, text in target.items() if text}
    after = {s.qualified_name: s for s in render.symbols_from_docs(after_docs)}

    added = sorted(
        name
        for name, symbol in after.items()
        if name not in before or (before[name].status == "removed" and symbol.status == "active")
    )
    removed = sorted(
        name
        for name, symbol in before.items()
        if name not in after or after[name].status == "removed"
    )
    changed = sorted(
        name
        for name in set(before) & set(after)
        if before[name].status == "active"
        and after[name].status == "active"
        and (before[name].signature, before[name].doc, before[name].returns)
        != (after[name].signature, after[name].doc, after[name].returns)
    )
    return added, removed, changed


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def run_sync(
    root: Path,
    version: str | None = None,
    *,
    allow_branch: bool = False,
    write: bool = True,
    use_lock: bool = True,
) -> SyncResult:
    """One stateless sync pass (scheduled by cron/timer or ``--watch``)."""
    config = config_mod.Config.load(root)
    head = project.read_head(root)
    if not head:
        raise SyncError("docs/api/head is empty: declare at least one version first")
    target_version = normalize_version(version or _version_from_branch(root) or head[0])
    if target_version not in head:
        raise SyncError(
            f"{target_version} is not listed in docs/api/head (found: {', '.join(head)})"
        )

    if not allow_branch:
        branch = gitutils.current_branch(root)
        expected = f"indev-{target_version}"
        if branch != expected:
            raise SyncError(
                f"draft patches must be generated on branch {expected} "
                f"(now: {branch or 'detached'})"
            )

    lock: FileLock | None = None
    if use_lock and write:
        lock = FileLock(project.lock_path(root))
        if not lock.acquire():
            return SyncResult(
                version=target_version,
                skipped=True,
                reason="another sync/release holds docs/api/.lock",
            )

    conn: sqlite3.Connection | None = None
    try:
        # Both entry points share the same index lifecycle; rebuilding here keeps
        # decisions fresh for `context` even if only a timer runs sync.
        index_warnings: list[str] = []
        if write:
            conn, index_warnings = db.ensure_index(root, config)

        patch, base_docs, target, _symbols, warnings = compute_patch(root, target_version, config)
        warnings = [*index_warnings, *warnings]
        added, removed, changed = summarize(base_docs, target)

        result = SyncResult(
            version=target_version,
            patch_text=patch,
            added=added,
            removed=removed,
            changed=changed,
            base_count=len(render.symbols_from_docs(base_docs)),
            now_count=len(
                render.symbols_from_docs({path: text for path, text in target.items() if text})
            ),
            warnings=warnings,
        )

        if not write:
            return result

        directory = ensure_draft_dir(root, target_version)
        path = directory / PATCH_NAME
        if patch:
            path.write_text(patch, encoding="utf-8", newline="\n")
            result.patch_path = path
        else:
            path.unlink(missing_ok=True)

        if conn is not None:
            _write_draft_index(conn, target_version, target)
        return result
    finally:
        if conn is not None:
            conn.close()
        if lock is not None:
            lock.release()


def _write_draft_index(
    conn: sqlite3.Connection,
    version: str,
    target: dict[str, str | None],
) -> None:
    """Record the future state so ``context`` can answer during development."""
    docs = {path: text for path, text in target.items() if text}
    symbols = render.symbols_from_docs(docs)
    db.replace_api_index(
        conn,
        f"~{version}",
        symbols,
        base_version=version,
        status="draft",
    )


# ---------------------------------------------------------------------------
# Consistency check
# ---------------------------------------------------------------------------

INFO, WARN, ERROR = "info", "warn", "error"


def check_project(root: Path) -> list[tuple[str, str]]:
    """Report inconsistencies in the version/branch/patch bookkeeping."""
    findings: list[tuple[str, str]] = []
    head = project.read_head(root)
    if not head:
        return [(INFO, "docs/api/head is empty (no version declared)")]

    api = project.api_root(root)
    known_dirs = (
        {path.name for path in api.iterdir() if path.is_dir() and not path.name.startswith(".")}
        if api.is_dir()
        else set()
    )
    released = {name for name in known_dirs if not name.startswith("~")}
    drafts = {name[1:] for name in known_dirs if name.startswith("~")}

    for version in head:
        branch = f"indev-{version}"
        branch_exists = gitutils.branch_exists(root, branch)
        if version in released and branch_exists:
            # Normal post-finish state: the baseline is confirmed but the line
            # stays open for further patch releases.
            findings.append((INFO, f"{version} baseline confirmed; open line is {branch}"))
        elif version in released:
            findings.append(
                (
                    WARN,
                    f"{version} is listed in head and docs/api/{version}/ already exists,"
                    f" but {branch} is gone; close the line with `head remove {version}`",
                )
            )
        elif branch_exists:
            findings.append(
                (
                    WARN,
                    f"docs/api/{version}/ is missing while {branch} exists;"
                    " create the baseline directory first",
                )
            )
        else:
            findings.append((INFO, f"{version} is in head; baseline directory not created yet"))

    for version in sorted(drafts - set(head)):
        findings.append(
            (ERROR, f"orphan draft docs/api/~{version}/ exists but {version} is not in head")
        )

    for version in sorted(released - set(head)):
        findings.append(
            (INFO, f"docs/api/{version}/ is a released baseline no longer tracked by head")
        )

    for version in sorted(drafts):
        patch = patch_path(root, version)
        if patch.exists() and patch.stat().st_size == 0:
            findings.append((WARN, f"docs/api/~{version}/changes.patch is empty"))
        if not gitutils.branch_exists(root, f"indev-{version}"):
            findings.append((ERROR, f"draft docs/api/~{version}/ has no indev-{version} branch"))

    if gitutils.is_repo(root):
        dirty = [
            line
            for line in gitutils.status_porcelain(root)
            if line[3:].strip().startswith("docs/api/") and "/~" not in line
        ]
        for line in dirty:
            findings.append((ERROR, f"released API docs modified locally: {line}"))
    return findings
