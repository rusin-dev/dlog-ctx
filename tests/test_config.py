"""Tests for configuration loading and merge precedence."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from decisions.core.config import Config, write_default


class ConfigTest(unittest.TestCase):
    """Loading, merging and defaulting of ``decisions.toml`` files."""

    def setUp(self) -> None:
        # `enterContext` registers the cleanup with unittest: the temporary
        # directory is removed even when an assertion fails.
        self._tmp = self.enterContext(
            tempfile.TemporaryDirectory()  # pylint: disable=consider-using-with
        )
        self.root = Path(self._tmp)

    def test_defaults_without_files(self) -> None:
        """Every field falls back to a default when no config file exists."""
        config = Config.load(self.root)
        self.assertEqual(config.source_roots, ["src"])
        self.assertTrue(config.tombstones)
        self.assertIsNone(config.languages)
        self.assertEqual(config.index_path, self.root / ".decisions/index.db")
        self.assertEqual(config.topic_patterns, {})

    def test_write_default_creates_local_file(self) -> None:
        """``write_default`` creates the local file once and never overwrites it."""
        path = write_default(self.root)
        self.assertTrue(path.exists())
        self.assertEqual(path.relative_to(self.root).as_posix(), ".decisions/config.toml")
        self.assertIn("languages", path.read_text(encoding="utf-8"))
        # Never overwrite an existing local file.
        path.write_text("# custom\n", encoding="utf-8")
        write_default(self.root)
        self.assertEqual(path.read_text(encoding="utf-8"), "# custom\n")

    def test_shared_config_is_read(self) -> None:
        """The committed ``decisions.toml`` is read without a local override."""
        (self.root / "decisions.toml").write_text(
            '[api]\nlanguages = ["python"]\n\n[api.topics]\ncore = ["src/core/**"]\n',
            encoding="utf-8",
        )
        config = Config.load(self.root)
        self.assertEqual(config.languages, ["python"])
        self.assertEqual(config.topic_patterns, {"core": ["src/core/**"]})

    def test_local_overrides_shared_and_keeps_siblings(self) -> None:
        """The gitignored local file overrides keys but keeps sibling settings."""
        (self.root / "decisions.toml").write_text(
            '[api]\nsource_roots = ["src"]\nlanguages = ["python"]\n'
            'tombstones = true\n\n[api.topics]\ncore = ["src/core/**"]\n',
            encoding="utf-8",
        )
        local = self.root / ".decisions" / "config.toml"
        local.parent.mkdir()
        local.write_text('[api]\nlanguages = ["go"]\ntombstones = false\n', encoding="utf-8")
        config = Config.load(self.root)
        self.assertEqual(config.languages, ["go"])
        self.assertFalse(config.tombstones)
        self.assertEqual(config.source_roots, ["src"])
        self.assertEqual(config.topic_patterns, {"core": ["src/core/**"]})

    def test_invalid_config_raises(self) -> None:
        """A malformed TOML file surfaces as ``ValueError``."""
        (self.root / "decisions.toml").write_text("[api\nbroken", encoding="utf-8")
        with self.assertRaises(ValueError):
            Config.load(self.root)

    def test_index_path_override(self) -> None:
        """``index.path`` relocates the SQLite index."""
        (self.root / "decisions.toml").write_text(
            '[index]\npath = ".decisions/other.db"\n', encoding="utf-8"
        )
        self.assertEqual(Config.load(self.root).index_path, self.root / ".decisions/other.db")


if __name__ == "__main__":
    unittest.main()
