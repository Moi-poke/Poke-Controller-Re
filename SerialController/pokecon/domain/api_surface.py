#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""利用者スクリプト向け公開面のうち、公開済み版で約束した名前の一覧（GUI非依存）。

基準は公開済みの Poke-Controller Modified
（Moi-poke/Poke-Controller-Modified の `master`）の
`SerialController/Commands/` に実在した7モジュールと、そこから
import できた名前である。2026-10-31 に同リポジトリの生ファイルから写した。

規則:
  - ここに載せた名前は消さない・改名しない（追加だけ許す）。
  - 段の区別（凍結 / 暫定）は `core/user_api_allowlist.py` が持つ。
    このファイルは「何が import できる約束か」だけを持つ。
  - 検査は `tools/gates/check_api_surface.py`（task api）。ファイルを
    import せず AST でトップレベル名を数えるので、GUI も cv2 も要らない。

なぜ機械可読で持つか: 互換の判断を人の記憶に置くと、Re: 側の再編で
公開名が静かに消える。2026-10-31 の時点で `PokeConDialogue` と
`TEMPLATE_PATH` が既に `Commands.PythonCommandBase` から消えていた
（Modified の 794 行版には存在した）。
"""

from __future__ import annotations

# モジュール（import 経路） → そのモジュールから import できる名前。
# 名前は Modified `master` のソースで `class` / 代入されていたもの。
FROZEN_MODULES: dict[str, tuple[str, ...]] = {
    "Commands.CommandBase": ("Command",),
    "Commands.Keys": (
        "Button",
        "Hat",
        "Stick",
        "Tilt",
        "SendFormat",
        "Direction",
        "KeyPress",
    ),
    "Commands.McuCommandBase": ("McuCommand",),
    "Commands.PythonCommandBase": (
        "StopThread",
        "PythonCommand",
        "PokeConDialogue",
        "TEMPLATE_PATH",
        "ImageProcPythonCommand",
    ),
    "Commands.Sender": ("Sender",),
    "Commands.StickCommand": (
        "StickCommand",
        "StickLeft",
        "StickRight",
    ),
    "Commands.UnitCommand": (
        "UnitCommand",
        "A",
        "B",
        "X",
        "Y",
        "L",
        "R",
        "ZL",
        "ZR",
        "MINUS",
        "PLUS",
        "LCLICK",
        "RCLICK",
        "HOME",
        "CAPTURE",
        "UP",
        "UP_RIGHT",
        "RIGHT",
        "DOWN_RIGHT",
        "DOWN",
        "DOWN_LEFT",
        "LEFT",
        "UP_LEFT",
    ),
}
