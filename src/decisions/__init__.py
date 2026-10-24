"""decisions-ctx: a single-project context indexer.

Two data streams are linked in SQLite:

- **Current state**: API docs under ``docs/api/<version>/``, extracted from code.
- **Why it changed**: decision log under ``docs/decision/``, written by humans.

The main exit point is ``decisions context <path>``: before touching a file it
answers "what is the current contract, why is it this way, and which decisions
does this change affect".
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
