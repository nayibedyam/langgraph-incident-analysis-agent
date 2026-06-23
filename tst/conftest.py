"""Pytest bootstrap.

Adds the ``src/`` directory to ``sys.path`` so tests can import the
``backend`` and ``eval`` packages (``from backend.pipeline... import ...``).
"""

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
