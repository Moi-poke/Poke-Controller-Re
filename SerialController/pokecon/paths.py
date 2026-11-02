#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""実行時の基準ディレクトリ（GUI非依存）。

配布形態は「フォルダを展開して `python SerialController/Window.py`」なので、
パッケージの親（`SerialController/`）が設定ファイル・テンプレート・
利用者スクリプトの置き場になる。`__file__` からの相対計算をここ1か所に集め、
各モジュールが `..` を数えるのをやめる。
"""

from __future__ import annotations

from pathlib import Path

#: `SerialController/`。settings*.ini・Template・db・Commands・Controller_Log の親。
BASE_DIR = str(Path(__file__).resolve().parent.parent)
