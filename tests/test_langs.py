"""Tests for non-Python backends and the extractor registry."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from decisions.api import extractor, langs
from decisions.api.langs import Backend
from decisions.core.config import Config
from decisions.core.models import ApiSymbol

TS_SOURCE = """
/** Max retries before giving up. */
export const MAX_RETRIES = 3;

export interface Route {
  path: string;
}

export class Server {
  /** Start listening. */
  public async start(port: number): Promise<void> {
    if (port) {
      return;
    }
  }

  _internal() {}
}

export function createServer(routes: Route[]): Server {
  return new Server();
}

export const loadConfig = async (path: string) => {
  return path;
};
"""

GO_SOURCE = """
package web

type Server struct {
	addr string
}

// Start begins listening.
func (s *Server) Start(port int) error {
	return nil
}

func New(addr string) *Server {
	return &Server{addr: addr}
}

const MAX_CONN = 10
"""

RUST_SOURCE = """
/// Cache entry.
pub struct Cache {
    size: usize,
}

impl Cache {
    /// Create a cache.
    pub fn new(size: usize) -> Self {
        Cache { size }
    }
}

pub const MAX_ENTRIES: usize = 128;

/// Free helper.
pub fn helper() {}
"""

JAVA_SOURCE = """
package web;

public class Routes {
    public static final int MAX = 10;

    /** Register a route. */
    public void register(String path) {
    }
}
"""

RUBY_SOURCE = """
# Task helpers.
class Task
  MAX = 10

  # Run the task.
  def run(args)
    args
  end
end

def helper
end
"""

PHP_SOURCE = """
<?php
// Greeter.
class Greeter {
    const MAX = 5;

    /** Say hello. */
    public function hello(string $name): string {
        return $name;
    }
}

function greet(string $name): string {
    return $name;
}
"""


class GenericBackendTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "src" / "web").mkdir(parents=True)
        self.files = {
            "src/web/app.ts": TS_SOURCE,
            "src/web/main.go": GO_SOURCE,
            "src/web/lib.rs": RUST_SOURCE,
            "src/web/Routes.java": JAVA_SOURCE,
            "src/web/tasks.rb": RUBY_SOURCE,
            "src/web/greet.php": PHP_SOURCE,
        }
        for rel, text in self.files.items():
            (self.root / rel).write_text(text, encoding="utf-8")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def extract(self, **config_overrides) -> dict[str, ApiSymbol]:
        config = Config(
            self.root,
            {"api": {"source_roots": ["src"], **config_overrides}},
        )
        symbols, warnings = extractor.extract_symbols(self.root, config, "v1.0")
        self.assertEqual(warnings, [])
        return {symbol.qualified_name: symbol for symbol in symbols}

    # -- TypeScript / JavaScript ---------------------------------------
    def test_typescript_symbols(self) -> None:
        symbols = self.extract()
        for name in (
            "web.app.Server",
            "web.app.Server.start",
            "web.app.Route",
            "web.app.createServer",
            "web.app.loadConfig",
            "web.app.MAX_RETRIES",
        ):
            self.assertIn(name, symbols)
        self.assertNotIn("web.app.Server._internal", symbols)
        self.assertEqual(symbols["web.app.Server.start"].kind, "method")
        self.assertEqual(symbols["web.app.createServer"].kind, "function")
        self.assertEqual(symbols["web.app.MAX_RETRIES"].kind, "constant")
        self.assertEqual(symbols["web.app.Server.start"].doc, "Start listening.")
        self.assertEqual(symbols["web.app.MAX_RETRIES"].doc, "Max retries before giving up.")
        self.assertEqual(symbols["web.app.MAX_RETRIES"].topic, "web")

    # -- Go --------------------------------------------------------------
    def test_go_symbols(self) -> None:
        symbols = self.extract()
        self.assertIn("web.main.Server", symbols)
        self.assertIn("web.main.Server.Start", symbols)
        self.assertIn("web.main.New", symbols)
        self.assertIn("web.main.MAX_CONN", symbols)
        self.assertEqual(symbols["web.main.Server.Start"].kind, "method")
        self.assertEqual(symbols["web.main.Server.Start"].doc, "Start begins listening.")
        self.assertEqual(symbols["web.main.New"].kind, "function")

    # -- Rust ------------------------------------------------------------
    def test_rust_symbols(self) -> None:
        symbols = self.extract()
        self.assertIn("web.lib.Cache", symbols)
        self.assertIn("web.lib.Cache.new", symbols)
        self.assertIn("web.lib.MAX_ENTRIES", symbols)
        self.assertIn("web.lib.helper", symbols)
        self.assertEqual(symbols["web.lib.Cache.new"].kind, "method")
        self.assertEqual(symbols["web.lib.Cache"].doc, "Cache entry.")
        # `impl Cache` only establishes scope, it must not duplicate the symbol.
        self.assertEqual([name for name in symbols if name == "web.lib.Cache"], ["web.lib.Cache"])

    # -- Java / Ruby / PHP ----------------------------------------------
    def test_java_symbols(self) -> None:
        symbols = self.extract()
        self.assertIn("web.Routes.Routes", symbols)
        self.assertIn("web.Routes.Routes.register", symbols)
        self.assertIn("web.Routes.Routes.MAX", symbols)
        self.assertEqual(symbols["web.Routes.Routes.register"].doc, "Register a route.")

    def test_ruby_symbols(self) -> None:
        symbols = self.extract()
        self.assertIn("web.tasks.Task", symbols)
        self.assertIn("web.tasks.Task.run", symbols)
        self.assertIn("web.tasks.Task.MAX", symbols)
        self.assertIn("web.tasks.helper", symbols)
        self.assertEqual(symbols["web.tasks.Task.run"].kind, "method")
        self.assertEqual(symbols["web.tasks.helper"].kind, "function")
        self.assertEqual(symbols["web.tasks.Task.run"].doc, "Run the task.")

    def test_php_symbols(self) -> None:
        symbols = self.extract()
        self.assertIn("web.greet.Greeter", symbols)
        self.assertIn("web.greet.Greeter.hello", symbols)
        self.assertIn("web.greet.Greeter.MAX", symbols)
        self.assertIn("web.greet.greet", symbols)
        self.assertEqual(symbols["web.greet.Greeter.hello"].kind, "method")

    # -- configuration ---------------------------------------------------
    def test_language_restriction(self) -> None:
        config = Config(
            self.root,
            {"api": {"source_roots": ["src"], "languages": ["python"]}},
        )
        symbols, _ = extractor.extract_symbols(self.root, config, "v1.0")
        self.assertEqual(symbols, [])
        self.assertNotIn(".ts", extractor.supported_suffixes(config))
        self.assertIn(".ts", extractor.supported_suffixes())

    def test_language_restriction_accepts_suffix(self) -> None:
        config = Config(
            self.root,
            {"api": {"source_roots": ["src"], "languages": [".ts"]}},
        )
        symbols, _ = extractor.extract_symbols(self.root, config, "v1.0")
        names = {symbol.qualified_name for symbol in symbols}
        self.assertIn("web.app.Server", names)
        self.assertNotIn("web.main.Server", names)

    def test_language_restriction_accepts_alias(self) -> None:
        config = Config(
            self.root,
            {"api": {"source_roots": ["src"], "languages": ["python", "typescript"]}},
        )
        symbols, _ = extractor.extract_symbols(self.root, config, "v1.0")
        names = {symbol.qualified_name for symbol in symbols}
        self.assertIn("web.app.Server", names)
        self.assertNotIn("web.main.Server", names)

    def test_decision_refs_from_comments(self) -> None:
        path = self.root / "src" / "web" / "refs.go"
        path.write_text(
            "// Chosen for reasons.\n//\n// @decision:storage/use-sqlite\nfunc RefOne() {}\n",
            encoding="utf-8",
        )
        symbols = self.extract()
        symbol = symbols["web.refs.RefOne"]
        self.assertEqual(symbol.decision_refs, ["storage/use-sqlite"])
        self.assertNotIn("@decision:", symbol.doc or "")


class RegistryTest(unittest.TestCase):
    def test_builtin_backends_are_registered(self) -> None:
        self.assertIn("python", langs.available())
        for name in ("javascript", "go", "rust", "java", "kotlin", "csharp", "ruby", "php"):
            self.assertIn(name, langs.available())
        self.assertEqual(langs.backend_for(".py").name, "python")
        self.assertEqual(langs.backend_for(".tsx").name, "javascript")
        self.assertIsNone(langs.backend_for(".txt"))

    def test_custom_backend_is_picked_up(self) -> None:
        def fake_extract(
            text: str,
            *,
            module: str,
            topic: str,
            doc_path: str,
            rel_path: str,
        ) -> list[ApiSymbol]:
            return [
                ApiSymbol(
                    qualified_name=f"{module}.custom",
                    kind="function",
                    module=module,
                    topic=topic,
                    doc_path=doc_path,
                    signature="def custom()",
                    source_file=rel_path,
                )
            ]

        langs.register(Backend(name="fake", suffixes=(".fake",), extract=fake_extract))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "src").mkdir()
            (root / "src" / "thing.fake").write_text("whatever", encoding="utf-8")
            config = Config(root, {"api": {"source_roots": ["src"]}})
            symbols, warnings = extractor.extract_symbols(root, config, "v1.0")
        self.assertEqual(warnings, [])
        self.assertEqual([symbol.qualified_name for symbol in symbols], ["thing.custom"])


if __name__ == "__main__":
    unittest.main()
