#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""互換の入口。実体は [`pokecon/app/Window.py`](pokecon/app/Window.py)。

`python SerialController/Window.py`（launcher.py が使う従来経路）を
そのまま動かすための薄い扉。新しいコードは `python -m pokecon` を使う。
"""

from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from pokecon.app.Window import main  # noqa: E402

if __name__ == "__main__":
    main()
