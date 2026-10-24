"""Language backend registry.

Python is parsed with :mod:`ast` (exact). Other languages use a best-effort
regex backend; anything better can be shipped as a plugin without touching the
pipeline:

```toml
# pyproject.toml of a plugin package
[project.entry-points."decisions.extractors"]
mylang = "my_package.extractor:Backend"
```

The entry point must resolve to a :class:`Backend` instance or a zero-argument
callable returning one.
"""

from __future__ import annotations

import importlib.metadata
from dataclasses import dataclass
from typing import Protocol

from ...core.models import ApiSymbol

PLUGIN_GROUP = "decisions.extractors"


class ExtractCallable(Protocol):
    def __call__(
        self,
        text: str,
        *,
        module: str,
        topic: str,
        doc_path: str,
        rel_path: str,
    ) -> list[ApiSymbol]: ...


@dataclass(frozen=True)
class Backend:
    """One language backend: which suffixes it owns and how to extract them."""

    name: str
    suffixes: tuple[str, ...]
    extract: ExtractCallable


_REGISTRY: dict[str, Backend] = {}
_PLUGINS_LOADED = False
_PLUGIN_WARNINGS: list[str] = []


def register(backend: Backend) -> None:
    """Register a backend; later registrations win for the same suffix."""
    for suffix in backend.suffixes:
        _REGISTRY[suffix.lower()] = backend


def backend_for(suffix: str) -> Backend | None:
    return _REGISTRY.get(suffix.lower())


def suffixes() -> tuple[str, ...]:
    return tuple(sorted(_REGISTRY))


def backends() -> list[Backend]:
    """Unique registered backends (ordered by name)."""
    unique: dict[str, Backend] = {}
    for backend in _REGISTRY.values():
        unique.setdefault(backend.name, backend)
    return [unique[name] for name in sorted(unique)]


def backend_by_name(name: str) -> Backend | None:
    for backend in backends():
        if backend.name == name:
            return backend
    return None


def available() -> list[str]:
    return [backend.name for backend in backends()]


def load_plugins() -> list[str]:
    """Discover ``decisions.extractors`` entry points once per process."""
    global _PLUGINS_LOADED
    if _PLUGINS_LOADED:
        return []
    _PLUGINS_LOADED = True
    try:
        entry_points = importlib.metadata.entry_points(group=PLUGIN_GROUP)
    except TypeError:  # pragma: no cover - very old importlib.metadata
        entry_points = importlib.metadata.entry_points().get(PLUGIN_GROUP, [])
    except Exception as exc:  # pragma: no cover - broken metadata
        _PLUGIN_WARNINGS.append(f"cannot list extractor plugins: {exc}")
        return list(_PLUGIN_WARNINGS)
    for entry_point in entry_points:
        try:
            loaded = entry_point.load()
            backend = loaded() if not isinstance(loaded, Backend) else loaded
            if not isinstance(backend, Backend):
                raise TypeError(f"expected Backend, got {type(backend).__name__}")
            register(backend)
        except Exception as exc:  # pragma: no cover - third-party code
            _PLUGIN_WARNINGS.append(f"extractor plugin {entry_point.name!r} failed: {exc}")
    return list(_PLUGIN_WARNINGS)


def _register_builtins() -> None:
    from . import generic, python_ast

    register(python_ast.BACKEND)
    for backend in generic.BACKENDS:
        register(backend)


_register_builtins()
