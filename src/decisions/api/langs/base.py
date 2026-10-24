"""Types shared by the language backends.

Keeping the registry types here (instead of in :mod:`decisions.api.langs`)
lets the built-in backends import them without creating an import cycle, while
``langs`` still re-exports them for plugins.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ...core.models import ApiSymbol


class ExtractCallable(Protocol):
    """Signature every backend exposes: text in, symbols out."""

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


def module_symbol(
    *,
    module: str,
    topic: str,
    doc_path: str,
    rel_path: str,
    doc: str | None = None,
) -> ApiSymbol:
    """Build the module-level symbol every backend emits first."""
    return ApiSymbol(
        qualified_name=module,
        kind="module",
        module=module,
        topic=topic,
        doc_path=doc_path,
        signature=f"module {module}",
        doc=doc,
        source_file=rel_path,
        lineno=1,
    )
