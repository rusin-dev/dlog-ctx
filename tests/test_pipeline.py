"""End-to-end tests: decision log, API cycles, indev/release bookkeeping.

Every test builds a throwaway git repository, so these are slow-ish but they
cover the real commit/merge/patch boundaries.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from decisions import cli as decisions_cli
from decisions.api import cli as api_cli
from decisions.api import sync
from decisions.api.lock import FileLock
from decisions.core import gitutils, project

APP_SOURCE = '''"""Application entry point."""

VERSION = "1.0"


class App:
    """The app."""

    def run(self, argv: list[str]) -> int:
        """Run it."""
        return 0


def helper(value: int) -> int:
    """Help."""
    return value + 1
'''


@unittest.skipIf(shutil.which("git") is None, "git is required")
class RepoTestCase(unittest.TestCase):
    """Base class that gives every test an initialised throwaway repository."""

    def setUp(self) -> None:
        # `enterContext` registers the cleanup with unittest: the temporary
        # directory is removed even when an assertion fails.
        self._tmp = self.enterContext(
            tempfile.TemporaryDirectory()  # pylint: disable=consider-using-with
        )
        self.root = Path(self._tmp).resolve()
        self._env_before = os.environ.pop("DECISIONS_ROOT", None)
        self.git("init", "-q", "-b", "main", ".")
        self.git("config", "user.email", "test@example.com")
        self.git("config", "user.name", "Test")
        self.git("config", "commit.gpgsign", "false")
        (self.root / "src").mkdir()
        (self.root / "src" / "app.py").write_text(APP_SOURCE, encoding="utf-8")
        code, output = self.decisions("init")
        self.assertEqual(code, 0, output)
        self.assertTrue((self.root / "docs" / "decision").is_dir())
        self.assertTrue((self.root / ".decisions" / "config.toml").exists())
        self.assertTrue((self.root / ".decisions").is_dir())
        self.assertIn(".decisions/", (self.root / ".gitignore").read_text(encoding="utf-8"))
        self.commit("init")

    def tearDown(self) -> None:
        os.environ.pop("DECISIONS_ROOT", None)
        if self._env_before is not None:
            os.environ["DECISIONS_ROOT"] = self._env_before

    # -- helpers --------------------------------------------------------
    def git(self, *args: str) -> str:
        """Run a git command in the test repository and return its stdout."""
        return gitutils.out(self.root, *args)

    def commit(self, message: str = "commit") -> None:
        """Stage everything and commit it."""
        gitutils.add(self.root, ".")
        gitutils.commit(self.root, message)

    def decisions(self, *args: str) -> tuple[int, str]:
        """Run ``decisions <args>`` against the test repository."""
        return self._run(decisions_cli.main, args)

    def api(self, *args: str) -> tuple[int, str]:
        """Run ``decisions-api <args>`` against the test repository."""
        return self._run(api_cli.main, args)

    def _run(self, entry, args: tuple[str, ...]) -> tuple[int, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = entry(["-C", str(self.root), *args])
        return code, stdout.getvalue() + stderr.getvalue()

    def bootstrap_baseline(self, version: str = "v1.0") -> None:
        """Declare a version, run one indev cycle and confirm its baseline."""
        code, output = self.api("head", "add", version)
        self.assertEqual(code, 0, output)
        project.version_dir(self.root, version).mkdir(parents=True, exist_ok=True)
        self.commit(f"bootstrap {version}")
        code, output = self.api("indev", "start", version)
        self.assertEqual(code, 0, output)
        code, output = self.api("sync")
        self.assertEqual(code, 0, output)
        code, output = self.api("indev", "finish", version)
        self.assertEqual(code, 0, output)


class DecisionFlowTest(RepoTestCase):
    """``add`` / ``recall`` / ``timeline`` / ``context`` end to end."""

    def test_add_recall_timeline_context(self) -> None:
        """A recorded decision is reachable through every read command."""
        target = str(self.root / "src" / "app.py")
        code, output = self.decisions(
            "add",
            "--topic",
            "storage",
            "-r",
            "Chose SQLite over Postgres because deployment cost dominates.",
            "--title",
            "SQLite over Postgres",
            "--tag",
            "storage",
            "--tag",
            "database",
            "--file",
            target,
            "--symbol",
            "app.App.run",
            "--rejected",
            "Postgres (deployment cost)",
            "--json",
        )
        self.assertEqual(code, 0, output)
        payload = json.loads(output)
        self.assertEqual(payload["topic"], "storage")
        self.assertEqual(payload["files"], ["src/app.py"])
        path = self.root / payload["path"]
        self.assertTrue(path.exists())
        text = path.read_text(encoding="utf-8")
        self.assertIn("title: SQLite over Postgres", text)
        self.assertIn("commit:", text)
        self.assertIn("branch: main", text)

        code, output = self.decisions("recall", "Postgres")
        self.assertEqual(code, 0, output)
        self.assertIn("SQLite over Postgres", output)

        code, output = self.decisions("recall", "--tag", "database", "--json")
        self.assertEqual(code, 0, output)
        self.assertEqual(len(json.loads(output)), 1)

        code, output = self.decisions("recall", "--file", "src/app.py")
        self.assertEqual(code, 0, output)
        self.assertIn("SQLite over Postgres", output)

        code, output = self.decisions("recall", "--since", "1y")
        self.assertEqual(code, 0, output)
        self.assertIn("SQLite over Postgres", output)

        code, output = self.decisions("recall", "--since", "not-a-date")
        self.assertEqual(code, 2)
        self.assertIn("invalid --since", output)

        code, output = self.decisions("timeline", "--json")
        self.assertEqual(code, 0, output)
        self.assertEqual(json.loads(output)[0]["title"], "SQLite over Postgres")

        code, output = self.decisions("context", "src/app.py", "--json")
        self.assertEqual(code, 0, output)
        context = json.loads(output)
        self.assertEqual(context["path"], "src/app.py")
        self.assertEqual(len(context["decisions"]), 1)
        self.assertEqual(context["decisions"][0]["rejected"], ["Postgres (deployment cost)"])

    def test_topics_and_dedup_slugs(self) -> None:
        """Two decisions with the same title get distinct file paths."""
        for _ in range(2):
            code, output = self.decisions(
                "add", "--topic", "ui", "-r", "Same title", "--title", "Same title"
            )
            self.assertEqual(code, 0, output)
        files = sorted(p.name for p in (self.root / "docs" / "decision" / "ui").glob("*.md"))
        self.assertEqual(len(files), 2)
        self.assertNotEqual(files[0], files[1])

        code, output = self.decisions("recall", "--topics", "--json")
        self.assertEqual(code, 0, output)
        self.assertEqual(json.loads(output)[0]["topic"], "ui")

    def test_supersede_chain_in_context(self) -> None:
        """``supersedes`` links the replacement to the old decision."""
        code, output = self.decisions(
            "add",
            "--topic",
            "storage",
            "-r",
            "Postgres was fine.",
            "--title",
            "Use Postgres",
            "--status",
            "superseded",
        )
        self.assertEqual(code, 0, output)
        old_path = output.split("recorded ", 1)[1].split("\n", 1)[0]

        code, output = self.decisions(
            "add",
            "--topic",
            "storage",
            "-r",
            "SQLite is cheaper.",
            "--title",
            "Use SQLite",
            "--supersedes",
            old_path,
            "--file",
            "src/app.py",
        )
        self.assertEqual(code, 0, output)

        # The old decision must reference the file too for context to list it.
        old_file = self.root / old_path
        old_file.write_text(
            old_file.read_text(encoding="utf-8").replace(
                "status: superseded", "status: superseded\nfiles:\n  - src/app.py"
            ),
            encoding="utf-8",
        )
        code, output = self.decisions("context", "src/app.py", "--json")
        self.assertEqual(code, 0, output)
        context = json.loads(output)
        chain = context["supersede"]
        self.assertTrue(chain)
        self.assertEqual(chain[0]["superseded_by"][0], context["decisions"][0]["path"])

    def test_missing_file_is_reported(self) -> None:
        """``context`` reports a note for a path that exists nowhere."""
        code, output = self.decisions("context", "src/missing.py", "--json")
        self.assertEqual(code, 0, output)
        payload = json.loads(output)
        self.assertFalse(payload["exists"])
        self.assertTrue(any("does not exist" in note for note in payload["notes"]))


class ApiCycleTest(RepoTestCase):
    """``sync`` / ``indev`` / ``release`` lifecycle on a real repository."""

    def test_sync_requires_indev_branch(self) -> None:
        """Draft patches may only be generated on ``indev-<version>``."""
        code, output = self.api("head", "add", "v1.0")
        self.assertEqual(code, 0, output)
        project.version_dir(self.root, "v1.0").mkdir(parents=True, exist_ok=True)
        self.commit()
        code, output = self.api("sync")
        self.assertEqual(code, 2)
        self.assertIn("indev-v1.0", output)

    def test_full_cycle_generates_docs(self) -> None:
        """A full indev cycle leaves a clean tree and a rendered baseline."""
        self.bootstrap_baseline("v1.0")
        doc = self.root / "docs" / "api" / "v1.0" / "app" / "index.md"
        self.assertTrue(doc.exists())
        text = doc.read_text(encoding="utf-8")
        self.assertIn("## app.App.run", text)
        self.assertIn("def run(argv: list[str]) -> int", text)
        self.assertIn("- **Defined in**: `src/app.py:", text)
        # Baseline confirmed: only the baseline exists, no draft leftovers.
        self.assertFalse(project.draft_dir(self.root, "v1.0").exists())
        self.assertFalse((self.root / "AGENTS.md").exists())
        self.assertEqual(gitutils.status_porcelain(self.root), [])
        self.assertEqual(gitutils.current_branch(self.root), "main")

        code, output = self.api("check")
        self.assertEqual(code, 0, output)
        self.assertIn("v1.0 baseline confirmed", output)

    def test_patch_never_touches_baseline_before_finish(self) -> None:
        """``sync`` only writes the draft patch, never the confirmed baseline."""
        self.bootstrap_baseline("v1.0")
        code, output = self.api("head", "add", "v1.1")
        self.assertEqual(code, 0, output)
        shutil.copytree(self.root / "docs" / "api" / "v1.0", self.root / "docs" / "api" / "v1.1")
        self.commit("baseline v1.1")
        code, output = self.api("indev", "start", "v1.1")
        self.assertEqual(code, 0, output)
        agents = (self.root / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("<!-- decisions:indev -->", agents)
        self.assertIn("> Currently developing the content of `indev-v1.1`.", agents)
        self.assertIn("<!-- /decisions:indev -->", agents)

        source = self.root / "src" / "app.py"
        source.write_text(
            source.read_text(encoding="utf-8") + "\n\ndef extra() -> None:\n    pass\n",
            encoding="utf-8",
        )
        code, output = self.api("sync")
        self.assertEqual(code, 0, output)
        self.assertIn("added (1)", output)
        patch = sync.patch_path(self.root, "v1.1")
        self.assertTrue(patch.exists())

        code, output = self.api("sync", "--json")
        self.assertEqual(code, 0, output)
        payload = json.loads(output)
        self.assertEqual(payload["patch"], "docs/api/~v1.1/changes.patch")
        self.assertEqual(payload["added"], ["app.extra"])
        self.assertFalse(
            (self.root / "docs" / "api" / "v1.1" / "app" / "index.md")
            .read_text(encoding="utf-8")
            .count("app.extra")
        )
        # The confirmed baseline directory is untouched.
        baseline_dirty = [
            line for line in gitutils.dirty_paths(self.root) if "docs/api/v1.1" in line
        ]
        self.assertEqual(baseline_dirty, [])

        # A second identical sync is a no-op (patch content unchanged).
        first = patch.read_text(encoding="utf-8")
        code, _ = self.api("sync")
        self.assertEqual(code, 0)
        self.assertEqual(patch.read_text(encoding="utf-8"), first)

        code, output = self.api("diff", "v1.1")
        self.assertEqual(code, 0, output)
        self.assertIn("+def extra() -> None", output)

    def test_removal_creates_tombstone_and_context_reports_it(self) -> None:
        """A deleted symbol keeps its anchor and is reported as removed."""
        self.bootstrap_baseline("v1.0")
        code, output = self.api("head", "add", "v1.1")
        self.assertEqual(code, 0, output)
        shutil.copytree(self.root / "docs" / "api" / "v1.0", self.root / "docs" / "api" / "v1.1")
        self.commit("baseline v1.1")
        self.assertEqual(self.api("indev", "start", "v1.1")[0], 0)

        source = self.root / "src" / "app.py"
        source.write_text(
            source.read_text(encoding="utf-8").replace(
                '''

def helper(value: int) -> int:
    """Help."""
    return value + 1
''',
                "",
            ),
            encoding="utf-8",
        )
        code, output = self.api("sync")
        self.assertEqual(code, 0, output)
        self.assertIn("removed (1)", output)

        code, output = self.decisions("context", "src/app.py")
        self.assertEqual(code, 0, output)
        self.assertIn("removed in v1.1", output)

        self.commit("drop helper")
        code, output = self.api("indev", "finish", "v1.1")
        self.assertEqual(code, 0, output)
        text = (self.root / "docs" / "api" / "v1.1" / "app" / "index.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("> [!IMPORTANT]", text)
        self.assertIn("app.helper", text)
        self.assertIn("- **Status**: `removed in v1.1`", text)

        code, output = self.decisions("context", "src/app.py", "--json")
        self.assertEqual(code, 0, output)
        symbols = {item["qualified_name"]: item for item in json.loads(output)["api"]["symbols"]}
        self.assertEqual(symbols["app.helper"]["status"], "removed")
        self.assertEqual(symbols["app.helper"]["removed_in"], "v1.1")

    def test_release_tags_and_rejects_pending_patch(self) -> None:
        """``release`` tags once and refuses to tag over an unconfirmed patch."""
        self.bootstrap_baseline("v1.0")
        code, _ = self.api("head", "add", "v1.1")
        self.assertEqual(code, 0)
        shutil.copytree(self.root / "docs" / "api" / "v1.0", self.root / "docs" / "api" / "v1.1")
        self.commit("baseline v1.1")
        self.assertEqual(self.api("indev", "start", "v1.1")[0], 0)

        code, output = self.api("release", "v1.1.0")
        self.assertEqual(code, 0, output)
        self.assertTrue(gitutils.tag_exists(self.root, "v1.1.0"))

        code, output = self.api("release", "v1.1.0")
        self.assertEqual(code, 2)
        self.assertIn("already exists", output)

        source = self.root / "src" / "app.py"
        source.write_text(
            source.read_text(encoding="utf-8") + "\n\ndef another() -> None:\n    pass\n",
            encoding="utf-8",
        )
        self.commit("add another")
        self.assertEqual(self.api("sync")[0], 0)
        code, output = self.api("release", "v1.1.1")
        self.assertEqual(code, 2)
        self.assertIn("unconfirmed patch", output)

        code, output = self.api("release", "not-a-tag")
        self.assertEqual(code, 2)
        self.assertIn("vX.Y.Z", output)

    def test_sync_skips_silently_when_lock_is_held(self) -> None:
        """A concurrent holder of ``docs/api/.lock`` makes sync a no-op."""
        self.bootstrap_baseline("v1.0")
        self.assertEqual(self.api("head", "add", "v1.1")[0], 0)
        shutil.copytree(self.root / "docs" / "api" / "v1.0", self.root / "docs" / "api" / "v1.1")
        self.commit("baseline v1.1")
        self.assertEqual(self.api("indev", "start", "v1.1")[0], 0)

        lock = FileLock(project.lock_path(self.root))
        self.assertTrue(lock.acquire())
        try:
            code, output = self.api("sync")
            self.assertEqual(code, 0, output)
            self.assertIn("skipped", output)
            self.assertFalse(sync.patch_path(self.root, "v1.1").exists())
        finally:
            lock.release()

    def test_indev_list_and_check(self) -> None:
        """``indev list`` reports version state; ``check`` flags orphan drafts."""
        self.bootstrap_baseline("v1.0")
        code, output = self.api("indev", "list", "--json")
        self.assertEqual(code, 0, output)
        item = json.loads(output)[0]
        self.assertEqual(item["version"], "v1.0")
        self.assertTrue(item["branch"])
        self.assertTrue(item["baseline"])
        self.assertFalse(item["draft"])
        self.assertEqual(item["patch"], "none")

        project.draft_dir(self.root, "v9.9").mkdir(parents=True)
        code, output = self.api("check")
        self.assertEqual(code, 1)
        self.assertIn("orphan", output)

    def test_indev_start_validations(self) -> None:
        """``indev start`` refuses missing baselines, dirty trees and duplicates."""
        code, _ = self.api("head", "add", "v2.0")
        self.assertEqual(code, 0)
        code, output = self.api("indev", "start", "v2.0")
        self.assertEqual(code, 2)
        self.assertIn("baseline directory", output)

        project.version_dir(self.root, "v2.0").mkdir(parents=True, exist_ok=True)
        self.commit("v2.0 dir")
        (self.root / "dirty.txt").write_text("x", encoding="utf-8")
        code, output = self.api("indev", "start", "v2.0")
        self.assertEqual(code, 2)
        self.assertIn("not clean", output)

        (self.root / "dirty.txt").unlink()
        self.assertEqual(self.api("indev", "start", "v2.0")[0], 0)
        code, output = self.api("indev", "start", "v2.0")
        self.assertEqual(code, 2)
        self.assertIn("already exists", output)

    def test_finish_resume_after_conflict_is_possible(self) -> None:
        """A conflicted merge is left in place and finished with ``--resume``."""
        self.bootstrap_baseline("v1.0")
        self.assertEqual(self.api("head", "add", "v1.1")[0], 0)
        shutil.copytree(self.root / "docs" / "api" / "v1.0", self.root / "docs" / "api" / "v1.1")
        self.commit("baseline v1.1")
        self.assertEqual(self.api("indev", "start", "v1.1")[0], 0)

        # Conflicting change on main for the same file.
        project.write_head(self.root, ["v1.0", "v1.1"])
        gitutils.checkout(self.root, "main")
        (self.root / "src" / "app.py").write_text("# main version\n", encoding="utf-8")
        self.commit("main change")
        gitutils.checkout(self.root, "indev-v1.1")
        (self.root / "src" / "app.py").write_text("# branch version\n", encoding="utf-8")
        self.commit("branch change")

        code, output = self.api("indev", "finish", "v1.1")
        self.assertEqual(code, 3)
        self.assertTrue(gitutils.merge_in_progress(self.root))

        # Resolve manually then resume.
        (self.root / "src" / "app.py").write_text("# resolved\n", encoding="utf-8")
        gitutils.git(self.root, "add", "src/app.py")
        code, output = self.api("indev", "finish", "v1.1", "--resume")
        self.assertEqual(code, 0, output)
        self.assertEqual(gitutils.current_branch(self.root), "main")
        self.assertEqual((self.root / "src" / "app.py").read_text(encoding="utf-8"), "# resolved\n")

    def test_no_merge_stops_before_merge(self) -> None:
        """``--no-merge`` stops on the indev branch after committing."""
        self.bootstrap_baseline("v1.0")
        self.assertEqual(self.api("head", "add", "v1.1")[0], 0)
        shutil.copytree(self.root / "docs" / "api" / "v1.0", self.root / "docs" / "api" / "v1.1")
        self.commit("baseline v1.1")
        self.assertEqual(self.api("indev", "start", "v1.1")[0], 0)
        code, output = self.api("indev", "finish", "v1.1", "--no-merge")
        self.assertEqual(code, 0, output)
        self.assertEqual(gitutils.current_branch(self.root), "indev-v1.1")
        self.assertIn("merge skipped", output)

    def test_close_head_removes_version_line(self) -> None:
        """``--close-head`` drops the finished version from ``docs/api/head``."""
        self.bootstrap_baseline("v1.0")
        self.assertEqual(project.read_head(self.root), ["v1.0"])
        self.assertEqual(self.api("head", "add", "v1.1")[0], 0)
        shutil.copytree(self.root / "docs" / "api" / "v1.0", self.root / "docs" / "api" / "v1.1")
        self.commit("baseline v1.1")
        self.assertEqual(self.api("indev", "start", "v1.1")[0], 0)
        code, output = self.api("indev", "finish", "v1.1", "--close-head")
        self.assertEqual(code, 0, output)
        self.assertEqual(project.read_head(self.root), ["v1.0"])

    def test_head_commands(self) -> None:
        """``head add/list/remove`` are idempotent and validate versions."""
        code, output = self.api("head", "add", "v1.0")
        self.assertEqual(code, 0, output)
        code, output = self.api("head", "add", "v1.0")
        self.assertEqual(code, 0, output)
        self.assertIn("already in head", output)
        code, output = self.api("head", "list")
        self.assertIn("v1.0 (default)", output)
        code, output = self.api("head", "remove", "v1.0")
        self.assertIn("removed", output)
        self.assertEqual(project.read_head(self.root), [])
        code, output = self.api("head", "add", "nope")
        self.assertEqual(code, 2)
        self.assertIn("not a valid version", output)

    def test_head_add_create_bootstraps_baseline(self) -> None:
        """``head add --create`` creates the baseline directory as well."""
        code, output = self.api("head", "add", "v2.0", "--create")
        self.assertEqual(code, 0, output)
        self.assertTrue(project.version_dir(self.root, "v2.0").is_dir())
        self.assertIn("created docs/api/v2.0/", output)
        self.assertEqual(project.read_head(self.root), ["v2.0"])
        self.commit("bootstrap v2.0")
        self.assertEqual(self.api("indev", "start", "v2.0")[0], 0)

    def test_sync_extracts_non_python_sources(self) -> None:
        """Non-Python sources are documented by the regex backends."""
        web = self.root / "src" / "web"
        web.mkdir()
        (web / "app.ts").write_text(
            "export class Server {\n"
            "  /** Start listening. */\n"
            "  public start(port: number): void {\n"
            "  }\n"
            "}\n",
            encoding="utf-8",
        )
        self.commit("typescript source")
        self.bootstrap_baseline("v1.0")
        doc = self.root / "docs" / "api" / "v1.0" / "web" / "index.md"
        self.assertTrue(doc.exists())
        text = doc.read_text(encoding="utf-8")
        self.assertIn("## web.app.Server.start", text)
        self.assertIn("public start(port: number): void", text)

    def test_docstring_backreferences_resolved_in_context(self) -> None:
        """``@decision:`` markers resolve to decisions and stale ones are noted."""
        source = APP_SOURCE.replace(
            '"""Run it."""', '"""Run it.\n\n@decision:storage/use-sqlite\n"""'
        ).replace('"""Help."""', '"""Help.\n\n@decision:missing/ghost\n"""')
        (self.root / "src" / "app.py").write_text(source, encoding="utf-8")
        self.commit("back-references")
        self.bootstrap_baseline("v1.0")

        code, output = self.decisions(
            "add", "--topic", "storage", "-r", "Use SQLite.", "--title", "Use SQLite"
        )
        self.assertEqual(code, 0, output)

        code, output = self.decisions("context", "src/app.py", "--json")
        self.assertEqual(code, 0, output)
        payload = json.loads(output)
        symbols = {item["qualified_name"]: item for item in payload["api"]["symbols"]}
        refs = symbols["app.App.run"]["decision_refs"]
        self.assertEqual([ref["ref"] for ref in refs], ["storage/use-sqlite"])
        self.assertFalse(refs[0]["stale"])
        self.assertTrue(any(decision["title"] == "Use SQLite" for decision in payload["decisions"]))
        self.assertTrue(
            any(
                "stale reference" in note and "@decision:missing/ghost" in note
                for note in payload["notes"]
            )
        )
        ghost = symbols["app.helper"]["decision_refs"]
        self.assertTrue(ghost[0]["stale"])

        # Rendered docs must not leak the raw marker syntax.
        doc = (self.root / "docs" / "api" / "v1.0" / "app" / "index.md").read_text(encoding="utf-8")
        self.assertNotIn("@decision:", doc)
        self.assertIn("- **Decisions**: `storage/use-sqlite`", doc)


if __name__ == "__main__":
    unittest.main()
