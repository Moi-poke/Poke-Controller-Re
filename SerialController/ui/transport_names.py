#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""transport_names.py - 通信方式の内部キーと画面表示名の対応。

内部キーは保存・比較・ログに使い、画面には何がつながるか分かる名前を出す。
「BCON」「USB直結」だけでは PC と Pico、Pico と Switch のどちらを指すのか
分からないため、PC↔Pico は USB、Pico が Switch を中継する、と読める名前にする。
表に無い方式（利用者のプラグイン）はキーをそのまま返す（候補から落とさない）。
"""

from __future__ import annotations

from core.transport.registry import canonical_transport_name

DISPLAY_NAMES: dict[str, str] = {
    "legacy_text": "マイコン直結（USB シリアル）",
    "pico_uart": "Pico UART（旧方式）",
    "switch-bcon": "Pico 中継（USB）",
    "switch-bcon-proc": "Pico 中継（別プロセス）",
}


def display_name(name: str) -> str:
    """内部キー→画面の表示名。未知の方式はキーのまま。"""
    key = canonical_transport_name(name) or str(name)
    return DISPLAY_NAMES.get(key, key)
