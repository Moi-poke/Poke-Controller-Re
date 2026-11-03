"""`python -m pokecon` の入口。実体は pokecon.app.Window.main。"""

from __future__ import annotations

import sys
from pathlib import Path

_APP_ROOT = str(Path(__file__).resolve().parent.parent)
if _APP_ROOT not in sys.path:
    sys.path.insert(0, _APP_ROOT)

from pokecon.app.Window import main  # noqa: E402

if __name__ == "__main__":
    main()
