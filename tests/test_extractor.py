"""Tests for the Python ``ast`` extractor."""

from __future__ import annotations

import tempfile
import textwrap
import unittest
from pathlib import Path

from decisions.api import extractor
from decisions.core.config import Config

SOURCE = '''
"""Storage module."""

DEFAULT_DSN = "sqlite://local"
_private = object()


class Database(Base):
    """A wrapper."""

    def connect(self, dsn: str, timeout: int = 5) -> "Database":
        """Open a connection."""
        return self

    def _internal(self) -> None:
        pass

    class Cursor:
        def fetch(self, size: int = 1, *args, **kwargs) -> list:
            """Fetch rows."""
            return []


async def open_db(path: str, /, *, retries: int = 3) -> Database:
    """Open the default database."""
    return Database()


def _hidden() -> None:
    pass
'''


class ExtractorTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "src").mkdir()
        (self.root / "src" / "storage.py").write_text(textwrap.dedent(SOURCE), encoding="utf-8")
        self.config = Config(self.root, {"api": {"source_roots": ["src"]}})

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def extract(self) -> dict[str, object]:
        symbols, warnings = extractor.extract_symbols(self.root, self.config, "v1.0")
        self.assertEqual(warnings, [])
        return {symbol.qualified_name: symbol for symbol in symbols}

    def test_public_symbols(self) -> None:
        symbols = self.extract()
        self.assertEqual(
            sorted(symbols),
            [
                "storage",
                "storage.DEFAULT_DSN",
                "storage.Database",
                "storage.Database.Cursor",
                "storage.Database.Cursor.fetch",
                "storage.Database.connect",
                "storage.open_db",
            ],
        )

    def test_private_symbols_skipped(self) -> None:
        symbols = self.extract()
        self.assertNotIn("storage._private", symbols)
        self.assertNotIn("storage.Database._internal", symbols)
        self.assertNotIn("storage._hidden", symbols)

    def test_method_signature_drops_self(self) -> None:
        symbols = self.extract()
        self.assertEqual(
            symbols["storage.Database.connect"].signature,
            "def connect(dsn: str, timeout: int = 5) -> 'Database'",
        )
        self.assertEqual(symbols["storage.Database.connect"].kind, "method")
        self.assertEqual(symbols["storage.Database.connect"].returns, "'Database'")

    def test_keyword_only_and_positional_only_markers(self) -> None:
        symbols = self.extract()
        self.assertEqual(
            symbols["storage.open_db"].signature,
            "async def open_db(path: str, /, *, retries: int = 3) -> Database",
        )

    def test_varargs(self) -> None:
        symbols = self.extract()
        self.assertEqual(
            symbols["storage.Database.Cursor.fetch"].signature,
            "def fetch(size: int = 1, *args, **kwargs) -> list",
        )

    def test_class_signature_and_doc(self) -> None:
        symbols = self.extract()
        self.assertEqual(symbols["storage.Database"].signature, "class Database(Base)")
        self.assertEqual(symbols["storage.Database"].doc, "A wrapper.")

    def test_constant(self) -> None:
        symbols = self.extract()
        self.assertEqual(symbols["storage.DEFAULT_DSN"].kind, "constant")
        self.assertEqual(symbols["storage.DEFAULT_DSN"].signature, "DEFAULT_DSN")

    def test_topic_mapping_defaults_to_first_module_part(self) -> None:
        symbols = self.extract()
        self.assertEqual(symbols["storage.Database"].topic, "storage")
        self.assertEqual(symbols["storage.Database"].doc_path, "docs/api/v1.0/storage/index.md")

    def test_explicit_topic_mapping(self) -> None:
        config = Config(
            self.root,
            {"api": {"source_roots": ["src"], "topics": {"persistence": ["src/storage.py"]}}},
        )
        symbols, _ = extractor.extract_symbols(self.root, config, "v1.0")
        self.assertTrue(all(symbol.topic == "persistence" for symbol in symbols))

    def test_nested_package_module_name(self) -> None:
        package = self.root / "src" / "pkg"
        package.mkdir()
        (package / "__init__.py").write_text('"""Package."""\n', encoding="utf-8")
        (package / "core.py").write_text("def run() -> None:\n    pass\n", encoding="utf-8")
        symbols, _ = extractor.extract_symbols(self.root, self.config, "v1.0")
        names = {symbol.qualified_name for symbol in symbols}
        self.assertIn("pkg", names)
        self.assertIn("pkg.core.run", names)

    def test_syntax_error_becomes_warning(self) -> None:
        (self.root / "src" / "broken.py").write_text("def broken(:\n", encoding="utf-8")
        symbols, warnings = extractor.extract_symbols(self.root, self.config, "v1.0")
        self.assertTrue(any("broken.py" in warning for warning in warnings))
        self.assertTrue(symbols)

    def test_excluded_paths(self) -> None:
        tests = self.root / "src" / "tests"
        tests.mkdir()
        (tests / "test_x.py").write_text("def test_x() -> None:\n    pass\n", encoding="utf-8")
        symbols, _ = extractor.extract_symbols(self.root, self.config, "v1.0")
        self.assertFalse(any("test_x" in s.qualified_name for s in symbols))


if __name__ == "__main__":
    unittest.main()
