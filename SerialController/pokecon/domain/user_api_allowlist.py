#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""利用者スクリプトの公開API面の正本（GUI非依存）。

Commands.* のうち利用者スクリプトが使ってよい経路と、標準ライブラリ
以外で許可するトップレベル名を一か所で持つ。正本はこのファイル。
`tools/gates/check_user_api.py`・`core/blockly_validate.py`・
`core/pack_zip.py` はここから読むだけにし、個別に複写しない。

表面ごとの深刻度の違い（仕様）:
  - `tools/gates/check_user_api.py`: 許可外は違反（非ゼロ終了で落とす）。
  - `core/blockly_validate.py`: 許可外・未知は異常（保存不可）。
  - `core/pack_zip.py`: 相対 import・公開API面外の Commands 経路は
    異常（導入不可）、見知らぬトップレベルは注意（導入は通す）。
この違いは意図的で、統一しない。保存時は厳しく、配布時は先方の
環境差を注意で残す。
"""

from __future__ import annotations

# 公開面は2段に分ける。基準は「公開済みの Poke-Controller Modified
# （Moi-poke/Poke-Controller-Modified の master）にあったか」。
#
# FROZEN: 公開済みの版に存在した経路。利用者のスクリプトが既に書いて
#   いる可能性があるため、名前も import 経路も動かさない。追加だけ許す。
#   （master の SerialController/Commands/ にあった7モジュール）
FROZEN_COMMANDS_SUBS = frozenset(
    {
        "CommandBase",
        "Keys",
        "McuCommandBase",
        "PythonCommandBase",
        "Sender",
        "StickCommand",
        "UnitCommand",
    }
)

# PROVISIONAL: v4 で増えた経路。v4.0 が公開されるまでは利用者が存在
#   しないので、自由に統合・改名・削除してよい。公開した時点で FROZEN
#   へ移す（移動の判断はこのファイル1か所で行う）。
PROVISIONAL_COMMANDS_SUBS = frozenset(
    {
        "WakeLink",
        "CommandVision",
        "CommandAudio",
        "CommandColor",
    }
)

# 検査が実際に使う集合（両段の和）。blockly_validate / pack_zip は
#   「保存・配布を通してよいか」を見るので、段の区別までは要らない。
ALLOWED_COMMANDS_SUBS = FROZEN_COMMANDS_SUBS | PROVISIONAL_COMMANDS_SUBS

# 標準ライブラリ以外で許可するトップレベル名（pyproject の依存＋実績）。
THIRD_PARTY = frozenset(
    {
        "cv2",
        "numpy",
        "PIL",
        "pandas",
        "scipy",
        "requests",
        "yaml",
        "loguru",
        "deprecated",
        "pynput",
        "serial",
        "matplotlib",
        # 利用者スクリプトの実績（listen_shiny.py が使用）
        "pyaudio",
        "sounddevice",
    }
)
