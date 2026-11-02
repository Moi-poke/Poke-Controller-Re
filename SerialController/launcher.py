#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""互換の入口。実体は [`pokecon/app/launcher.py`](pokecon/app/launcher.py)。

`python SerialController/launcher.py` をそのまま動かすための薄い扉。
"""

from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from pokecon.app.launcher import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
