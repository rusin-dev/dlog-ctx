"""Symbol extraction orchestration.

Walking, module/topic naming and warnings live here; the actual parsing is
delegated to :mod:`decisions.api.langs` backends (Python via ``ast``, a regex
backend for common languages, plus third-party plugins).
"""

from __future__ import annotations

import fnmatch
from pathlib import Path

from ..core.config import Config
from ..core.models import ApiSymbol
from . import langs, render

SKIP_DIRS = {
    "__pycache__",
    ".git",
    ".venv",
    "venv",
    "env",
    "node_modules",
    "build",
    "dist",
    "target",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    "site-packages",
    ".eggs",
}

#: Convenience aliases accepted in ``api.languages``.
LANGUAGE_ALIASES = {
    "py": "python",
    "python3": "python",
    "js": "javascript",
    "jsx": "javascript",
    "ts": "javascript",
    "tsx": "javascript",
    "typescript": "javascript",
    "golang": "go",
    "rs": "rust",
    "rb": "ruby",
    "cs": "csharp",
    "c#": "csharp",
    "kt": "kotlin",
    "kts": "kotlin",
}


def extract_symbols(
    root: Path,
    config: Config | None = None,
    version: str = "v0.0",
) -> tuple[list[ApiSymbol], list[str]]:
    """Extract public symbols from every supported file under the source roots.

    Returns ``(symbols, warnings)``. Parse failures become warnings instead of
    aborting the sync: a single broken file must not freeze documentation.
    """
    config = config or Config.load(root)
    warnings = langs.load_plugins()
    enabled = _enabled_backends(config)
    symbols: list[ApiSymbol] = []

    for rel_path, source_root in _iter_source_files(root, config):
        suffix = Path(rel_path).suffix.lower()
        backend = langs.backend_for(suffix)
        if backend is None:
            continue
        if enabled is not None and backend.name not in enabled:
            continue
        path = root / rel_path
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            warnings.append(f"{rel_path}: {exc}")
            continue
        module = _module_name(rel_path, source_root)
        topic = _topic_for(rel_path, module, config)
        doc_path = render.doc_path(version, topic)
        location = {
            "module": module,
            "topic": topic,
            "doc_path": doc_path,
            "rel_path": rel_path,
        }
        try:
            symbols.extend(backend.extract(text, **location))
        except SyntaxError as exc:
            where = f"line {exc.lineno}" if exc.lineno else "unknown line"
            warnings.append(f"{rel_path}: {exc.msg} ({where})")
        except (ValueError, RecursionError) as exc:
            warnings.append(f"{rel_path}: {exc}")
        except Exception as exc:  # pylint: disable=broad-exception-caught
            # Plugin backends are third-party code: report and keep going.
            warnings.append(f"{rel_path}: extractor {backend.name} failed: {exc}")

    symbols.sort(key=lambda symbol: symbol.qualified_name)
    unique: dict[str, ApiSymbol] = {}
    for symbol in symbols:
        # Rust `impl Foo` and `struct Foo` share a name; keep the declaration
        # that produced a real symbol first (scope-only containers never emit).
        unique.setdefault(symbol.qualified_name, symbol)
    return list(unique.values()), warnings


def supported_suffixes(config: Config | None = None) -> tuple[str, ...]:
    """Suffixes the enabled backends can parse (useful for docs/UX)."""
    enabled = _enabled_backends(config) if config is not None else None
    result: list[str] = []
    for backend in langs.backends():
        if enabled is None or backend.name in enabled:
            result.extend(backend.suffixes)
    return tuple(sorted(set(result)))


def _enabled_backends(config: Config) -> set[str] | None:
    """Resolve ``api.languages`` (backend names or suffixes) to backend names."""
    names = config.languages
    if names is None:
        return None
    resolved: set[str] = set()
    for token in names:
        candidate = LANGUAGE_ALIASES.get(token.strip().lower(), token.strip().lower())
        backend = langs.backend_by_name(candidate)
        if backend is None:
            backend = langs.backend_for(candidate if candidate.startswith(".") else f".{candidate}")
        if backend is not None:
            resolved.add(backend.name)
    if not resolved:
        return None
    return resolved


def _iter_source_files(root: Path, config: Config) -> list[tuple[str, str]]:
    """Return ``(repo-relative file, source root)`` pairs for supported files."""
    known_suffixes = set(langs.suffixes())
    found: dict[str, str] = {}
    for source_root in config.source_roots:
        base = root / source_root
        if not base.exists():
            continue
        candidates = [base] if base.is_file() else sorted(base.rglob("*"))
        for candidate in candidates:
            if not candidate.is_file():
                continue
            if candidate.suffix.lower() not in known_suffixes:
                continue
            try:
                rel_path = candidate.relative_to(root).as_posix()
            except ValueError:
                continue
            parts = rel_path.split("/")
            if any(part in SKIP_DIRS for part in parts[:-1]):
                continue
            if _excluded(rel_path, parts, config.exclude):
                continue
            found.setdefault(rel_path, source_root)
    return sorted(found.items())


def _excluded(rel_path: str, parts: list[str], patterns: list[str]) -> bool:
    for pattern in patterns:
        if fnmatch.fnmatch(rel_path, pattern):
            return True
        if any(fnmatch.fnmatch(part, pattern) for part in parts[:-1]):
            return True
    return False


def _module_name(rel_path: str, source_root: str) -> str:
    path = rel_path
    root = source_root.strip("/")
    if root and root != "." and path.startswith(root + "/"):
        path = path[len(root) + 1 :]
    parts = [part for part in path.split("/") if part]
    if parts:
        filename = parts[-1]
        stem = filename.rsplit(".", 1)[0] if "." in filename else filename
        if stem == "__init__":
            parts.pop()
        else:
            parts[-1] = stem
    return ".".join(parts) or "__init__"


def _topic_for(rel_path: str, module: str, config: Config) -> str:
    for topic, patterns in config.topic_patterns.items():
        if any(
            fnmatch.fnmatch(rel_path, pattern) or fnmatch.fnmatch(module, pattern)
            for pattern in patterns
        ):
            return topic
    return module.split(".")[0]
