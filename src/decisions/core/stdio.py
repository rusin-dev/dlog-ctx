"""Console helpers shared by both entry points."""

from __future__ import annotations

import contextlib
import sys


def configure_stdio() -> None:
    """Force UTF-8 output so piped output stays valid for agents and CI.

    Windows consoles default to a legacy codepage; writing UTF-8 keeps
    ``decisions context --json`` and CJK decision titles byte-exact when the
    output is captured by another program.
    """
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(AttributeError, ValueError):
            stream.reconfigure(encoding="utf-8")
