"""Allow ``python -m decisions`` to behave like the ``decisions`` script."""

from __future__ import annotations

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
