"""Tests for rendering, round-tripping and diffing API documents."""

from __future__ import annotations

import unittest

from decisions.api import diff, render
from decisions.core.models import ApiSymbol, version_key


def symbol(name: str, signature: str, *, status: str = "active", **kwargs) -> ApiSymbol:
    """Build an :class:`ApiSymbol` with test-friendly defaults."""
    module = name.split(".")[0]
    return ApiSymbol(
        qualified_name=name,
        kind=kwargs.pop("kind", "function"),
        module=module,
        topic=kwargs.pop("topic", module),
        doc_path=kwargs.pop("doc_path", render.doc_path("v1.0", module)),
        signature=signature,
        returns=kwargs.pop("returns", "None"),
        doc=kwargs.pop("doc", f"Doc for {name}."),
        source_file=kwargs.pop("source_file", "src/app.py"),
        lineno=kwargs.pop("lineno", 1),
        status=status,
        **kwargs,
    )


class RenderTargetTest(unittest.TestCase):
    """Rendering symbol sections and computing the target document tree."""

    def test_creates_document(self) -> None:
        """A first render creates one document with markers and metadata."""
        symbols = [symbol("app.main", "def main() -> None")]
        target = render.render_target({}, symbols, "v1.0")

        path = "docs/api/v1.0/app/index.md"
        self.assertIn(path, target)
        text = target[path] or ""
        self.assertIn("<!-- decisions:api:app.main:start -->", text)
        self.assertIn("## app.main", text)
        self.assertIn("- **Status**: `active`", text)
        parsed = render.parse_document(text)
        self.assertEqual(list(parsed.sections), ["app.main"])
        self.assertEqual(parsed.sections["app.main"].signature, "def main() -> None")

    def test_idempotent(self) -> None:
        """Rendering an unchanged symbol set produces byte-identical docs."""
        symbols = [symbol("app.main", "def main() -> None")]
        first = render.render_target({}, symbols, "v1.0")
        second = render.render_target(
            {path: text for path, text in first.items() if text}, symbols, "v1.0"
        )
        self.assertEqual(first, second)

    def test_changed_signature_replaces_section(self) -> None:
        """A changed signature replaces the old section instead of appending."""
        base = render.render_target({}, [symbol("app.main", "def main() -> None")], "v1.0")
        target = render.render_target(
            {path: text for path, text in base.items() if text},
            [symbol("app.main", "def main(argv: list[str]) -> int", returns="int")],
            "v1.0",
        )
        text = target["docs/api/v1.0/app/index.md"] or ""
        self.assertIn("def main(argv: list[str]) -> int", text)
        self.assertNotIn("def main() -> None", text)

    def test_removed_symbol_becomes_tombstone_with_callout(self) -> None:
        """A removed symbol keeps its section, with a removal callout."""
        base = render.render_target({}, [symbol("app.main", "def main() -> None")], "v1.0")
        base_docs = {path: text for path, text in base.items() if text}
        target = render.render_target(base_docs, [], "v1.1")
        text = target["docs/api/v1.0/app/index.md"] or ""
        self.assertIn("> [!IMPORTANT]", text)
        self.assertIn("removed in the next version [v1.1]", text)
        self.assertIn("- **Status**: `removed in v1.1`", text)
        self.assertIn("<!-- decisions:api:app.main:end -->", text)

    def test_tombstone_is_idempotent(self) -> None:
        """Re-rendering a tombstone does not duplicate the callout."""
        base = render.render_target({}, [symbol("app.main", "def main() -> None")], "v1.0")
        base_docs = {path: text for path, text in base.items() if text}
        first = render.render_target(base_docs, [], "v1.1")
        second = render.render_target(
            {path: text for path, text in first.items() if text}, [], "v1.1"
        )
        self.assertEqual(first, second)
        text = second["docs/api/v1.0/app/index.md"] or ""
        self.assertEqual(text.count("removed in the next version"), 1)

    def test_readded_symbol_keeps_callout_and_appends_line(self) -> None:
        """A re-added symbol keeps the tombstone callout and gains a re-add line."""
        base = render.render_target({}, [symbol("app.main", "def main() -> None")], "v1.0")
        removed = render.render_target(
            {path: text for path, text in base.items() if text}, [], "v1.1"
        )
        readded = render.render_target(
            {path: text for path, text in removed.items() if text},
            [symbol("app.main", "def main() -> None")],
            "v1.2",
        )
        text = readded["docs/api/v1.0/app/index.md"] or ""
        self.assertIn("was re-added in [v1.2]", text)
        self.assertIn("- **Status**: `active`", text)

    def test_tombstones_disabled_deletes_file(self) -> None:
        """With ``tombstones=False`` an emptied topic document is deleted."""
        base = render.render_target({}, [symbol("app.main", "def main() -> None")], "v1.0")
        target = render.render_target(
            {path: text for path, text in base.items() if text},
            [],
            "v1.1",
            tombstones=False,
        )
        self.assertIsNone(target["docs/api/v1.0/app/index.md"])

    def test_decision_refs_are_rendered_and_parsed(self) -> None:
        """Decision back-references survive a render/parse round trip."""
        symbol_entry = symbol("app.main", "def main() -> None")
        symbol_entry.decision_refs = ["storage/use-sqlite", "cli-piped-input"]
        target = render.render_target({}, [symbol_entry], "v1.0")
        text = target["docs/api/v1.0/app/index.md"] or ""
        self.assertIn("- **Decisions**: `storage/use-sqlite`, `cli-piped-input`", text)
        parsed = render.parse_document(text)
        self.assertEqual(
            parsed.sections["app.main"].decision_refs,
            ["storage/use-sqlite", "cli-piped-input"],
        )
        rebuilt = render.symbols_from_docs({path: body for path, body in target.items() if body})
        self.assertEqual(rebuilt[0].decision_refs, ["storage/use-sqlite", "cli-piped-input"])

    def test_topics_are_separate_documents(self) -> None:
        """Each topic gets its own ``index.md`` document."""
        symbols = [
            symbol("app.main", "def main() -> None"),
            symbol("storage.open_db", "def open_db() -> None"),
        ]
        target = render.render_target({}, symbols, "v1.0")
        self.assertEqual(
            sorted(path for path, text in target.items() if text),
            ["docs/api/v1.0/app/index.md", "docs/api/v1.0/storage/index.md"],
        )


class DiffTest(unittest.TestCase):
    """``git apply`` compatible unified diffs between two document trees."""

    def test_added_file(self) -> None:
        """A new path produces a ``new file`` diff against ``/dev/null``."""
        patch = diff.diff_tree({}, {"docs/api/v1.0/app/index.md": "# app\n"})
        self.assertIn("new file mode 100644", patch)
        self.assertIn("--- /dev/null", patch)
        self.assertIn("+++ b/docs/api/v1.0/app/index.md", patch)

    def test_deleted_file(self) -> None:
        """A vanished path produces a ``deleted file`` diff."""
        patch = diff.diff_tree({"a.md": "x\n"}, {"a.md": None})
        self.assertIn("deleted file mode 100644", patch)

    def test_unchanged_has_no_patch(self) -> None:
        """Identical trees yield an empty patch."""
        self.assertEqual(diff.diff_tree({"a.md": "x\n"}, {"a.md": "x\n"}), "")

    def test_modified_file_hunks(self) -> None:
        """A changed line is reported as a normal hunk."""
        patch = diff.diff_tree({"a.md": "a\nb\nc\n"}, {"a.md": "a\nB\nc\n"})
        self.assertIn("@@ -1,3 +1,3 @@", patch)
        self.assertIn("-b", patch)
        self.assertIn("+B", patch)


class VersionKeyTest(unittest.TestCase):
    """Numeric ordering of ``vX.Y[.Z]`` version strings."""

    def test_numeric_ordering(self) -> None:
        """``v1.10`` sorts after ``v1.9`` and drafts sort like releases."""
        self.assertGreater(version_key("v1.10"), version_key("v1.9"))
        self.assertGreater(version_key("v2.0"), version_key("v1.99"))
        self.assertEqual(version_key("~v1.3"), version_key("v1.3"))


if __name__ == "__main__":
    unittest.main()
