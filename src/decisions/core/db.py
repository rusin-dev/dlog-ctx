"""SQLite index: schema, full rebuild, and query helpers.

The database lives at ``.decisions/index.db`` (gitignored) and is always
derived state: ``docs/`` is the source of truth, so any newer mtime under
``docs/`` triggers a full rebuild. At personal-project scale this takes
milliseconds, which removes every incremental-update bug class.

Both entry points share :func:`ensure_index`; whichever runs first rebuilds.
WAL mode keeps concurrent readers happy.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterable, Sequence
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from ..api import render
from ..core import config as config_mod
from ..core import frontmatter, project
from ..core.models import Decision, normalize_version

SCHEMA_PATH = Path(__file__).with_name("schema.sql")
FTS_TABLE = "decisions_fts"
FINGERPRINT_KEY = "docs_fingerprint"
REBUILD_DRAFT_PREFIX = "~"


class IndexBuildError(RuntimeError):
    """Raised when the index cannot be built."""


# ---------------------------------------------------------------------------
# Connection / schema
# ---------------------------------------------------------------------------


def connect(db_path: Path | str) -> sqlite3.Connection:
    """Open (and migrate) the index database."""
    if str(db_path) != ":memory:":
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    ensure_schema(conn)
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    _migrate(conn)
    _ensure_fts(conn)
    conn.commit()


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns introduced after a database was first created."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(api_symbols)")}
    if "decision_refs" not in columns:
        conn.execute("ALTER TABLE api_symbols ADD COLUMN decision_refs TEXT")


def _ensure_fts(conn: sqlite3.Connection) -> None:
    existing = conn.execute(
        "SELECT name FROM sqlite_master WHERE name = ?", (FTS_TABLE,)
    ).fetchone()
    if existing:
        return
    # trigram handles CJK (and substring queries) well; unicode61 is the fallback.
    for tokenizer in ("trigram", "unicode61 remove_diacritics 2"):
        try:
            conn.execute(
                f"CREATE VIRTUAL TABLE {FTS_TABLE} USING fts5("
                f"title, body, content=decisions, content_rowid=id, "
                f'tokenize="{tokenizer}")'
            )
            conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES('fts_tokenizer', ?)",
                (tokenizer.split()[0],),
            )
            return
        except sqlite3.OperationalError:  # pragma: no cover - depends on sqlite build
            continue
    raise IndexBuildError("this SQLite build has no FTS5 support")  # pragma: no cover


def fts_tokenizer(conn: sqlite3.Connection) -> str:
    """Tokenizer name, used to decide when a query is too short for trigram."""
    try:
        row = conn.execute("SELECT value FROM meta WHERE key = 'fts_tokenizer'").fetchone()
    except sqlite3.DatabaseError:  # pragma: no cover
        return ""
    return str(row["value"]) if row else ""


# ---------------------------------------------------------------------------
# Index lifecycle
# ---------------------------------------------------------------------------


def docs_fingerprint(root: Path) -> str:
    """Latest mtime under ``docs/`` (files *and* directories, so deletes count)."""
    latest = 0
    docs = root / "docs"
    if not docs.exists():
        return "0"
    for dirpath, _dirnames, filenames in os.walk(docs):
        try:
            latest = max(latest, int(os.path.getmtime(dirpath) * 1000))
        except OSError:
            continue
        for name in filenames:
            if name == ".lock":
                continue
            try:
                latest = max(latest, int(os.path.getmtime(os.path.join(dirpath, name)) * 1000))
            except OSError:
                continue
    return str(latest)


def is_stale(conn: sqlite3.Connection, root: Path) -> bool:
    try:
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (FINGERPRINT_KEY,)).fetchone()
    except sqlite3.DatabaseError:
        return True
    return row is None or row["value"] != docs_fingerprint(root)


def ensure_index(
    root: Path,
    config: config_mod.Config | None = None,
    *,
    force: bool = False,
) -> tuple[sqlite3.Connection, list[str]]:
    """Return a connection with a fresh index, rebuilding it when needed."""
    config = config or config_mod.Config.load(root)
    conn = connect(config.index_path)
    if force or is_stale(conn, root):
        warnings = rebuild(conn, root, config)
        return conn, warnings
    return conn, []


def rebuild(
    conn: sqlite3.Connection,
    root: Path,
    config: config_mod.Config | None = None,
) -> list[str]:
    """Drop all derived rows and re-scan ``docs/``.

    Draft (``~V``) rows are produced by ``sync`` and have no on-disk documents,
    so they are preserved across rebuilds.
    """
    warnings: list[str] = []
    conn.execute("DELETE FROM decisions")
    conn.execute("DELETE FROM api_symbols WHERE version NOT LIKE '~%'")
    conn.execute("DELETE FROM api_versions WHERE version NOT LIKE '~%'")

    warnings.extend(_load_decisions(conn, root))
    warnings.extend(_load_api_docs(conn, root))
    _prune_orphan_drafts(conn, root)
    conn.execute("INSERT INTO decisions_fts(decisions_fts) VALUES('rebuild')")
    conn.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)",
        (FINGERPRINT_KEY, docs_fingerprint(root)),
    )
    conn.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)",
        ("indexed_at", datetime.now().isoformat(timespec="seconds")),
    )
    conn.commit()
    return warnings


def _load_decisions(conn: sqlite3.Connection, root: Path) -> list[str]:
    warnings: list[str] = []
    base = project.decision_root(root)
    if not base.exists():
        return warnings
    for path in sorted(base.rglob("*.md")):
        rel = path.relative_to(root).as_posix()
        try:
            text = path.read_text(encoding="utf-8")
            decision = decision_from_text(rel, text)
        except (OSError, ValueError, frontmatter.FrontmatterError) as exc:
            warnings.append(f"{rel}: {exc}")
            continue
        insert_decision(conn, decision)
    return warnings


def decision_from_text(rel_path: str, text: str) -> Decision:
    """Build a :class:`Decision` from a document path and its content."""
    meta, body = frontmatter.parse(text)
    parts = rel_path.split("/")
    if len(parts) < 4 or parts[0] != "docs" or parts[1] != "decision":
        raise ValueError("decision file must live under docs/decision/<topic>/")
    topic = parts[2]
    slug = "/".join(parts[3:])[: -len(".md")]
    title = str(meta.get("title") or _title_from_body(body) or slug).strip()
    date_value = str(meta.get("date") or _date_from_slug(slug) or "").strip()
    status = str(meta.get("status") or "accepted").strip() or "accepted"
    return Decision(
        path=rel_path,
        topic=topic,
        slug=slug,
        title=title,
        date=date_value,
        status=status,
        commit_hash=_opt(meta.get("commit")),
        branch=_opt(meta.get("branch")),
        body=body.strip("\n"),
        supersedes=_opt(meta.get("supersedes")),
        tags=_as_list(meta.get("tags")),
        files=_as_list(meta.get("files")),
        rejected=_as_list(meta.get("rejected")),
        symbols=_as_list(meta.get("symbols")),
        version=_opt(meta.get("version")),
    )


def insert_decision(conn: sqlite3.Connection, decision: Decision) -> int:
    cursor = conn.execute(
        """
        INSERT INTO decisions
            (path, topic, slug, title, date, status, commit_hash, branch, body, supersedes)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            decision.path,
            decision.topic,
            decision.slug,
            decision.title,
            decision.date,
            decision.status,
            decision.commit_hash,
            decision.branch,
            decision.body,
            decision.supersedes,
        ),
    )
    decision_id = int(cursor.lastrowid or 0)
    conn.executemany(
        "INSERT OR IGNORE INTO decision_tags(decision_id, tag) VALUES(?, ?)",
        [(decision_id, tag) for tag in decision.tags],
    )
    conn.executemany(
        "INSERT OR IGNORE INTO decision_files(decision_id, file_path) VALUES(?, ?)",
        [(decision_id, path) for path in decision.files],
    )
    conn.executemany(
        "INSERT OR IGNORE INTO decision_symbols(decision_id, qualified_name) VALUES(?, ?)",
        [(decision_id, name) for name in decision.symbols],
    )
    extras = {}
    if decision.rejected:
        extras["rejected"] = json.dumps(decision.rejected, ensure_ascii=False)
    if decision.version:
        extras["version"] = decision.version
    if extras:
        conn.executemany(
            "INSERT OR REPLACE INTO decision_meta(decision_id, key, value) VALUES(?, ?, ?)",
            [(decision_id, key, value) for key, value in extras.items()],
        )
    return decision_id


def _load_api_docs(conn: sqlite3.Connection, root: Path) -> list[str]:
    warnings: list[str] = []
    base = project.api_root(root)
    if not base.exists():
        return warnings
    for version_dir in sorted(base.iterdir()):
        if not version_dir.is_dir() or version_dir.name.startswith("."):
            continue
        version = version_dir.name
        if version.startswith(REBUILD_DRAFT_PREFIX):
            continue  # drafts come from sync, not from disk
        docs: dict[str, str] = {}
        for path in sorted(version_dir.rglob("*.md")):
            rel = path.relative_to(root).as_posix()
            try:
                docs[rel] = path.read_text(encoding="utf-8")
            except OSError as exc:
                warnings.append(f"{rel}: {exc}")
        symbols = render.symbols_from_docs(docs)
        replace_api_index(conn, version, symbols, status="release")
    return warnings


def _prune_orphan_drafts(conn: sqlite3.Connection, root: Path) -> None:
    """Drop draft rows whose ``docs/api/~V/`` directory no longer exists."""
    rows = conn.execute("SELECT version FROM api_versions WHERE version LIKE '~%'").fetchall()
    for row in rows:
        version = row["version"]
        if not (project.api_root(root) / version).is_dir():
            conn.execute("DELETE FROM api_symbols WHERE version = ?", (version,))
            conn.execute("DELETE FROM api_versions WHERE version = ?", (version,))


def replace_api_index(
    conn: sqlite3.Connection,
    version: str,
    symbols: Iterable[Any],
    *,
    base_version: str | None = None,
    status: str = "release",
) -> None:
    """Atomically replace the symbol set for one (release or draft) version."""
    version = version if version.startswith("~") else normalize_version(version)
    conn.execute("DELETE FROM api_symbols WHERE version = ?", (version,))
    rows = [
        (
            version,
            symbol.qualified_name,
            symbol.kind,
            symbol.module,
            symbol.topic,
            symbol.doc_path,
            symbol.signature or "",
            symbol.returns,
            symbol.doc,
            symbol.source_file,
            symbol.lineno,
            symbol.status,
            symbol.removed_in,
            json.dumps(symbol.decision_refs, ensure_ascii=False) if symbol.decision_refs else None,
        )
        for symbol in symbols
    ]
    conn.executemany(
        """
        INSERT OR REPLACE INTO api_symbols
            (version, qualified_name, kind, module, topic, doc_path, signature,
             returns, doc, source_file, lineno, status, removed_in, decision_refs)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    conn.execute(
        """
        INSERT INTO api_versions(version, base_version, status, generated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(version) DO UPDATE SET
            base_version = excluded.base_version,
            status = excluded.status,
            generated_at = excluded.generated_at
        """,
        (
            version,
            base_version,
            status,
            datetime.now().isoformat(timespec="seconds"),
        ),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------


def search(
    conn: sqlite3.Connection,
    query: str | None = None,
    *,
    tags: Sequence[str] = (),
    since: str | None = None,
    files: Sequence[str] = (),
    status: str | None = None,
    version: str | None = None,
    topic: str | None = None,
    limit: int = 20,
) -> list[sqlite3.Row]:
    """Search decisions. ``query`` may be ``None`` to filter only."""
    where: list[str] = []
    params: list[Any] = []
    if since:
        where.append("d.date >= ?")
        params.append(_since_date(since))
    if status:
        where.append("d.status = ?")
        params.append(status)
    if topic:
        where.append("d.topic = ?")
        params.append(topic)
    for tag in tags:
        where.append(
            "EXISTS (SELECT 1 FROM decision_tags t WHERE t.decision_id = d.id AND t.tag = ?)"
        )
        params.append(tag)
    for path in files:
        where.append(
            "EXISTS (SELECT 1 FROM decision_files f WHERE f.decision_id = d.id"
            " AND (f.file_path = ? OR f.file_path LIKE '%/' || ?))"
        )
        params.extend([path, path])
    if version:
        where.append(
            "EXISTS (SELECT 1 FROM decision_meta m WHERE m.decision_id = d.id"
            " AND m.key = 'version' AND m.value = ?)"
        )
        params.append(version)

    base_sql = "SELECT d.* FROM decisions d"
    if query:
        stripped = query.strip()
        # trigram indexes need at least 3 characters and silently return nothing
        # for shorter queries; go straight to the substring scan in that case.
        short_for_trigram = fts_tokenizer(conn) == "trigram" and len(stripped) < 3
        if not short_for_trigram:
            clause = " AND ".join(where)
            sql = (
                f"{base_sql} JOIN {FTS_TABLE} f ON f.rowid = d.id "
                f"WHERE {FTS_TABLE} MATCH ?"
                + (f" AND {clause}" if clause else "")
                + " ORDER BY bm25("
                + FTS_TABLE
                + ") LIMIT ?"
            )
            try:
                rows = conn.execute(sql, [query, *params, limit]).fetchall()
                if rows:
                    return rows
            except sqlite3.OperationalError:
                pass  # odd FTS syntax: degrade to a substring scan
        return _search_like(conn, stripped, where, params, limit)

    clause = " AND ".join(where) if where else "1"
    sql = f"{base_sql} WHERE {clause} ORDER BY d.date DESC, d.id DESC LIMIT ?"
    return conn.execute(sql, [*params, limit]).fetchall()


def _search_like(
    conn: sqlite3.Connection,
    query: str,
    where: list[str],
    params: list[Any],
    limit: int,
) -> list[sqlite3.Row]:
    like = f"%{query}%"
    clause = " AND ".join(["d.id > 0", *where])
    sql = (
        f"SELECT d.* FROM decisions d WHERE {clause}"
        " AND (d.title LIKE ? OR d.body LIKE ?)"
        " ORDER BY d.date DESC, d.id DESC LIMIT ?"
    )
    return conn.execute(sql, [*params, like, like, limit]).fetchall()


def timeline(conn: sqlite3.Connection, limit: int = 500) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM decisions ORDER BY date ASC, id ASC LIMIT ?", (limit,)
    ).fetchall()


def topics(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT topic, COUNT(*) AS count, MAX(date) AS last_date"
        " FROM decisions GROUP BY topic ORDER BY topic"
    ).fetchall()


def decision_by_path(conn: sqlite3.Connection, path: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM decisions WHERE path = ?", (path,)).fetchone()


def decision_tags(conn: sqlite3.Connection, decision_id: int) -> list[str]:
    rows = conn.execute(
        "SELECT tag FROM decision_tags WHERE decision_id = ? ORDER BY tag", (decision_id,)
    ).fetchall()
    return [row["tag"] for row in rows]


def decision_files(conn: sqlite3.Connection, decision_id: int) -> list[str]:
    rows = conn.execute(
        "SELECT file_path FROM decision_files WHERE decision_id = ? ORDER BY file_path",
        (decision_id,),
    ).fetchall()
    return [row["file_path"] for row in rows]


def decision_meta(conn: sqlite3.Connection, decision_id: int) -> dict[str, str]:
    rows = conn.execute(
        "SELECT key, value FROM decision_meta WHERE decision_id = ?", (decision_id,)
    ).fetchall()
    return {row["key"]: row["value"] for row in rows}


def decisions_for_file(
    conn: sqlite3.Connection, path: str, *, status: str | None = None
) -> list[sqlite3.Row]:
    sql = (
        "SELECT d.* FROM decisions d"
        " JOIN decision_files f ON f.decision_id = d.id"
        " WHERE (f.file_path = ? OR f.file_path LIKE '%/' || ?)"
    )
    params: list[Any] = [path, path]
    if status:
        sql += " AND d.status = ?"
        params.append(status)
    sql += " ORDER BY d.date DESC, d.id DESC"
    return conn.execute(sql, params).fetchall()


def decisions_for_symbol(conn: sqlite3.Connection, name: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT d.* FROM decisions d"
        " JOIN decision_symbols s ON s.decision_id = d.id"
        " WHERE s.qualified_name = ? ORDER BY d.date DESC, d.id DESC",
        (name,),
    ).fetchall()


def decisions_for_ref(conn: sqlite3.Connection, ref: str) -> list[sqlite3.Row]:
    """Resolve a docstring reference to decision rows.

    Accepted forms: a repo path (``docs/decision/t/2026-01-01-x.md``), a bare
    slug (``use-sqlite``, matched against the date-prefixed slug), or
    ``topic/slug``. Returns newest first; an empty list means a stale reference.
    """
    ref = ref.strip().strip("/")
    if not ref:
        return []
    if ref.endswith(".md"):
        rows = conn.execute(
            "SELECT * FROM decisions WHERE path = ? OR path LIKE '%/' || ?"
            " ORDER BY date DESC, id DESC",
            (ref, ref),
        ).fetchall()
        if rows:
            return rows
    topic = None
    slug = ref
    if "/" in ref:
        topic, slug = ref.split("/", 1)
    sql = "SELECT * FROM decisions WHERE (slug = ? OR slug LIKE '%-' || ?)"
    params: list[Any] = [slug, slug]
    if topic:
        sql += " AND topic = ?"
        params.append(topic)
    sql += " ORDER BY date DESC, id DESC"
    return conn.execute(sql, params).fetchall()


def superseded_by(conn: sqlite3.Connection, path: str) -> list[sqlite3.Row]:
    """Decisions that declare ``supersedes: <path>``."""
    return conn.execute(
        "SELECT * FROM decisions WHERE supersedes = ? ORDER BY date ASC", (path,)
    ).fetchall()


def api_versions(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM api_versions ORDER BY version DESC").fetchall()


def api_version(conn: sqlite3.Connection, version: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM api_versions WHERE version = ?", (version,)).fetchone()


def api_symbols(
    conn: sqlite3.Connection,
    *,
    version: str | None = None,
    source_file: str | None = None,
    qualified_name: str | None = None,
) -> list[sqlite3.Row]:
    where: list[str] = []
    params: list[Any] = []
    if version:
        where.append("version = ?")
        params.append(version)
    if source_file:
        where.append("source_file = ?")
        params.append(source_file)
    if qualified_name:
        where.append("qualified_name = ?")
        params.append(qualified_name)
    clause = " AND ".join(where) if where else "1"
    return conn.execute(
        f"SELECT * FROM api_symbols WHERE {clause} ORDER BY qualified_name", params
    ).fetchall()


def api_topics(conn: sqlite3.Connection, version: str) -> list[str]:
    rows = conn.execute(
        "SELECT DISTINCT topic FROM api_symbols WHERE version = ? ORDER BY topic",
        (version,),
    ).fetchall()
    return [row["topic"] for row in rows]


def drop_api_version(conn: sqlite3.Connection, version: str) -> None:
    """Remove every row for one version (used when a draft cycle ends)."""
    conn.execute("DELETE FROM api_symbols WHERE version = ?", (version,))
    conn.execute("DELETE FROM api_versions WHERE version = ?", (version,))
    conn.commit()


def stats(conn: sqlite3.Connection) -> dict[str, int]:
    def count(table: str) -> int:
        row = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()
        return int(row["n"]) if row else 0

    return {
        "decisions": count("decisions"),
        "tags": count("decision_tags"),
        "files": count("decision_files"),
        "api_symbols": count("api_symbols"),
        "api_versions": count("api_versions"),
    }


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _since_date(value: str) -> str:
    """Accept ``YYYY-MM-DD`` or a relative window such as ``7d``/``3w``/``6m``/``1y``."""
    value = value.strip()
    if not value:
        return "0000-00-00"
    if value[-1:].lower() in ("d", "w", "m", "y") and value[:-1].isdigit():
        amount = int(value[:-1])
        unit = value[-1].lower()
        days = {"d": 1, "w": 7, "m": 30, "y": 365}[unit] * amount
        return (date.today() - timedelta(days=days)).isoformat()
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise ValueError(f"invalid --since value {value!r}: use YYYY-MM-DD or 7d/3w/6m/1y") from exc


def _title_from_body(body: str) -> str | None:
    for line in body.splitlines():
        text = line.strip().lstrip("#").strip()
        if text:
            return text[:120]
    return None


def _date_from_slug(slug: str) -> str | None:
    head = slug.split("/")[-1][:10]
    try:
        return date.fromisoformat(head).isoformat()
    except ValueError:
        return None


def _as_list(value: Any) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    return [str(value)]


def _opt(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
