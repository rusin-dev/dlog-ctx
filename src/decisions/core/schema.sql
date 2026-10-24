-- decisions-ctx index schema (.decisions/index.db, gitignored)
-- Full rebuild strategy: if any file under docs/ is newer than the index, the
-- whole database is rebuilt. Milliseconds at personal-project scale.

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- ---------------------------------------------------------------------------
-- Decision log
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS decisions (
    id           INTEGER PRIMARY KEY,
    path         TEXT NOT NULL UNIQUE,
    topic        TEXT NOT NULL,
    slug         TEXT NOT NULL,
    title        TEXT NOT NULL,
    date         TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'accepted',
    commit_hash  TEXT,
    branch       TEXT,
    body         TEXT NOT NULL,
    supersedes   TEXT
);

CREATE TABLE IF NOT EXISTS decision_tags (
    decision_id  INTEGER NOT NULL REFERENCES decisions(id) ON DELETE CASCADE,
    tag          TEXT NOT NULL,
    PRIMARY KEY (decision_id, tag)
);

CREATE TABLE IF NOT EXISTS decision_files (
    decision_id  INTEGER NOT NULL REFERENCES decisions(id) ON DELETE CASCADE,
    file_path    TEXT NOT NULL,
    PRIMARY KEY (decision_id, file_path)
);

-- Forward-compatible extra fields (rejected / version) kept out of the main table
CREATE TABLE IF NOT EXISTS decision_meta (
    decision_id  INTEGER NOT NULL REFERENCES decisions(id) ON DELETE CASCADE,
    key          TEXT NOT NULL,
    value        TEXT NOT NULL,
    PRIMARY KEY (decision_id, key)
);

-- frontmatter `symbols` (qualified names such as storage.Database.connect)
CREATE TABLE IF NOT EXISTS decision_symbols (
    decision_id    INTEGER NOT NULL REFERENCES decisions(id) ON DELETE CASCADE,
    qualified_name TEXT NOT NULL,
    PRIMARY KEY (decision_id, qualified_name)
);

CREATE INDEX IF NOT EXISTS idx_decisions_date ON decisions(date);
CREATE INDEX IF NOT EXISTS idx_decisions_topic ON decisions(topic);
CREATE INDEX IF NOT EXISTS idx_decisions_status ON decisions(status);
CREATE INDEX IF NOT EXISTS idx_decision_files_path ON decision_files(file_path);
CREATE INDEX IF NOT EXISTS idx_decision_tags_tag ON decision_tags(tag);

-- ---------------------------------------------------------------------------
-- API documentation index
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS api_versions (
    version      TEXT PRIMARY KEY,                 -- v1.3 / ~v1.3
    base_version TEXT,                             -- ~v1.3 -> v1.3
    status       TEXT NOT NULL DEFAULT 'release',  -- release / draft
    generated_at TEXT
);

CREATE TABLE IF NOT EXISTS api_symbols (
    id             INTEGER PRIMARY KEY,
    version        TEXT NOT NULL,
    qualified_name TEXT NOT NULL,
    kind           TEXT NOT NULL,
    module         TEXT NOT NULL,
    topic          TEXT NOT NULL,
    doc_path       TEXT NOT NULL,
    signature      TEXT NOT NULL DEFAULT '',
    returns        TEXT,
    doc            TEXT,
    source_file    TEXT,
    lineno         INTEGER,
    status         TEXT NOT NULL DEFAULT 'active',
    removed_in     TEXT,
    decision_refs  TEXT,  -- JSON array of @decision:<ref> markers
    UNIQUE (version, qualified_name)
);

CREATE INDEX IF NOT EXISTS idx_api_symbols_qname ON api_symbols(qualified_name);
CREATE INDEX IF NOT EXISTS idx_api_symbols_source ON api_symbols(source_file);
CREATE INDEX IF NOT EXISTS idx_api_symbols_topic ON api_symbols(version, topic);

-- ---------------------------------------------------------------------------
-- Full-text search (FTS5)
--
-- The tokenizer is chosen in Python based on availability: `trigram` (CJK
-- friendly, substring matching) when supported, else `unicode61`. The virtual
-- table is therefore created by db.py instead of being declared here.
-- ---------------------------------------------------------------------------
