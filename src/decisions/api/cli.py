"""``decisions-api`` — generate docs from code, manage the baseline lifecycle.

Commands: ``sync``, ``diff``, ``check``, ``indev start|finish|list``,
``release <vX.Y.Z>``, ``head list|add|remove``.

``decisions api <sub>`` forwards here in-process, so both entry points behave
identically.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Sequence
from pathlib import Path

from .. import __version__
from ..core import config as config_mod
from ..core import gitutils, project
from ..core.models import is_version, normalize_version
from ..core.stdio import configure_stdio
from . import indev, sync

EXIT_OK = 0
EXIT_ISSUES = 1
EXIT_ERROR = 2
EXIT_CONFLICT = 3


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="decisions-api",
        description="Generate API docs from code and manage baselines (vX.Y).",
    )
    parser.add_argument(
        "-C",
        "--root",
        help="project root (defaults to the git toplevel of the current directory)",
    )
    parser.add_argument("--version", action="version", version=f"decisions-api {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    sync_cmd = sub.add_parser("sync", help="regenerate ~V/changes.patch and the index")
    sync_cmd.add_argument("--version", dest="target_version", help="version to sync")
    sync_cmd.add_argument(
        "--watch",
        nargs="?",
        type=int,
        const=300,
        metavar="SECONDS",
        help="loop forever, default interval 300s (external cron/timer is preferred)",
    )
    sync_cmd.add_argument(
        "--allow-branch",
        action="store_true",
        help="skip the 'must be on indev-V' branch check (escape hatch)",
    )
    sync_cmd.add_argument("--dry-run", action="store_true", help="print the patch, write nothing")
    sync_cmd.add_argument("--quiet", action="store_true")
    sync_cmd.add_argument("--json", action="store_true")

    diff_cmd = sub.add_parser("diff", help="print the unified diff for a version (no write)")
    diff_cmd.add_argument("version", help="version to diff, e.g. v1.3")

    check_cmd = sub.add_parser("check", help="report bookkeeping inconsistencies")
    check_cmd.add_argument("--json", action="store_true")

    indev_cmd = sub.add_parser("indev", help="in-development branch lifecycle")
    indev_sub = indev_cmd.add_subparsers(dest="indev_command", required=True)

    start_cmd = indev_sub.add_parser("start", help="create indev-V from a base branch")
    start_cmd.add_argument("version")
    start_cmd.add_argument("--base", help="base branch (auto-detected by default)")
    start_cmd.add_argument("--json", action="store_true")

    finish_cmd = indev_sub.add_parser("finish", help="apply the patch, commit, merge")
    finish_cmd.add_argument("version")
    finish_cmd.add_argument("--base", help="base branch to merge into")
    finish_cmd.add_argument("--no-merge", action="store_true", help="stop before merging")
    finish_cmd.add_argument(
        "--resume", action="store_true", help="only merge (patch already applied and committed)"
    )
    finish_cmd.add_argument(
        "--close-head", action="store_true", help="drop this version line from docs/api/head"
    )
    finish_cmd.add_argument("--json", action="store_true")

    list_cmd = indev_sub.add_parser("list", help="per-version branch/patch status")
    list_cmd.add_argument("--json", action="store_true")

    release_cmd = sub.add_parser("release", help="tag a release point (vX.Y.Z)")
    release_cmd.add_argument("tag")
    release_cmd.add_argument("--json", action="store_true")

    head_cmd = sub.add_parser("head", help="inspect or edit docs/api/head")
    head_sub = head_cmd.add_subparsers(dest="head_command", required=True)
    head_sub.add_parser("list", help="print versions")
    head_add = head_sub.add_parser("add", help="append a version line")
    head_add.add_argument("version")
    head_add.add_argument(
        "--create",
        action="store_true",
        help="also create docs/api/<version>/ (bootstrap a first baseline)",
    )
    head_remove = head_sub.add_parser("remove", help="remove a version line")
    head_remove.add_argument("version")

    return parser


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    configure_stdio()
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        build_parser().print_help()
        return EXIT_ERROR
    args = build_parser().parse_args(argv)
    root = project.find_root(args.root)

    try:
        if args.command == "sync":
            return cmd_sync(root, args)
        if args.command == "diff":
            return cmd_diff(root, args)
        if args.command == "check":
            return cmd_check(root, args)
        if args.command == "indev":
            return cmd_indev(root, args)
        if args.command == "release":
            return cmd_release(root, args)
        if args.command == "head":
            return cmd_head(root, args)
    except (indev.IndevError, sync.SyncError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except (ValueError, OSError, gitutils.GitError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    return EXIT_ERROR  # pragma: no cover


# ---------------------------------------------------------------------------
# sync
# ---------------------------------------------------------------------------


def cmd_sync(root: Path, args: argparse.Namespace) -> int:
    interval = args.watch
    if interval is None:
        return _sync_once(root, args)
    if interval < 5:
        print("error: --watch interval must be >= 5 seconds", file=sys.stderr)
        return EXIT_ERROR
    print(f"watching: sync every {interval}s (Ctrl-C to stop)", file=sys.stderr)
    try:
        while True:
            _sync_once(root, args)
            time.sleep(interval)
    except KeyboardInterrupt:  # pragma: no cover - interactive
        print("stopped", file=sys.stderr)
        return EXIT_OK


def _sync_once(root: Path, args: argparse.Namespace) -> int:
    result = sync.run_sync(
        root,
        args.target_version,
        allow_branch=args.allow_branch,
        write=not args.dry_run,
    )
    if result.skipped:
        if not args.quiet:
            print(f"skipped: {result.reason}", file=sys.stderr)
        return EXIT_OK

    if args.json:
        print(
            json.dumps(
                {
                    "version": result.version,
                    "patch": (
                        result.patch_path.relative_to(root).as_posix()
                        if result.patch_path
                        else None
                    ),
                    "added": result.added,
                    "removed": result.removed,
                    "changed": result.changed,
                    "baseline_symbols": result.base_count,
                    "current_symbols": result.now_count,
                    "warnings": result.warnings,
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        return EXIT_OK

    if args.dry_run:
        sys.stdout.write(result.patch_text)
        return EXIT_OK

    if not args.quiet:
        print(f"version: {result.version}")
        print(f"symbols: {result.base_count} baseline -> {result.now_count} now")
        _print_changes("added", result.added)
        _print_changes("removed", result.removed)
        _print_changes("changed", result.changed)
        if result.patch_path:
            rel = result.patch_path.relative_to(root).as_posix()
            size = result.patch_path.stat().st_size
            print(f"patch: {rel} ({size} bytes)")
        else:
            print("patch: none (docs are up to date)")
        for warning in result.warnings:
            print(f"warning: {warning}", file=sys.stderr)
    return EXIT_OK


def _print_changes(label: str, items: list[str], limit: int = 12) -> None:
    if not items:
        return
    print(f"{label} ({len(items)}):")
    for item in items[:limit]:
        print(f"  - {item}")
    if len(items) > limit:
        print(f"  ... and {len(items) - limit} more")


# ---------------------------------------------------------------------------
# diff
# ---------------------------------------------------------------------------


def cmd_diff(root: Path, args: argparse.Namespace) -> int:
    config = config_mod.Config.load(root)
    version = normalize_version(args.version)
    patch, _base, _target, _symbols, warnings = sync.compute_patch(root, version, config)
    for warning in warnings:
        print(f"warning: {warning}", file=sys.stderr)
    sys.stdout.write(patch)
    return EXIT_OK


# ---------------------------------------------------------------------------
# check
# ---------------------------------------------------------------------------


def cmd_check(root: Path, args: argparse.Namespace) -> int:
    findings = sync.check_project(root)
    if args.json:
        print(
            json.dumps(
                [{"level": level, "message": message} for level, message in findings],
                indent=2,
                ensure_ascii=False,
            )
        )
    else:
        if not findings:
            print("ok: no inconsistencies found")
        for level, message in findings:
            print(f"{level:<5}  {message}")
    return EXIT_ISSUES if any(level == sync.ERROR for level, _ in findings) else EXIT_OK


# ---------------------------------------------------------------------------
# indev
# ---------------------------------------------------------------------------


def cmd_indev(root: Path, args: argparse.Namespace) -> int:
    command = args.indev_command
    if command == "start":
        return _indev_start(root, args)
    if command == "finish":
        return _indev_finish(root, args)
    return _indev_list(root, args)


def _indev_start(root: Path, args: argparse.Namespace) -> int:
    result = indev.start(root, args.version, args.base)
    if args.json:
        print(
            json.dumps(
                {
                    "version": result.version,
                    "branch": result.branch,
                    "base": result.base,
                    "base_source": result.base_source,
                    "draft_dir": result.draft_dir.relative_to(root).as_posix(),
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        return EXIT_OK
    print(f"base branch: {result.base} (source: {result.base_source})")
    print(f"created branch {result.branch}")
    print(f"draft dir: {result.draft_dir.relative_to(root).as_posix()}/")
    if result.agents_updated:
        print("AGENTS.md: added indev marker block")
    print()
    print("next: run `decisions-api sync` (or a 5-minute timer) and edit the code")
    return EXIT_OK


def _indev_finish(root: Path, args: argparse.Namespace) -> int:
    result = indev.finish(
        root,
        args.version,
        no_merge=args.no_merge,
        resume=args.resume,
        base=args.base,
        close_head=args.close_head,
    )
    if args.json:
        print(
            json.dumps(
                {
                    "version": result.version,
                    "branch": result.branch,
                    "patch_applied": result.patch_applied,
                    "committed": result.committed,
                    "merged": result.merged,
                    "conflict": result.conflict,
                    "base": result.base,
                },
                indent=2,
                ensure_ascii=False,
            )
        )
    else:
        if result.patch_applied:
            print("applied changes.patch to the baseline")
        elif not args.resume:
            print("no pending patch")
        if result.committed:
            print(f"committed baseline for {result.version}")
        if result.conflict:
            print("merge stopped with conflicts (state kept, nothing aborted):")
            print(result.message)
            print()
            print("resolve the conflicts, then run:")
            print(f"  decisions-api indev finish {result.version} --resume")
            return EXIT_CONFLICT
        if result.merged:
            print(f"merged {result.branch} into {result.base} (--no-ff)")
        elif args.no_merge:
            print("merge skipped (--no-merge)")
    if result.conflict:
        return EXIT_CONFLICT
    return EXIT_OK


def _indev_list(root: Path, args: argparse.Namespace) -> int:
    statuses = indev.list_versions(root)
    if args.json:
        print(
            json.dumps(
                [
                    {
                        "version": item.version,
                        "branch": item.branch,
                        "draft": item.draft,
                        "patch": item.patch,
                        "baseline": item.baseline,
                    }
                    for item in statuses
                ],
                indent=2,
                ensure_ascii=False,
            )
        )
        return EXIT_OK
    if not statuses:
        print("docs/api/head is empty")
        return EXIT_OK
    print(f"{'version':<10} {'branch':<8} {'baseline':<10} {'draft':<7} patch")
    for item in statuses:
        print(
            f"{item.version:<10} {'yes' if item.branch else 'no':<8}"
            f" {'yes' if item.baseline else 'no':<10} {'yes' if item.draft else 'no':<7}"
            f" {item.patch}"
        )
    return EXIT_OK


# ---------------------------------------------------------------------------
# release
# ---------------------------------------------------------------------------


def cmd_release(root: Path, args: argparse.Namespace) -> int:
    result = indev.release(root, args.tag)
    if args.json:
        print(
            json.dumps(
                {
                    "tag": result.tag,
                    "version": result.version,
                    "commit": result.commit,
                },
                indent=2,
            )
        )
        return EXIT_OK
    print(f"tagged {result.tag} (commit {result.commit})")
    print(f"push it yourself: git push origin {result.tag}")
    return EXIT_OK


# ---------------------------------------------------------------------------
# head
# ---------------------------------------------------------------------------


def cmd_head(root: Path, args: argparse.Namespace) -> int:
    versions = project.read_head(root)
    if args.head_command == "list":
        if not versions:
            print("docs/api/head is empty")
        for index, version in enumerate(versions):
            marker = " (default)" if index == 0 else ""
            print(f"{version}{marker}")
        return EXIT_OK

    version = normalize_version(args.version)
    if not is_version(version):
        print(f"error: {version!r} is not a valid version (expected vX.Y)", file=sys.stderr)
        return EXIT_ERROR

    if args.head_command == "add":
        created: Path | None = None
        if args.create:
            created = project.version_dir(root, version)
            created.mkdir(parents=True, exist_ok=True)
        if version in versions:
            print(f"{version} is already in head")
        else:
            versions.append(version)
            project.write_head(root, versions)
            print(f"added {version} to docs/api/head")
        if created is not None:
            print(f"created {created.relative_to(root).as_posix()}/")
        return EXIT_OK

    if version not in versions:
        print(f"{version} is not in docs/api/head")
        return EXIT_OK
    versions = [item for item in versions if item != version]
    project.write_head(root, versions)
    print(f"removed {version} from docs/api/head")
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
