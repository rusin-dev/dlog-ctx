"""Thin wrappers around git plumbing/porcelain commands.

Everything runs with ``git -C <root>`` and decodes output as UTF-8.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


class GitError(RuntimeError):
    """Raised when a git command fails."""


def git(
    root: Path,
    *args: str,
    check: bool = False,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    cmd = ["git", "-C", str(cwd or root), *args]
    try:
        proc = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError) as exc:  # pragma: no cover
        raise GitError(f"cannot run git: {exc}") from exc
    if check and proc.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed: {proc.stderr.strip() or proc.stdout.strip()}")
    return proc


def out(root: Path, *args: str, cwd: Path | None = None) -> str:
    proc = git(root, *args, cwd=cwd)
    return proc.stdout.strip() if proc.returncode == 0 else ""


def is_repo(root: Path) -> bool:
    return out(root, "rev-parse", "--is-inside-work-tree") == "true"


def current_branch(root: Path) -> str | None:
    branch = out(root, "symbolic-ref", "--short", "-q", "HEAD")
    return branch or None


def head_commit(root: Path, short: bool = True) -> str | None:
    args = ("rev-parse", "--short", "HEAD") if short else ("rev-parse", "HEAD")
    commit = out(root, *args)
    return commit or None


def tag_commit(root: Path, tag: str, short: bool = True) -> str | None:
    fmt = "rev-parse"
    args = (fmt, "--short", f"{tag}^{{commit}}") if short else (fmt, f"{tag}^{{commit}}")
    commit = out(root, *args)
    return commit or None


def status_porcelain(root: Path) -> list[str]:
    """Raw ``git status --porcelain`` lines (leading spaces are significant)."""
    proc = git(root, "status", "--porcelain")
    if proc.returncode != 0:
        return []
    return [line for line in proc.stdout.splitlines() if line.strip()]


def dirty_paths(root: Path, *, ignore_prefixes: tuple[str, ...] = ()) -> list[str]:
    """Return dirty ``git status`` lines, ignoring paths under ``ignore_prefixes``."""
    dirty: list[str] = []
    for line in status_porcelain(root):
        path = line[3:].strip() if len(line) > 3 else ""
        if " -> " in path:  # rename
            path = path.split(" -> ", 1)[1]
        if any(path == p or path.startswith(p.rstrip("/") + "/") for p in ignore_prefixes):
            continue
        dirty.append(line)
    return dirty


def branch_exists(root: Path, branch: str) -> bool:
    proc = git(root, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}")
    return proc.returncode == 0


def branch_exists_any(root: Path, branch: str) -> bool:
    proc = git(root, "rev-parse", "--verify", "--quiet", f"{branch}^{{commit}}")
    return proc.returncode == 0


def checkout_new(root: Path, branch: str, base: str) -> None:
    git(root, "checkout", "-b", branch, base, check=True)


def checkout(root: Path, branch: str) -> None:
    git(root, "checkout", branch, check=True)


def merge_no_ff(root: Path, branch: str) -> None:
    git(root, "merge", "--no-ff", branch, check=True)


def merge_in_progress(root: Path) -> bool:
    return any(
        (root / ".git" / name).exists()
        for name in ("MERGE_HEAD", "REBASE_HEAD", "CHERRY_PICK_HEAD")
    )


def create_tag(root: Path, name: str, message: str | None = None) -> None:
    if message:
        git(root, "tag", "-a", name, "-m", message, check=True)
    else:
        git(root, "tag", name, check=True)


def tag_exists(root: Path, name: str) -> bool:
    return bool(out(root, "tag", "--list", name))


def apply_patch(root: Path, patch_file: Path, *, check_only: bool = False) -> tuple[bool, str]:
    """Apply a unified diff inside the repository.

    ``git apply`` is used (never ``git am``): ``changes.patch`` is a plain diff,
    not a mailbox.
    """
    args = ["apply", "--whitespace=nowarn"]
    if check_only:
        args.append("--check")
    args.append(str(patch_file))
    proc = git(root, *args)
    message = (proc.stderr or proc.stdout).strip()
    return proc.returncode == 0, message


def staged_any(root: Path) -> bool:
    """True when the index differs from HEAD (i.e. there is something to commit)."""
    proc = git(root, "diff", "--cached", "--quiet")
    return proc.returncode != 0


def unmerged_paths(root: Path) -> list[str]:
    """Paths with unresolved merge conflicts."""
    proc = git(root, "diff", "--name-only", "--diff-filter=U")
    if proc.returncode != 0:
        return []
    return [line for line in proc.stdout.splitlines() if line.strip()]


def add(root: Path, *paths: str) -> None:
    git(root, "add", "--", *paths, check=True)


def commit(root: Path, message: str) -> str:
    git(root, "commit", "-m", message, check=True)
    return head_commit(root) or ""


def config_get(root: Path, key: str) -> str | None:
    return out(root, "config", "--get", key) or None


def discover_default_branch(root: Path) -> tuple[str, str] | None:
    """Detect the base branch, returning ``(name, source)``."""
    origin_head = out(root, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
    if origin_head:
        name = origin_head.split("/", 1)[-1]
        if branch_exists_any(root, name):
            return name, f"origin/HEAD ({origin_head})"
    configured = config_get(root, "init.defaultBranch")
    if configured and branch_exists_any(root, configured):
        return configured, "init.defaultBranch config"
    for candidate in ("main", "master", "dev"):
        if branch_exists_any(root, candidate):
            return candidate, "local branch probe"
    return None


def remote_exists(root: Path, name: str = "origin") -> bool:
    return name in out(root, "remote").split()
