"""``decisions`` — the read & record entry point.

Commands: ``init``, ``add``, ``recall``, ``timeline``, ``context``, plus
``decisions api <sub>`` which forwards, in-process, to ``decisions-api``.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from datetime import date
from pathlib import Path
from typing import Any

from . import __version__
from .api.cli import main as api_main
from .core import config as config_mod
from .core import db, frontmatter, gitutils, project
from .core.models import STATUSES, anchor, normalize_version, version_key
from .core.stdio import configure_stdio

EDITOR_TEMPLATE = """
# Write the rationale for this decision.
# Save and close the editor when done; an empty file aborts.
"""


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """Build the ``decisions`` argument parser."""
    parser = argparse.ArgumentParser(
        prog="decisions",
        description="Project context indexer: decision log + API docs, linked in SQLite.",
    )
    parser.add_argument(
        "-C",
        "--root",
        help="project root (defaults to the git toplevel of the current directory)",
    )
    parser.add_argument("--version", action="version", version=f"decisions {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="create docs/decision/, .decisions/ and .gitignore")
    init.add_argument("--json", action="store_true", help="machine readable output")

    add = sub.add_parser("add", help="record one decision")
    add.add_argument("--topic", "-t", required=True, help="free-form topic (directory)")
    add.add_argument("-r", "--reason", help="body text (falls back to stdin or $EDITOR)")
    add.add_argument("--title", help="title (defaults to the first body line)")
    add.add_argument("--tag", "-T", action="append", default=[], help="tag (repeatable)")
    add.add_argument(
        "--rejected",
        action="append",
        default=[],
        help="rejected alternative and why (repeatable)",
    )
    add.add_argument(
        "--file", "-f", action="append", default=[], dest="files", help="related file (repeatable)"
    )
    add.add_argument(
        "--symbol",
        "-s",
        action="append",
        default=[],
        dest="symbols",
        help="related qualified symbol, e.g. storage.Database.connect (repeatable)",
    )
    add.add_argument("--commit", help="commit hash (defaults to HEAD)")
    add.add_argument("--branch", help="branch name (defaults to the current branch)")
    add.add_argument("--status", choices=STATUSES, default="accepted")
    add.add_argument("--supersedes", help="path of the decision this one supersedes")
    add.add_argument("--version", dest="api_version", help="API version this applies to")
    add.add_argument("--date", help="date (defaults to today, YYYY-MM-DD)")
    add.add_argument("--edit", action="store_true", help="force opening $EDITOR")
    add.add_argument("--print-path", action="store_true", help="print only the new path")
    add.add_argument("--json", action="store_true", help="machine readable output")

    recall = sub.add_parser("recall", help="full-text search over the decision log")
    recall.add_argument("query", nargs="?", help="FTS query (optional: filter only)")
    recall.add_argument("--tag", "-T", action="append", default=[], help="require tag")
    recall.add_argument("--since", help="YYYY-MM-DD or relative window 7d/3w/6m/1y")
    recall.add_argument(
        "--file", "-f", action="append", default=[], dest="files", help="require related file"
    )
    recall.add_argument("--status", choices=STATUSES, help="filter by status")
    recall.add_argument("--version", dest="api_version", help="filter by API version")
    recall.add_argument("--topic", help="filter by topic")
    recall.add_argument("--limit", type=int, default=20)
    recall.add_argument("--json", action="store_true")
    recall.add_argument("--paths", action="store_true", help="print paths only")
    recall.add_argument("--topics", action="store_true", help="list existing topics")

    timeline = sub.add_parser("timeline", help="chronological list of decisions")
    timeline.add_argument("--topic")
    timeline.add_argument("--status", choices=STATUSES)
    timeline.add_argument("--limit", type=int, default=500)
    timeline.add_argument("--json", action="store_true")

    context = sub.add_parser(
        "context", help="API contract + decisions for a file (or a qualified symbol)"
    )
    context.add_argument("path", help="file path (or qualified symbol name)")
    context.add_argument("--version", dest="api_version", help="query a specific API version")
    context.add_argument("--json", action="store_true")
    context.add_argument("--limit", type=int, default=50)

    api = sub.add_parser("api", add_help=False, help="forward to decisions-api (same process)")
    api.add_argument("api_args", nargs=argparse.REMAINDER)

    return parser


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point: dispatch one sub-command and return its exit code."""
    configure_stdio()
    argv = list(sys.argv[1:] if argv is None else argv)
    args = build_parser().parse_args(argv)
    if args.root:
        os.environ["DECISIONS_ROOT"] = str(Path(args.root).expanduser().resolve())

    handlers = {
        "init": cmd_init,
        "add": cmd_add,
        "recall": cmd_recall,
        "timeline": cmd_timeline,
        "context": cmd_context,
    }
    if args.command == "api":
        return api_main(args.api_args)

    try:
        return handlers[args.command](args)
    except (ValueError, RuntimeError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------


def cmd_init(args: argparse.Namespace) -> int:
    """Create the decision log, state directory, config and .gitignore entries."""
    root = project.find_root()
    project.decision_root(root).mkdir(parents=True, exist_ok=True)
    project.state_dir(root).mkdir(parents=True, exist_ok=True)
    config_path = config_mod.write_default(root)

    added = []
    for entry in (".decisions/", "docs/api/.lock"):
        if ensure_gitignored(root, entry):
            added.append(entry)

    api = project.api_root(root)
    if api.is_dir() and not project.head_path(root).exists():
        project.head_path(root).write_text("", encoding="utf-8", newline="\n")

    payload = {
        "root": str(root),
        "decision_dir": project.DECISION_DIR,
        "state_dir": project.STATE_DIR,
        "config": str(config_path.relative_to(root).as_posix()),
        "gitignore_added": added,
    }
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(f"initialised project at {root}")
        print(f"  {project.DECISION_DIR}/   decision log")
        print(f"  {project.STATE_DIR}/      index database (gitignored)")
        print(f"  {config_path.relative_to(root).as_posix()}  optional config")
        if added:
            print(f"  .gitignore += {', '.join(added)}")
        print()
        print('next: decisions add --topic <topic> -r "why ..."')
    return 0


def ensure_gitignored(root: Path, entry: str) -> bool:
    """Append ``entry`` to .gitignore when missing. Returns True if modified."""
    path = root / ".gitignore"
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    lines = [line.strip() for line in text.splitlines()]
    if entry in lines:
        return False
    prefix = "" if not text or text.endswith("\n") else "\n"
    path.write_text(f"{text}{prefix}{entry}\n", encoding="utf-8", newline="\n")
    return True


# ---------------------------------------------------------------------------
# add
# ---------------------------------------------------------------------------


def cmd_add(args: argparse.Namespace) -> int:
    """Record one decision (metadata from flags, body from -r/stdin/$EDITOR)."""
    root = project.find_root()
    config = config_mod.Config.load(root)
    topic = _safe_topic(args.topic)

    body = _resolve_body(args)
    if not body.strip():
        raise ValueError("empty body: pass -r, pipe stdin, or omit --edit to use $EDITOR")

    title = (args.title or _first_line(body) or "").strip()
    if not title:
        raise ValueError("cannot derive a title: pass --title")

    decision_date = _parse_date(args.date)
    slug = slugify(title)
    directory = project.decision_root(root) / topic
    directory.mkdir(parents=True, exist_ok=True)
    file_path = _unique_path(directory, f"{decision_date}-{slug}")

    commit = args.commit or (gitutils.head_commit(root) if gitutils.is_repo(root) else None)
    branch = args.branch or (gitutils.current_branch(root) if gitutils.is_repo(root) else None)
    files = [_normalise_file(root, item) for item in args.files]

    meta: dict[str, Any] = {
        "title": title,
        "date": decision_date,
        "status": args.status,
        "commit": commit,
        "branch": branch,
        "version": args.api_version,
        "files": files,
        "symbols": [item.strip() for item in args.symbols if item.strip()],
        "tags": [item.strip() for item in args.tag if item.strip()],
        "rejected": [item.strip() for item in args.rejected if item.strip()],
        "supersedes": args.supersedes,
    }
    text = frontmatter.dump(meta, body)
    file_path.write_text(text, encoding="utf-8", newline="\n")

    rel = file_path.relative_to(root).as_posix()
    conn, warnings = db.ensure_index(root, config, force=True)
    try:
        row = db.decision_by_path(conn, rel)
        decision_id = int(row["id"]) if row else 0
        payload = {
            "path": rel,
            "title": title,
            "topic": topic,
            "date": decision_date,
            "status": args.status,
            "id": decision_id,
            "tags": meta["tags"],
            "files": files,
        }
    finally:
        conn.close()

    for warning in warnings:
        print(f"warning: {warning}", file=sys.stderr)

    if args.print_path:
        print(rel)
    elif args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(f"recorded {rel}")
        print(f"  {decision_date}  {args.status}  {topic}  {title}")
    return 0


def _resolve_body(args: argparse.Namespace) -> str:
    if args.reason:
        return args.reason
    if not args.edit and not sys.stdin.isatty():
        piped = sys.stdin.read()
        if piped.strip():
            return piped
    return _read_from_editor()


def _read_from_editor() -> str:
    editor = os.environ.get("EDITOR") or os.environ.get("VISUAL")
    if not editor:
        raise ValueError("-r is required when $EDITOR is not set")
    with tempfile.NamedTemporaryFile("r+", suffix=".md", delete=False, encoding="utf-8") as handle:
        handle.write(EDITOR_TEMPLATE)
        temp_path = Path(handle.name)
    try:
        proc = subprocess.run([*editor.split(), str(temp_path)], check=False)
        if proc.returncode != 0:
            raise ValueError(f"editor exited with {proc.returncode}")
        text = temp_path.read_text(encoding="utf-8")
    finally:
        temp_path.unlink(missing_ok=True)

    lines = [
        line
        for line in text.splitlines()
        if not line.startswith("# Write the rationale") and line.strip() != "#"
    ]
    body = "\n".join(lines).strip()
    return body


def _safe_topic(topic: str) -> str:
    value = topic.strip().strip("/")
    if not value:
        raise ValueError("--topic must not be empty")
    if ".." in value.split("/"):
        raise ValueError("--topic must not contain '..'")
    value = re.sub(r"\\", "/", value)
    value = re.sub(r"[^\w./-]", "-", value, flags=re.UNICODE)
    value = re.sub(r"/+", "/", value).strip("/")
    if not value:
        raise ValueError("--topic is not a valid directory name")
    return value


def slugify(text: str, limit: int = 60) -> str:
    """ASCII-ish URL/file slug derived from a title (never empty)."""
    value = text.strip().lower()
    value = re.sub(r"[^\w\s-]", "", value, flags=re.UNICODE)
    value = re.sub(r"[\s_]+", "-", value).strip("-")
    value = value[:limit].strip("-")
    return value or "decision"


def _unique_path(directory: Path, stem: str) -> Path:
    candidate = directory / f"{stem}.md"
    counter = 2
    while candidate.exists():
        candidate = directory / f"{stem}-{counter}.md"
        counter += 1
    return candidate


def _first_line(body: str) -> str:
    for line in body.splitlines():
        text = line.strip()
        if text:
            return text.lstrip("#").strip()[:120]
    return ""


def _parse_date(value: str | None) -> str:
    if not value:
        return date.today().isoformat()
    try:
        return date.fromisoformat(value.strip()).isoformat()
    except ValueError as exc:
        raise ValueError(f"invalid --date {value!r} (expected YYYY-MM-DD)") from exc


def _normalise_file(root: Path, value: str) -> str:
    rel = project.rel(root, value)
    if rel.startswith("/") or re.match(r"^[A-Za-z]:/", rel):
        print(f"warning: {value} is outside the repository", file=sys.stderr)
    return rel


# ---------------------------------------------------------------------------
# recall
# ---------------------------------------------------------------------------


def cmd_recall(args: argparse.Namespace) -> int:
    """Full-text search over the decision log (optionally filter-only)."""
    root = project.find_root()
    config = config_mod.Config.load(root)
    conn, warnings = db.ensure_index(root, config)
    try:
        for warning in warnings:
            print(f"warning: {warning}", file=sys.stderr)
        if args.topics:
            rows = db.topics(conn)
            if args.json:
                print(
                    json.dumps(
                        [
                            {
                                "topic": row["topic"],
                                "count": row["count"],
                                "last_date": row["last_date"],
                            }
                            for row in rows
                        ],
                        indent=2,
                        ensure_ascii=False,
                    )
                )
            else:
                for row in rows:
                    print(f"{row['count']:>4}  {row['topic']:<24} last {row['last_date']}")
                if not rows:
                    print("no topics yet")
            return 0

        rows = db.search(
            conn,
            args.query,
            tags=args.tag,
            since=args.since,
            files=[project.rel(root, item) for item in args.files],
            status=args.status,
            version=args.api_version,
            topic=args.topic,
            limit=args.limit,
        )
        if args.paths:
            for row in rows:
                print(row["path"])
        elif args.json:
            print(
                json.dumps(
                    [_decision_json(conn, row) for row in rows], indent=2, ensure_ascii=False
                )
            )
        else:
            if not rows:
                print("no decisions found")
            for row in rows:
                _print_decision(conn, row)
    finally:
        conn.close()
    return 0


def _decision_json(conn, row) -> dict[str, Any]:
    extras = db.decision_meta(conn, row["id"])
    rejected: list[str] = []
    if "rejected" in extras:
        try:
            rejected = json.loads(extras["rejected"])
        except json.JSONDecodeError:
            rejected = [extras["rejected"]]
    return {
        "id": row["id"],
        "path": row["path"],
        "topic": row["topic"],
        "slug": row["slug"],
        "title": row["title"],
        "date": row["date"],
        "status": row["status"],
        "commit": row["commit_hash"],
        "branch": row["branch"],
        "supersedes": row["supersedes"],
        "tags": db.decision_tags(conn, row["id"]),
        "files": db.decision_files(conn, row["id"]),
        "rejected": rejected,
        "version": extras.get("version"),
        "summary": _summary(row["body"]),
        "body": row["body"],
    }


def _print_decision(conn, row) -> None:
    tags = db.decision_tags(conn, row["id"])
    print(f"{row['date']}  {row['status']:<10}  {row['topic']:<20}  {row['title']}")
    print(f"    {row['path']}")
    if tags:
        print(f"    tags: {', '.join(tags)}")
    summary = _summary(row["body"])
    if summary:
        print(f"    {summary}")
    print()


def _summary(body: str, limit: int = 200) -> str:
    text = re.sub(r"\s+", " ", body.strip())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "..."


# ---------------------------------------------------------------------------
# timeline
# ---------------------------------------------------------------------------


def cmd_timeline(args: argparse.Namespace) -> int:
    """Print decisions in chronological order."""
    root = project.find_root()
    config = config_mod.Config.load(root)
    conn, warnings = db.ensure_index(root, config)
    try:
        for warning in warnings:
            print(f"warning: {warning}", file=sys.stderr)
        rows = db.search(
            conn,
            None,
            status=args.status,
            topic=args.topic,
            limit=args.limit,
        )
        rows = sorted(rows, key=lambda row: (row["date"], row["id"]))
        if args.json:
            print(
                json.dumps(
                    [
                        {
                            "date": row["date"],
                            "status": row["status"],
                            "topic": row["topic"],
                            "title": row["title"],
                            "path": row["path"],
                        }
                        for row in rows
                    ],
                    indent=2,
                    ensure_ascii=False,
                )
            )
        else:
            if not rows:
                print("no decisions yet")
            for row in rows:
                print(f"{row['date']}  {row['status']:<10}  {row['topic']:<20}  {row['title']}")
    finally:
        conn.close()
    return 0


# ---------------------------------------------------------------------------
# context
# ---------------------------------------------------------------------------


def cmd_context(args: argparse.Namespace) -> int:
    """Show the API contract and related decisions for a file or symbol."""
    root = project.find_root()
    config = config_mod.Config.load(root)
    conn, warnings = db.ensure_index(root, config)
    try:
        for warning in warnings:
            print(f"warning: {warning}", file=sys.stderr)
        payload = _build_context(conn, root, args)
        if args.json:
            print(json.dumps(payload, indent=2, ensure_ascii=False))
        else:
            _print_context(payload)
    finally:
        conn.close()
    return 0


def _build_context(conn, root: Path, args: argparse.Namespace) -> dict[str, Any]:
    raw = args.path
    path = project.rel(root, raw)
    exists = (root / path).exists()
    notes: list[str] = []
    focus_symbol: str | None = None

    if not exists:
        # Graceful v0.2 behaviour: accept a qualified symbol name too.
        symbol_rows = db.api_symbols(conn, qualified_name=raw)
        if not symbol_rows and "." in raw:
            symbol_rows = [
                row for row in db.api_symbols(conn) if row["qualified_name"].endswith(raw)
            ]
        if symbol_rows:
            focus_symbol = symbol_rows[0]["qualified_name"]
            path = symbol_rows[0]["source_file"] or path
            exists = (root / path).exists()
            notes.append(f"resolved {raw!r} to {focus_symbol} defined in {path}")
        else:
            notes.append(f"{raw} does not exist on disk and matches no indexed symbol")

    version_label, is_draft, api_rows = _resolve_api(
        conn, root, path, args.api_version, focus_symbol
    )

    symbol_payload = []
    ref_decisions: dict[int, object] = {}
    for row in api_rows:
        resolved_refs = []
        for ref in _json_list(row["decision_refs"]):
            matches = db.decisions_for_ref(conn, ref)
            if not matches:
                notes.append(
                    f"stale reference: {row['qualified_name']} references"
                    f" @decision:{ref} which does not exist"
                )
                resolved_refs.append({"ref": ref, "stale": True, "paths": [], "titles": []})
                continue
            resolved_refs.append(
                {
                    "ref": ref,
                    "stale": False,
                    "paths": [match["path"] for match in matches],
                    "titles": [match["title"] for match in matches],
                }
            )
            for match in matches:
                ref_decisions.setdefault(int(match["id"]), match)
        symbol_payload.append(
            {
                "qualified_name": row["qualified_name"],
                "kind": row["kind"],
                "signature": row["signature"],
                "returns": row["returns"],
                "status": row["status"],
                "removed_in": row["removed_in"],
                "doc": row["doc"],
                "doc_path": row["doc_path"],
                "anchor": anchor(row["qualified_name"]),
                "doc_link": f"{row['doc_path']}#{anchor(row['qualified_name'])}",
                "source_file": row["source_file"],
                "lineno": row["lineno"],
                "decision_refs": resolved_refs,
            }
        )

    rows = list(db.decisions_for_file(conn, path))
    if focus_symbol:
        rows += db.decisions_for_symbol(conn, focus_symbol)
    if ref_decisions:
        rows += list(ref_decisions.values())
    seen_ids: set[int] = set()
    unique_rows = []
    for row in rows:
        if int(row["id"]) in seen_ids:
            continue
        seen_ids.add(int(row["id"]))
        unique_rows.append(row)
    unique_rows.sort(key=lambda row: (row["date"], row["id"]), reverse=True)
    rows = unique_rows[: args.limit]

    decisions = []
    supersede = []
    for row in rows:
        item = _decision_json(conn, row)
        item.pop("body", None)
        stale = [name for name in item["files"] if not (root / name).exists()]
        item["stale_files"] = stale
        for name in stale:
            notes.append(f"stale reference: {row['path']} references {name} which no longer exists")
        decisions.append(item)

        replaced_by = db.superseded_by(conn, row["path"])
        if row["status"] == "superseded" or replaced_by:
            supersede.append(
                {
                    "path": row["path"],
                    "title": row["title"],
                    "status": row["status"],
                    "supersedes": row["supersedes"],
                    "superseded_by": [alt["path"] for alt in replaced_by],
                }
            )

    return {
        "path": path,
        "exists": exists,
        "input": raw,
        "focus_symbol": focus_symbol,
        "api": {
            "version": version_label,
            "draft": is_draft,
            "symbols": symbol_payload,
        },
        "decisions": decisions,
        "supersede": supersede,
        "notes": notes,
    }


def _json_list(raw: object) -> list[str]:
    """Decode a JSON array column into a list of strings (tolerantly)."""
    if not raw:
        return []
    if isinstance(raw, list):
        return [str(item) for item in raw]
    try:
        value = json.loads(str(raw))
    except json.JSONDecodeError:
        return [str(raw)]
    if isinstance(value, list):
        return [str(item) for item in value]
    return [str(value)]


def _resolve_api(
    conn,
    root: Path,
    path: str,
    requested: str | None,
    focus_symbol: str | None,
):
    """Choose the API version to answer from.

    Priority: explicit ``--version`` > the in-development version of the current
    branch > drafts and baselines listed in ``head`` > anything else indexed.
    While developing, "current contract" means the draft.
    """
    candidates: list[str] = []
    if requested:
        version = normalize_version(requested)
        candidates = [f"~{version}", version]
    else:
        head = project.read_head(root)
        branch = gitutils.current_branch(root) or ""
        if branch.startswith("indev-"):
            branch_version = normalize_version(branch[len("indev-") :])
            if branch_version in head:
                candidates += [f"~{branch_version}", branch_version]
        indexed = [row["version"] for row in db.api_versions(conn)]
        released = sorted(
            (version for version in indexed if not version.startswith("~")),
            key=version_key,
            reverse=True,
        )
        drafts = sorted(
            (version for version in indexed if version.startswith("~")),
            key=version_key,
            reverse=True,
        )
        # Default answer = newest confirmed baseline; a draft only wins on its
        # own indev branch (the code there no longer matches the baseline).
        candidates += released
        candidates += [f"~{version}" for version in head] + drafts
    candidates = list(dict.fromkeys(candidates))

    for candidate in candidates:
        rows = db.api_symbols(conn, version=candidate, source_file=path)
        if not rows and (root / path).is_dir():
            rows = [
                row
                for row in db.api_symbols(conn, version=candidate)
                if row["source_file"] and row["source_file"].startswith(path.rstrip("/") + "/")
            ]
        if focus_symbol:
            rows = [row for row in rows if row["qualified_name"] == focus_symbol] or rows
        if rows:
            return candidate, candidate.startswith("~"), rows
    version = candidates[0] if candidates else None
    return version, bool(version and version.startswith("~")), []


def _print_context(payload: dict[str, Any]) -> None:
    path = payload["path"]
    print(f"# context: {path}")
    print()
    print(f"file exists: {'yes' if payload['exists'] else 'NO'}")
    for note in payload["notes"]:
        print(f"note: {note}")
    print()

    api = payload["api"]
    version = api["version"]
    label = f"{version} ({'draft' if api['draft'] else 'released'})" if version else "not indexed"
    print(f"## API - {label}")
    if not api["symbols"]:
        print("(no indexed symbols for this path)")
    for symbol in api["symbols"]:
        suffix = ""
        if symbol["status"] == "removed":
            suffix = f"  [removed in {symbol['removed_in'] or '?'}]"
        print(f"- {symbol['qualified_name']}  ({symbol['kind']}){suffix}")
        if symbol["status"] == "active" and symbol["signature"]:
            print(f"    {symbol['signature'].splitlines()[0]}")
        print(f"    docs: {symbol['doc_link']}")
        for ref in symbol.get("decision_refs", []):
            if ref["stale"]:
                print(f"    stale decision ref: @decision:{ref['ref']}")
            else:
                print(f"    decisions: {'; '.join(ref['titles'])}")
    print()

    print(f"## Decisions ({len(payload['decisions'])}, newest first)")
    if not payload["decisions"]:
        print("(none recorded for this path)")
    for decision in payload["decisions"]:
        print(f"### {decision['date']} - {decision['status']} - {decision['title']}")
        print(f"path: {decision['path']}")
        if decision["tags"]:
            print(f"tags: {', '.join(decision['tags'])}")
        if decision["rejected"]:
            print(f"rejected: {'; '.join(decision['rejected'])}")
        if decision["stale_files"]:
            print(f"stale: {', '.join(decision['stale_files'])}")
        if decision["summary"]:
            print(decision["summary"])
        print()

    if payload["supersede"]:
        print("## Supersede chain")
        for item in payload["supersede"]:
            if item["superseded_by"]:
                print(f"- {item['path']} -> superseded by {', '.join(item['superseded_by'])}")
            elif item["supersedes"]:
                print(f"- {item['path']} -> supersedes {item['supersedes']}")
            else:
                print(f"- {item['path']} -> status superseded (replacement not recorded)")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
