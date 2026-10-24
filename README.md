# decisions-ctx

A **single-project context indexer**: it keeps the *current contract* of your code
(API docs generated from source) and the *reason it evolved* (decision log written by
humans) in sync, and links both through SQLite.

The exit point is one command:

```console
$ decisions context src/storage.py
```

It answers the three questions you have when you come back to a project after three
months – or when an AI agent is about to change a file:

1. **What is the contract now?** Symbols exported by that file: signature, return type,
   doc link to `docs/api/<version>/`.
2. **Why is it like this?** Decisions that touched this file, newest first, with the
   rejected alternatives.
3. **What else does this change affect?** Supersede chains, removed symbols, and stale
   references to files that no longer exist.

## Not in scope

MCP server, web UI, cross-project search, vector search, team approval workflows.
The whole thing is stdlib-only Python (SQLite + `ast` + `argparse`).

## Install

```console
uv tool install decisions-ctx      # or: pipx install decisions-ctx
# from a checkout:
uv pip install -e .
```

Two console scripts are installed and share one implementation:

| entry point     | audience | responsibilities |
| --------------- | -------- | ---------------- |
| `decisions`     | humans & agents | read and record: `init`, `add`, `recall`, `timeline`, `context` |
| `decisions-api` | maintainers     | generate docs from code: `sync`, `indev`, `release`, `check`, `diff` |

`decisions api <sub>` forwards to `decisions-api` **in the same process**, so both
forms are interchangeable.

## Quick start (decision log)

```console
$ cd my-project
$ decisions init
initialised project at /path/to/my-project
  docs/decision/   decision log
  .decisions/      index database (gitignored)
  .decisions/config.toml  optional config
  .gitignore += .decisions/, docs/api/.lock

$ decisions add --topic storage \
    -r "Chose SQLite over Postgres: deployment cost dominates for a single-user tool." \
    --title "SQLite instead of Postgres" \
    --tag storage --tag database \
    --file src/storage.py \
    --rejected "Postgres (deployment cost)" --rejected "JSON files (no querying)"
recorded docs/decision/storage/2026-10-24-sqlite-instead-of-postgres.md

$ decisions recall Postgres
2026-10-24  accepted    storage     SQLite instead of Postgres
    docs/decision/storage/2026-10-24-sqlite-instead-of-postgres.md
    tags: database, storage
    Chose SQLite over Postgres: deployment cost dominates ...

$ decisions recall --topics
   1  storage                  last 2026-10-24
```

One decision = one file at `docs/decision/<topic>/<YYYY-MM-DD>-<slug>.md`.
`--topic` is a free-form string (the directory is created on demand); `recall --topics`
lists existing topics so naming converges naturally. The commit hash and branch are
captured automatically.

Body input, in priority order: `-r/--reason`, piped stdin, `$EDITOR` (use `--edit` to
force the editor). Title defaults to the first body line. `--rejected`, `--file`,
`--symbol`, `--tag`, `--status`, `--supersedes`, `--version`, `--date` are optional.

### File format

```markdown
---
title: SQLite instead of Postgres
date: 2026-10-24
status: accepted          # accepted / superseded / deprecated
commit: a1b2c3d
branch: main
files:
  - src/storage.py
symbols:
  - storage.Database.connect
tags:
  - storage
  - database
rejected:
  - Postgres (deployment cost)
supersedes: docs/decision/storage/2026-09-01-use-postgres.md
---

Reasoning...
```

### Querying

```console
decisions recall <query>            # FTS5 (trigram when available, so CJK works)
decisions recall --tag storage --since 6m --status accepted
decisions recall --file src/storage.py --json
decisions recall --version v1.3      # decisions tagged with an API version
decisions timeline [--topic storage] [--json]
```

`--since` accepts `YYYY-MM-DD` or a relative window (`7d`, `3w`, `6m`, `1y`).
`--json` is stable and intended for agents called through a shell.

### The index is disposable

`.decisions/index.db` is derived state and gitignored. Whenever anything under `docs/`
is newer than the index fingerprint, it is rebuilt from scratch (milliseconds at
personal-project scale). `docs/` is the single source of truth, so there is no
migration or corruption class to worry about.

## Quick start (API docs + version lifecycle)

### 1. Bootstrap a baseline

```console
$ decisions-api head add v1.0 --create
added v1.0 to docs/api/head
created docs/api/v1.0/
$ git add -A && git commit -m "bootstrap api v1.0"
```

`docs/api/head` lists in-development versions, one per line; the first line is the
default target. Directory names stop at **minor** granularity (`v1.3`); patch releases
(`v1.3.1`, `v1.3.2`) update the same directory and are marked by git tags.

### 2. Develop on an `indev-` branch

```console
$ decisions-api indev start v1.0
base branch: main (source: local branch probe)
created branch indev-v1.0
draft dir: docs/api/~v1.0/
AGENTS.md: added indev marker block
```

`indev start` requires a clean tree, an existing `docs/api/v1.0/`, and a non-existing
`indev-v1.0` branch. The base branch is detected from `origin/HEAD`, then
`init.defaultBranch`, then `main`/`master`/`dev`; pass `--base <branch>` to be explicit.
The printed `source:` lets you spot a bad detection immediately.

It also inserts a marker range into `AGENTS.md` (after the first heading) so agents
know which version is being developed:

```markdown
<!-- decisions:indev -->
> Currently developing the content of `indev-v1.0`.
<!-- /decisions:indev -->
```

Several versions can be in flight at once; each block names its `indev-` branch and
`finish` deletes exactly the matching pair.

### 3. Generate drafts

```console
$ decisions-api sync
version: v1.0
symbols: 0 baseline -> 6 now
added (6):
  - app.App.run
  ...
patch: docs/api/~v1.0/changes.patch (2497 bytes)
```

`sync` is **stateless**: run it from cron/systemd-timer every 5 minutes, or use
`sync --watch 300` for convenience. Rules it will not break:

* It writes exactly two places: `docs/api/~<version>/changes.patch` and the index.
  `docs/api/<version>/` stays clean in `git status` for the whole cycle, so the commit
  boundary at `finish` is deterministic.
* It refuses to run off the `indev-<version>` branch (`--allow-branch` is an escape
  hatch). Without `--version` it targets the version implied by the current branch.
* If another process holds `docs/api/.lock`, the round is silently skipped (exit 0).
* Rendering returns strings; the diff against disk is computed in memory. Nothing is
  applied until you ask for it.
* `docs/api/~<version>/` is created with `.gitignore` containing `*` and `!.gitignore`.

`decisions-api diff v1.3` prints the same unified diff without writing anything.

### 4. Release a point (optional, any time)

```console
$ decisions-api release v1.0.1
tagged v1.0.1 (commit 1bd3fe0)
push it yourself: git push origin v1.0.1
```

Runs on `indev-v1.0`, requires a clean tree and an empty/absent patch, creates an
annotated tag only. **No new branch, no `head` change, no merge, no push.**

### 5. Confirm the baseline

```console
$ decisions-api indev finish v1.0
applied changes.patch to the baseline
committed baseline for v1.0
merged indev-v1.0 into main (--no-ff)
```

`finish` applies the patch with `git apply` (never `git am`: the patch is a plain diff),
removes the `AGENTS.md` marker range, deletes the draft directory, commits
`docs/api/v1.0/` + `AGENTS.md`, then merges `--no-ff` into the base branch.

* `--no-merge` stops before the merge.
* On conflicts it **keeps the conflict state and never aborts**. Resolve, `git add`
  the files, then run `indev finish v1.0 --resume` (from the base branch).
* `--close-head` drops the version line from `docs/api/head` in the same commit.

### 6. Inspect

```console
$ decisions-api indev list
version    branch   baseline   draft   patch
v1.0       yes      yes        no      none

$ decisions-api check
warn   v1.0 is listed in head but docs/api/v1.0/ already exists
info   branch indev-v1.0 exists
```

`check` (exit code 1 on real errors) reports: a version already confirmed while still
listed in `head`, `~V/` without a matching `head` entry (orphan), a draft with no
`indev-` branch, an empty patch file, and released API docs modified locally.

### Generated document format

```
docs/api/v1.0/<topic>/index.md
```

One file per topic (a topic defaults to the first module component under the source
root, and can be remapped in config). Every symbol is wrapped in anchors, so links keep
working even after the symbol is gone:

````markdown
<!-- decisions:api:storage.Database.connect:start -->

## storage.Database.connect

```python
def connect(self, dsn: str) -> Connection
```

Open a connection.

- **Module**: `storage`
- **Kind**: `method`
- **Defined in**: `src/storage.py:12`
- **Returns**: `Connection`
- **Status**: `active`

<!-- decisions:api:storage.Database.connect:end -->
````

When a symbol disappears, its section becomes a tombstone with a callout pointing at
the version that removed it (idempotent: one callout per version). When it comes back,
a "re-added in" line is appended to the same callout block. Disable tombstones with
`api.tombstones = false` if you prefer plain deletions.

## `decisions context`

```console
$ decisions context src/storage.py
$ decisions context storage.Database.connect      # qualified symbol also works
$ decisions context src/storage.py --version v1.0
$ decisions context src/storage.py --json
```

* **API section** – symbols whose `source_file` matches, from the newest confirmed
  baseline. On an `indev-vX` branch (or inside a draft) the draft wins, because the
  working tree no longer matches the baseline. `--version vX` pins one version;
  removed symbols are shown with `removed in vX`.
* **Decisions** – matched through `decision_files` (path or directory prefix),
  `decision_symbols` and `@decision:` back-references from docstrings, newest first,
  with tags, rejected alternatives and a summary.
* **Supersede chain** – for `status: superseded` decisions, who replaced them.
* **Notes** – stale references are reported, never silently dropped: a decision that
  points at a file that no longer exists, or a `@decision:` marker whose target cannot
  be resolved.

### Back-references from code

Code can point at the decision that explains it. Put a marker in a docstring or doc
comment (works for every backend):

```python
def connect(dsn: str) -> Connection:
    """Open a connection.

    @decision:storage/use-sqlite
    """
```

Accepted forms: `slug` (`use-sqlite`, matched against the date-prefixed slug),
`topic/slug` (`storage/use-sqlite`), or a full repo path
(`docs/decision/storage/2026-10-24-use-sqlite.md`). The marker is stripped from the
rendered doc and recorded per symbol:

```markdown
- **Decisions**: `storage/use-sqlite`
```

`context` resolves those references to real decisions (even when the file is not listed
in `decision_files`) and reports unresolvable ones as `stale reference`. This closes the
loop: the code carries its own provenance, and it survives refactors because the link
is reviewed together with the code.

Human output is plain text; `--json` returns the same data as a structured object
(agent-friendly). Exit code stays 0 for a missing path: it is a query, not a validator.

## Configuration

Two layers, both optional (every key has a default):

* **`decisions.toml`** at the repo root — committed, shared with the team.
* **`.decisions/config.toml`** — created by `init`, gitignored (the index database lives
  next to it), merged **on top of** the shared file for machine-local overrides.

```toml
[index]
path = ".decisions/index.db"       # keep it inside the gitignored state dir

[api]
source_roots = ["src"]             # where symbols are extracted from
exclude = ["tests", "test", "test_*.py", "*_test.py", "conftest.py", "docs"]
tombstones = true                  # keep anchor-preserving tombstones
languages = "auto"                 # or ["python"], ["python", "go"], [".ts"]

[api.topics]                       # optional explicit topic ownership
storage = ["src/storage.py", "src/storage/**"]
```

Nested tables are deep-merged, so a local file can change `api.languages` without
repeating `api.topics`. `languages = "auto"` enables every registered backend; restrict
it to keep a project's scan focused. Parse failures and unsupported files are warnings,
never a hard failure.

## Symbol extraction (multi-language)

Python is parsed exactly with `ast`: modules, classes, nested classes, functions,
methods, async functions and UPPER_CASE constants; names starting with `_` are private
and skipped.

Other languages use a line-oriented best-effort backend that tracks `{`/`}` scopes
(indentation for Ruby), attaches the doc comment above a declaration, and extracts
`@decision:` markers:

| language | suffixes | scope source |
| -------- | -------- | ------------ |
| Python | `.py`, `.pyi` | `ast` (exact) |
| JavaScript / TypeScript | `.js .jsx .mjs .cjs .ts .tsx .mts .cts` | braces |
| Go | `.go` | braces (+ receiver methods) |
| Rust | `.rs` | braces (incl. `impl` blocks) |
| Java | `.java` | braces |
| Kotlin | `.kt .kts` | braces |
| C# | `.cs` | braces |
| Ruby | `.rb` | indentation |
| PHP | `.php` | braces |

It is deliberately conservative: first-line signatures, `class`-shaped containers, and
ALL_CAPS constants. If you need exact parsing for another language, ship a plugin — the
pipeline does not change:

```toml
# pyproject.toml of a plugin package
[project.entry-points."decisions.extractors"]
mylang = "my_package.extractor:Backend"
```

The entry point resolves to a `decisions.api.langs.Backend` instance (or a zero-argument
factory returning one): `Backend(name, suffixes, extract)`, where `extract(text, *,
module, topic, doc_path, rel_path)` returns `ApiSymbol` objects. Plugins are discovered
lazily, and a broken plugin produces a warning instead of a crash.

## Repository layout

```
src/decisions/
  core/
    project.py      # root discovery, docs/api/head
    config.py       # .decisions/config.toml
    frontmatter.py  # minimal YAML subset reader/writer
    gitutils.py     # git plumbing wrappers
    models.py       # Decision / ApiSymbol
    db.py           # schema, full rebuild, queries
    schema.sql
    stdio.py        # UTF-8 console setup
  cli.py            # decisions entry point
  api/
    cli.py          # decisions-api entry point
    extractor.py    # file walking, topic mapping, warnings
    langs/          # backend registry: base.py + python_ast.py + generic.py (regex)
    render.py       # Markdown render + parse (anchors, callouts, refs)
    diff.py         # in-memory unified diff
    sync.py         # sync orchestration + consistency check
    indev.py        # start / finish / release / list
    lock.py         # cross-platform advisory file lock
```

## Development

```console
uv pip install -e .
uv run python -m unittest discover -s tests -t .   # end-to-end, ~15s
uv run ruff check src tests && uv run ruff format --check src tests
```

The end-to-end tests build throwaway git repositories and cover the real
commit/merge/patch boundaries, including conflict + `--resume`.

## Status and roadmap

v0.1–v0.3 of the design are implemented: decision pipeline, Python + multi-language
extraction, full `indev`/`release`/`check` lifecycle, file- and symbol-level `context`,
docstring back-references with stale-reference reporting.

Ideas that are intentionally **not** implemented (out of scope by design): MCP server,
web UI, cross-project search, vector search, team approval flows. A cross-project
`decisions` mode would require a different index layout and is not planned.
