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

from . import generic, python_ast
from .base import Backend, ExtractCallable

__all__ = [
    "Backend",
    "ExtractCallable",
    "available",
    "backend_by_name",
    "backend_for",
    "backends",
    "load_plugins",
    "register",
    "suffixes",
]

PLUGIN_GROUP = "decisions.extractors"

_REGISTRY: dict[str, Backend] = {}
_PLUGIN_WARNINGS: list[str] = []
#: ``load_plugins`` runs once per process; a dict keeps the state mutable
#: without a module-level ``global`` statement.
_PLUGIN_STATE = {"loaded": False}


def register(backend: Backend) -> None:
    """Register a backend; later registrations win for the same suffix."""
    for suffix in backend.suffixes:
        _REGISTRY[suffix.lower()] = backend


def backend_for(suffix: str) -> Backend | None:
    """Return the backend owning ``suffix`` (e.g. ``".py"``), if any."""
    return _REGISTRY.get(suffix.lower())


def suffixes() -> tuple[str, ...]:
    """Every suffix a registered backend can parse."""
    return tuple(sorted(_REGISTRY))


def backends() -> list[Backend]:
    """Unique registered backends (ordered by name)."""
    unique: dict[str, Backend] = {}
    for backend in _REGISTRY.values():
        unique.setdefault(backend.name, backend)
    return [unique[name] for name in sorted(unique)]


def backend_by_name(name: str) -> Backend | None:
    """Return the backend called ``name`` (``"python"``, ``"go"``, ...)."""
    for backend in backends():
        if backend.name == name:
            return backend
    return None


def available() -> list[str]:
    """Names of the registered backends."""
    return [backend.name for backend in backends()]


def load_plugins() -> list[str]:
    """Discover ``decisions.extractors`` entry points once per process."""
    if _PLUGIN_STATE["loaded"]:
        return []
    _PLUGIN_STATE["loaded"] = True
    try:
        entry_points = importlib.metadata.entry_points(group=PLUGIN_GROUP)
    except Exception as exc:  # pylint: disable=broad-exception-caught
        # Broken/incomplete metadata must not stop the pipeline: report and go on.
        _PLUGIN_WARNINGS.append(f"cannot list extractor plugins: {exc}")
        return list(_PLUGIN_WARNINGS)
    for entry_point in entry_points:
        try:
            loaded = entry_point.load()
            backend = loaded() if not isinstance(loaded, Backend) else loaded
            if not isinstance(backend, Backend):
                raise TypeError(f"expected Backend, got {type(backend).__name__}")
            register(backend)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            # A third-party backend must never break the built-in ones.
            _PLUGIN_WARNINGS.append(f"extractor plugin {entry_point.name!r} failed: {exc}")
    return list(_PLUGIN_WARNINGS)


def _register_builtins() -> None:
    """Register the built-in Python and regex backends."""
    register(python_ast.BACKEND)
    for backend in generic.BACKENDS:
        register(backend)


_register_builtins()
