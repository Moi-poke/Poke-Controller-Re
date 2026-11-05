#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""controller_preview.py - 3Dプレビュー用の操作状態（Tkなし）。

IMUパネルが同じパッドから読んだ PadState を、描画側がそのまま使える
物理入力の形へ直す。送信・マクロ・割当の結果ではなく、生の押下と
スティック位置だけを持つ。

ボタン名は gamepad_map の既存表をそのまま使う。PadState の名前は
XInput 経路と SDL 経路で同じ意味に揃っているため、ここで A/B/X/Y を
入れ替えたりはしない。SL/SR は現在の読取口が報告しないが、報告する
読取口が将来あっても落とさないよう、その名前だけは素通しする。

スティックは SHORT（-32768〜32767）を -1.0〜1.0 へ直す。負側は
-32768 で割り、正側は 32767 で割る（非対称のまま ±1.0 に届かせる）。
Y は上が正（PadState と同じ向き）。斜め入力が √2 倍にならないよう、
半径が 1 を超えたときだけ円周へ縮める。遊びや設定の反映は行わない
（プレビューはパネルの送出値ではなく、パッドの物理状態を見る）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Final

from pokecon.domain.gamepad_map import BUTTON_MAP, DPAD_MAP, TRIGGER_MAP
from pokecon.domain.xinput import DEADZONE_TRIGGER, PadState

#: 描画側と共有する操作名。並び順がそのままボタンビットの位置になる。
CONTROL_NAMES: Final[tuple[str, ...]] = (
    "A",
    "B",
    "X",
    "Y",
    "L",
    "R",
    "ZL",
    "ZR",
    "PLUS",
    "MINUS",
    "HOME",
    "CAPTURE",
    "LCLICK",
    "RCLICK",
    "UP",
    "DOWN",
    "LEFT",
    "RIGHT",
    "SL",
    "SR",
)

_CONTROL_INDEX: Final[dict[str, int]] = {
    name: index for index, name in enumerate(CONTROL_NAMES)
}

# 押下名 → Switch 側の操作名。十字キーも同じ表にまとめる。
_DIGITAL_MAP: Final[dict[str, str]] = {**BUTTON_MAP, **DPAD_MAP}

# 読取口が報告した場合だけ素通しする名前（新しいハード対応はしない）。
_PASSTHROUGH: Final[frozenset[str]] = frozenset(("SL", "SR"))


@dataclass(frozen=True, slots=True)
class PreviewControls:
    """1 回分の物理操作状態。buttons は CONTROL_NAMES のビット列。"""

    buttons: int = 0
    lx: float = 0.0
    ly: float = 0.0
    rx: float = 0.0
    ry: float = 0.0
    connected: bool = False


def _axis_to_unit(raw: Any) -> float:
    """SHORT 1 軸を -1.0〜1.0 へ直す。非数は中立。"""
    try:
        value = int(raw)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    if value < 0:
        return max(-1.0, value / 32768.0)
    return min(1.0, value / 32767.0)


def _stick(raw_x: Any, raw_y: Any) -> tuple[float, float]:
    """SHORT の XY を単位円内の -1.0〜1.0 へ直す。"""
    x = _axis_to_unit(raw_x)
    y = _axis_to_unit(raw_y)
    radius = math.hypot(x, y)
    if radius > 1.0:
        x /= radius
        y /= radius
    return x, y


def _buttons(state: PadState) -> int:
    """押下名とトリガを CONTROL_NAMES のビット列へ直す。"""
    bits = 0
    for name in state.pressed:
        mapped = _DIGITAL_MAP.get(name)
        if mapped is None and name in _PASSTHROUGH:
            mapped = name
        if mapped is None:
            continue
        index = _CONTROL_INDEX.get(mapped)
        if index is not None:
            bits |= 1 << index
    if int(state.left_trigger) > DEADZONE_TRIGGER:
        bits |= 1 << _CONTROL_INDEX[TRIGGER_MAP["LEFT_TRIGGER"]]
    if int(state.right_trigger) > DEADZONE_TRIGGER:
        bits |= 1 << _CONTROL_INDEX[TRIGGER_MAP["RIGHT_TRIGGER"]]
    return bits


def controls_from_pad(state: PadState) -> PreviewControls:
    """PadState を描画用の操作状態へ直す（読むだけの純関数）。

    未接続は全操作を中立に戻す。スティックは遊びを掛けず、そのまま
    -1.0〜1.0 の物理位置として渡す。
    """
    if not state.connected:
        return PreviewControls()
    lx, ly = _stick(state.thumb_lx, state.thumb_ly)
    rx, ry = _stick(state.thumb_rx, state.thumb_ry)
    return PreviewControls(
        buttons=_buttons(state),
        lx=lx,
        ly=ly,
        rx=rx,
        ry=ry,
        connected=True,
    )
