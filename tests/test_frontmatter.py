"""Unit tests for the minimal frontmatter reader/writer."""

from __future__ import annotations

import datetime as dt
import unittest

from decisions.core import frontmatter


class ParseTest(unittest.TestCase):
    def test_no_frontmatter(self) -> None:
        meta, body = frontmatter.parse("just text\n")
        self.assertEqual(meta, {})
        self.assertEqual(body, "just text\n")

    def test_basic_fields_and_comments(self) -> None:
        text = (
            "---\n"
            "title: SQLite instead of Postgres\n"
            "date: 2026-10-24\n"
            "status: accepted          # accepted / superseded / deprecated\n"
            "files:\n"
            "  - src/storage.py\n"
            '  - "src/a b.py"\n'
            "tags: [storage, database]  # inline list\n"
            "rejected:\n"
            "  - Postgres (deployment cost)\n"
            "---\n"
            "\n"
            "Because.\n"
        )
        meta, body = frontmatter.parse(text)
        self.assertEqual(meta["title"], "SQLite instead of Postgres")
        self.assertEqual(meta["status"], "accepted")
        self.assertEqual(meta["files"], ["src/storage.py", "src/a b.py"])
        self.assertEqual(meta["tags"], ["storage", "database"])
        self.assertEqual(meta["rejected"], ["Postgres (deployment cost)"])
        self.assertEqual(body, "Because.")

    def test_empty_list_placeholder_becomes_string(self) -> None:
        meta, _ = frontmatter.parse("---\ntags:\n---\n\nx\n")
        self.assertEqual(meta["tags"], "")

    def test_invalid_line_raises(self) -> None:
        with self.assertRaises(frontmatter.FrontmatterError):
            frontmatter.parse("---\nnonsense line\n---\n\nbody\n")

    def test_unterminated_frontmatter_is_body(self) -> None:
        meta, body = frontmatter.parse("---\ntitle: x\n")
        self.assertEqual(meta, {})
        self.assertIn("title: x", body)


class DumpTest(unittest.TestCase):
    def test_roundtrip(self) -> None:
        meta = {
            "title": "Storage: SQLite #1",
            "date": "2026-10-24",
            "status": "accepted",
            "commit": "a1b2c3d",
            "branch": "main",
            "version": "v1.3",
            "files": ["src/storage.py", "src/a b.py"],
            "tags": ["storage", "database"],
            "rejected": ["Postgres (deployment cost)"],
            "symbols": ["storage.Database.connect"],
            "supersedes": "docs/decision/storage/2026-09-01-use-postgres.md",
        }
        body_in = "Reason line one.\n\nReason line two."
        text = frontmatter.dump(meta, body_in)
        parsed, body = frontmatter.parse(text)
        self.assertEqual(parsed, meta)
        self.assertEqual(body.strip(), body_in)

    def test_empty_values_are_skipped(self) -> None:
        text = frontmatter.dump({"title": "t", "commit": "", "tags": []}, "body")
        self.assertNotIn("commit:", text)
        self.assertNotIn("tags:", text)

    def test_date_object(self) -> None:
        text = frontmatter.dump({"title": "t", "date": dt.date(2026, 1, 2)}, "b")
        self.assertIn("date: 2026-01-02", text)


if __name__ == "__main__":
    unittest.main()
