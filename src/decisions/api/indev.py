"""``indev`` / ``release`` branch lifecycle.

The flow is intentionally git-native:

    indev start v1.3   ->  git checkout -b indev-v1.3 <base>
    (sync on a timer)  ->  docs/api/~v1.3/changes.patch
    release v1.3.1     ->  git tag v1.3.1            (release point)
    indev finish v1.3  ->  git apply + commit + merge --no-ff  (baseline confirmed)

``head`` holds the baseline line (``v1.3``), git tags hold release points
(``v1.3.1``): the two are deliberately decoupled.
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from ..core import config as config_mod
from ..core import db, gitutils, project
from ..core.models import minor_of, normalize_version
from . import sync
from .lock import FileLock

AGENTS_FILE = "AGENTS.md"

BLOCK_RE = re.compile(
    r"<!--\s*decisions:indev(?P<label>:[^>]*?)?\s*-->\n?"
    r"(?P<body>.*?)"
    r"<!--\s*/decisions:indev(?P<label_end>:[^>]*?)?\s*-->\n?",
    re.DOTALL,
)

RELEASE_TAG_RE = re.compile(r"^v\d+\.\d+\.\d+$")


class IndevError(RuntimeError):
    """Raised when an indev/release precondition fails."""


# ---------------------------------------------------------------------------
# AGENTS.md markers
# ---------------------------------------------------------------------------


def marker_block(version: str) -> str:
    version = normalize_version(version)
    return (
        "<!-- decisions:indev -->\n"
        f"> Currently developing the content of `indev-{version}`.\n"
        "<!-- /decisions:indev -->\n"
    )


def _block_version(match: re.Match[str]) -> str | None:
    label = (match.group("label") or match.group("label_end") or "").lstrip(": ").strip()
    if label:
        return normalize_version(label)
    found = re.search(r"indev-(v\d+\.\d+)", match.group("body") or "")
    return normalize_version(found.group(1)) if found else None


def agents_text(root: Path) -> str:
    path = root / AGENTS_FILE
    return path.read_text(encoding="utf-8") if path.exists() else ""


def insert_agents_block(root: Path, version: str) -> bool:
    """Insert the marker range after the first heading. Returns True if changed."""
    version = normalize_version(version)
    text = agents_text(root)
    if any(_block_version(m) == version for m in BLOCK_RE.finditer(text)):
        return False

    block = marker_block(version)
    lines = text.splitlines()
    heading = next(
        (index for index, line in enumerate(lines) if re.match(r"^#{1,6}\s", line)),
        None,
    )
    if heading is None:
        new_text = text.rstrip("\n") + "\n\n" + block if text.strip() else block
    else:
        before = lines[: heading + 1]
        after = lines[heading + 1 :]
        while after and not after[0].strip():
            after = after[1:]
        parts = ["\n".join(before), "\n" + block.rstrip("\n")]
        if after:
            parts.append("\n".join(after))
        new_text = "\n\n".join(parts) + "\n"
    (root / AGENTS_FILE).write_text(new_text, encoding="utf-8", newline="\n")
    return True


def remove_agents_block(root: Path, version: str) -> bool:
    """Remove the marker range for ``version`` (pairs matched precisely)."""
    path = root / AGENTS_FILE
    if not path.exists():
        return False
    version = normalize_version(version)
    text = path.read_text(encoding="utf-8")
    changed = False

    def _replace(match: re.Match[str]) -> str:
        nonlocal changed
        if _block_version(match) != version:
            return match.group(0)
        changed = True
        return ""

    new_text = BLOCK_RE.sub(_replace, text)
    new_text = re.sub(r"\n{3,}", "\n\n", new_text).strip("\n")
    if not changed:
        return False
    if not new_text.strip():
        # The marker block was the only content: restore the pre-start state.
        path.unlink(missing_ok=True)
        return True
    path.write_text(new_text + "\n", encoding="utf-8", newline="\n")
    return True


# ---------------------------------------------------------------------------
# indev start
# ---------------------------------------------------------------------------


@dataclass
class StartResult:
    version: str
    branch: str
    base: str
    base_source: str
    agents_updated: bool
    draft_dir: Path


def start(root: Path, version: str, base: str | None = None) -> StartResult:
    version = normalize_version(version)
    if not gitutils.is_repo(root):
        raise IndevError("not a git repository")

    head = project.read_head(root)
    if version not in head:
        raise IndevError(
            f"{version} is not listed in docs/api/head (found: {', '.join(head) or 'nothing'})"
        )
    baseline = project.version_dir(root, version)
    if not baseline.is_dir():
        raise IndevError(f"baseline directory docs/api/{version}/ does not exist; create it first")

    branch = f"indev-{version}"
    if gitutils.branch_exists(root, branch):
        raise IndevError(f"branch {branch} already exists")

    dirty = gitutils.dirty_paths(root)
    if dirty:
        raise IndevError("working tree is not clean:\n  " + "\n  ".join(dirty))

    if base:
        if not gitutils.branch_exists_any(root, base):
            raise IndevError(f"base branch {base!r} does not exist")
        base_name, base_source = base, "--base"
    else:
        discovered = gitutils.discover_default_branch(root)
        if discovered is None:
            raise IndevError("cannot detect the base branch; pass --base <branch>")
        base_name, base_source = discovered

    gitutils.checkout_new(root, branch, base_name)
    agents_updated = insert_agents_block(root, version)
    draft = sync.ensure_draft_dir(root, version)
    return StartResult(
        version=version,
        branch=branch,
        base=base_name,
        base_source=base_source,
        agents_updated=agents_updated,
        draft_dir=draft,
    )


# ---------------------------------------------------------------------------
# indev finish
# ---------------------------------------------------------------------------


@dataclass
class FinishResult:
    version: str
    branch: str
    patch_applied: bool
    committed: bool
    merged: bool
    conflict: bool
    base: str | None
    message: str = ""


def finish(
    root: Path,
    version: str,
    *,
    no_merge: bool = False,
    resume: bool = False,
    base: str | None = None,
    close_head: bool = False,
) -> FinishResult:
    version = normalize_version(version)
    branch = f"indev-{version}"
    current = gitutils.current_branch(root)

    result = FinishResult(
        version=version,
        branch=branch,
        patch_applied=False,
        committed=False,
        merged=False,
        conflict=False,
        base=None,
    )

    if resume and current != branch and gitutils.merge_in_progress(root):
        # Conflicts were resolved by hand on the base branch: commit the merge.
        conflicts = gitutils.unmerged_paths(root)
        if conflicts:
            raise IndevError("unresolved conflicts remain:\n  " + "\n  ".join(conflicts))
        gitutils.commit(root, f"api: merge {branch}")
        result.merged = True
        result.base = current
        result.message = "merge committed after resume"
        return result

    if current != branch:
        raise IndevError(f"finish must run on {branch} (now: {current or 'detached'})")

    draft_rel = f"docs/api/~{version}"
    # AGENTS.md is modified by `indev start` and restored by `finish`, so it is
    # draft state and must not block the commit boundary.
    dirty = gitutils.dirty_paths(root, ignore_prefixes=(draft_rel, AGENTS_FILE))
    if dirty:
        raise IndevError(
            "working tree must be clean apart from the draft directory:\n  " + "\n  ".join(dirty)
        )

    if not resume:
        patch = sync.patch_path(root, version)
        if patch.exists() and patch.stat().st_size > 0:
            ok, message = gitutils.apply_patch(root, patch)
            if not ok:
                raise IndevError(
                    "git apply failed, the tree is unchanged; resolve manually:\n" + message
                )
            result.patch_applied = True

        remove_agents_block(root, version)
        shutil.rmtree(project.draft_dir(root, version), ignore_errors=True)
        _drop_draft_index(root, version)
        sync.remove_empty_dirs(project.version_dir(root, version))

        if close_head:
            head = [v for v in project.read_head(root) if v != version]
            project.write_head(root, head)

        paths = [f"docs/api/{version}"]
        if (root / AGENTS_FILE).exists():
            paths.append(AGENTS_FILE)
        gitutils.add(root, *paths)
        if gitutils.staged_any(root):
            gitutils.commit(root, f"api: confirm {version} baseline")
            result.committed = True

    if no_merge:
        return result

    if base:
        base_name = base
    else:
        discovered = gitutils.discover_default_branch(root)
        if discovered is None:
            raise IndevError(
                "cannot detect the base branch to merge into; re-run with --base <branch>"
            )
        base_name = discovered[0]
    result.base = base_name

    gitutils.checkout(root, base_name)
    proc = gitutils.git(root, "merge", "--no-ff", branch)
    if proc.returncode != 0:
        result.conflict = True
        result.merged = False
        result.message = (proc.stdout + proc.stderr).strip()
        # Conflicts are intentionally left in place: resolve, then finish --resume.
        return result
    result.merged = True
    result.message = proc.stdout.strip()
    return result


# ---------------------------------------------------------------------------
# release
# ---------------------------------------------------------------------------


@dataclass
class ReleaseResult:
    tag: str
    version: str
    commit: str | None


def release(root: Path, tag: str) -> ReleaseResult:
    tag = tag.strip()
    if not RELEASE_TAG_RE.match(tag):
        raise IndevError(f"{tag!r} is not a release tag (expected vX.Y.Z)")
    version = minor_of(tag)
    branch = f"indev-{version}"
    current = gitutils.current_branch(root)
    if current != branch:
        raise IndevError(f"release must run on {branch} (now: {current or 'detached'})")

    draft_rel = f"docs/api/~{version}"
    dirty = gitutils.dirty_paths(root, ignore_prefixes=(draft_rel, AGENTS_FILE))
    if dirty:
        raise IndevError("working tree is not clean:\n  " + "\n  ".join(dirty))

    lock = FileLock(project.lock_path(root))
    if not lock.acquire(blocking=True, timeout=30):
        raise IndevError("cannot acquire docs/api/.lock")
    try:
        patch = sync.patch_path(root, version)
        if patch.exists():
            if patch.stat().st_size > 0:
                raise IndevError(
                    f"unconfirmed patch exists at {patch.relative_to(root).as_posix()};"
                    " run indev finish first"
                )
            patch.unlink()
        if gitutils.tag_exists(root, tag):
            raise IndevError(f"tag {tag} already exists")
        gitutils.create_tag(root, tag, f"api: release {tag}")
        return ReleaseResult(tag=tag, version=version, commit=gitutils.tag_commit(root, tag))
    finally:
        lock.release()


# ---------------------------------------------------------------------------
# indev list
# ---------------------------------------------------------------------------


@dataclass
class VersionStatus:
    version: str
    branch: bool
    draft: bool
    patch: str  # none / empty / pending
    baseline: bool


def _drop_draft_index(root: Path, version: str) -> None:
    """Forget the draft once it is merged, so `context` falls back to the baseline."""
    config = config_mod.Config.load(root)
    if not config.index_path.exists():
        return
    conn = db.connect(config.index_path)
    try:
        db.drop_api_version(conn, f"~{version}")
    finally:
        conn.close()


def list_versions(root: Path) -> list[VersionStatus]:
    result: list[VersionStatus] = []
    for version in project.read_head(root):
        draft_dir = project.draft_dir(root, version)
        patch = sync.patch_path(root, version)
        if not patch.exists():
            patch_state = "none"
        elif patch.stat().st_size == 0:
            patch_state = "empty"
        else:
            patch_state = "pending"
        result.append(
            VersionStatus(
                version=version,
                branch=gitutils.branch_exists(root, f"indev-{version}"),
                draft=draft_dir.is_dir(),
                patch=patch_state,
                baseline=project.version_dir(root, version).is_dir(),
            )
        )
    return result
