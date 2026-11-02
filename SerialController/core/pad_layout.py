#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""pad_layout.py - 仮想コントローラのボタンの並びと当たり判定。

Pro コントローラと Joy-Con (L)/(R) を上から見た並びを論理座標で持つ。
描画側（ui.controller_pad.ControllerGUI）は倍率を掛けて描くだけにして、
「どこを押したらどのボタンか」の判断をここ 1 箇所に閉じる。Tk には
触れない（core 層）。

名前は Commands.Keys.Button の属性名、十字キーは旧 UI の HAT 名
（"UP" / "UP_RIGHT" …）をそのまま使う。Joy-Con の十字キーは実機どおり
4 つの独立したボタンとして並べるが、送信側の Hat 処理は共通である。
レールの SL/SR は Button に無い（送れない）ため描かない。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Final

# 操作盤の種類。auto は接続中のパッド名から決める（resolve_layout）。
AUTO: Final[str] = "auto"
PROCON: Final[str] = "procon"
JOYCON_L: Final[str] = "joycon_l"
JOYCON_R: Final[str] = "joycon_r"
LAYOUTS: Final[tuple[str, ...]] = (AUTO, PROCON, JOYCON_L, JOYCON_R)
# 画面に出す名前。保存値は上のキーのまま（表示名を変えても設定を壊さない）。
LAYOUT_LABELS: Final[dict[str, str]] = {
    AUTO: "自動",
    PROCON: "プロコン",
    JOYCON_L: "Joy-Con (L)",
    JOYCON_R: "Joy-Con (R)",
}

# 論理座標の広さ。等倍（96dpi）で 1 単位 = 1px。旧 UI の 600x300 窓の半分以下。
PAD_W: Final[int] = 420
PAD_H: Final[int] = 170
# Joy-Con は片手分なので縦長。ProCon と同じく、これ以上は広げない。
JOYCON_W: Final[int] = 176
JOYCON_H: Final[int] = 300
# 押せる的の最小寸法（WCAG 2.2 のポインタの的 24px）。
MIN_TARGET: Final[int] = 24
# 広い所に置いても画面を占領しないよう、DPI 倍率に対してここまでしか広げない。
MAX_ZOOM: Final[float] = 1.5
# 別窓は利用者が窓の大きさで決めるので、大きめまで追従する。
FLOATING_MAX_ZOOM: Final[float] = 3.0
# 極端に狭いときも描画が壊れない下限。
MIN_SCALE: Final[float] = 0.25
# 十字キーの真ん中は向きが決まらないので何も押さない（半径に対する割合）。
_DPAD_DEAD: Final[float] = 0.25

# 0 度（右）から反時計回りに 45 度ずつ。斜めは角を押したときに入る。
DPAD_NAMES: Final[tuple[str, ...]] = (
    "RIGHT",
    "UP_RIGHT",
    "UP",
    "UP_LEFT",
    "LEFT",
    "DOWN_LEFT",
    "DOWN",
    "DOWN_RIGHT",
)


@dataclass(frozen=True, slots=True)
class PadShape:
    """1 つの的。cx/cy は中心、w/h は的の外接矩形の寸法（論理座標）。

    kind: "pill"（肩のボタン）/ "circle" / "square"（キャプチャ）/
    "stick"（スティック押し込み）/ "dpad"（十字キー。name は "DPAD"）。
    """

    name: str
    kind: str
    cx: float
    cy: float
    w: float
    h: float
    label: str


SHAPES: Final[tuple[PadShape, ...]] = (
    PadShape("ZL", "pill", 46, 17, 60, 24, "ZL"),
    PadShape("L", "pill", 112, 17, 60, 24, "L"),
    PadShape("R", "pill", 308, 17, 60, 24, "R"),
    PadShape("ZR", "pill", 374, 17, 60, 24, "ZR"),
    PadShape("LCLICK", "stick", 84, 76, 52, 52, "LS"),
    PadShape("DPAD", "dpad", 140, 128, 60, 60, ""),
    PadShape("MINUS", "circle", 176, 52, 24, 24, "−"),
    PadShape("PLUS", "circle", 244, 52, 24, 24, "+"),
    PadShape("CAPTURE", "square", 186, 92, 24, 24, ""),
    PadShape("HOME", "circle", 234, 92, 26, 26, "⌂"),
    PadShape("X", "circle", 336, 48, 28, 28, "X"),
    PadShape("Y", "circle", 308, 76, 28, 28, "Y"),
    PadShape("A", "circle", 364, 76, 28, 28, "A"),
    PadShape("B", "circle", 336, 104, 28, 28, "B"),
    PadShape("RCLICK", "stick", 280, 128, 52, 52, "RS"),
)

# Joy-Con (L): 左スティック・十字ボタン 4 つ・L/ZL・−・キャプチャ。
# 実機の十字ボタンは独立した丸ボタンなので、dpad ではなく方向名の丸で置く。
# これにより押した向きがそのまま既存の Hat 処理（HAT_NAME2DIRS）へ渡る。
_JOYCON_L_SHAPES: Final[tuple[PadShape, ...]] = (
    PadShape("ZL", "pill", 46, 17, 60, 24, "ZL"),
    PadShape("L", "pill", 112, 17, 60, 24, "L"),
    PadShape("MINUS", "circle", 148, 52, 24, 24, "−"),
    PadShape("LCLICK", "stick", 62, 110, 52, 52, "LS"),
    PadShape("UP", "circle", 100, 175, 30, 30, "▲"),
    PadShape("RIGHT", "circle", 130, 205, 30, 30, "▶"),
    PadShape("DOWN", "circle", 100, 235, 30, 30, "▼"),
    PadShape("LEFT", "circle", 70, 205, 30, 30, "◀"),
    PadShape("CAPTURE", "square", 28, 270, 24, 24, ""),
)

# Joy-Con (R): 右スティック・ABXY・R/ZR・＋・HOME。
_JOYCON_R_SHAPES: Final[tuple[PadShape, ...]] = (
    PadShape("ZR", "pill", 130, 17, 60, 24, "ZR"),
    PadShape("R", "pill", 64, 17, 60, 24, "R"),
    PadShape("PLUS", "circle", 28, 52, 24, 24, "+"),
    PadShape("X", "circle", 110, 77, 28, 28, "X"),
    PadShape("Y", "circle", 82, 105, 28, 28, "Y"),
    PadShape("A", "circle", 138, 105, 28, 28, "A"),
    PadShape("B", "circle", 110, 133, 28, 28, "B"),
    PadShape("RCLICK", "stick", 88, 205, 52, 52, "RS"),
    PadShape("HOME", "circle", 148, 270, 26, 26, "⌂"),
)

_LAYOUT_SHAPES: Final[dict[str, tuple[PadShape, ...]]] = {
    PROCON: SHAPES,
    JOYCON_L: _JOYCON_L_SHAPES,
    JOYCON_R: _JOYCON_R_SHAPES,
}
_LAYOUT_SIZES: Final[dict[str, tuple[int, int]]] = {
    PROCON: (PAD_W, PAD_H),
    JOYCON_L: (JOYCON_W, JOYCON_H),
    JOYCON_R: (JOYCON_W, JOYCON_H),
}


def normalize_layout(layout: object) -> str:
    """保存値・画面の選択を描ける種類へ寄せる。auto/不明は ProCon。"""
    key = str(layout or "").strip().lower()
    return key if key in _LAYOUT_SHAPES else PROCON


def layout_shapes(layout: object) -> tuple[PadShape, ...]:
    """その種類の的の並び。不明な種類は ProCon。"""
    return _LAYOUT_SHAPES[normalize_layout(layout)]


def layout_size(layout: object) -> tuple[int, int]:
    """その種類の論理座標の広さ (幅, 高さ)。"""
    return _LAYOUT_SIZES[normalize_layout(layout)]


def detect_layout(pad_name: object) -> str | None:
    """パッドの表示名から Joy-Con の左右を読む。判別できなければ None。

    SDL は「Joy-Con (L)」「Joy-Con (R)」のような実名を返す。左右が
    両方書かれた名前（Joy-Con (L/R) など）は 1 台に決められないため
    None にする。Pro Controller など Joy-Con 以外も None。
    """
    text = re.sub(r"joy[\s_-]*con", "joy con", str(pad_name or "").lower())
    words = re.findall(r"[a-z]+", text)
    if "joy" not in words:
        return None
    tail = words[words.index("joy") + 1 :]
    if "con" not in tail:
        return None
    tail = tail[tail.index("con") + 1 :]
    left = "l" in tail or "left" in tail
    right = "r" in tail or "right" in tail
    if left == right:  # どちらでもない／両方書かれている
        return None
    return JOYCON_L if left else JOYCON_R


def resolve_layout(choice: object, pad_name: object = "") -> str:
    """設定値と接続中のパッド名から、実際に描く種類を決める。

    choice が auto のときだけパッド名を見る。名前から決められない
    ときは ProCon（従来の見た目）へ戻す。
    """
    key = str(choice or "").strip().lower()
    if key in _LAYOUT_SHAPES:
        return key
    return detect_layout(pad_name) or PROCON


def dpad_direction(dx: float, dy: float, radius: float) -> str | None:
    """十字キーの中心からのずれ（画面座標、y は下向き）を 8 方向にする。"""
    dist = math.hypot(dx, dy)
    if dist > radius or dist < radius * _DPAD_DEAD:
        return None
    angle = math.degrees(math.atan2(-dy, dx)) % 360
    return DPAD_NAMES[int(((angle + 22.5) % 360) // 45)]


def hit_test(x: float, y: float, layout: object = PROCON) -> str | None:
    """論理座標の点にある的の名前。十字キーは向きの名前。無ければ None。"""
    for shape in layout_shapes(layout):
        dx, dy = x - shape.cx, y - shape.cy
        if shape.kind == "dpad":
            direction = dpad_direction(dx, dy, shape.w / 2)
            if direction is not None:
                return direction
            continue
        if shape.kind in ("circle", "stick"):
            if math.hypot(dx, dy) <= shape.w / 2:
                return shape.name
            continue
        if abs(dx) <= shape.w / 2 and abs(dy) <= shape.h / 2:
            return shape.name
    return None


# スティックを倒し切るまでのドラッグ量（論理座標）。スティックの外周の半径と同じ。
STICK_REACH: Final[float] = 26.0
# 押し込み（クリック）とドラッグを分ける動きのしきい値（論理座標）。
STICK_TAP_SLOP: Final[float] = 3.0
# スティックの名前（押し込みのボタン名）から、どちら側のスティックか。
STICK_SIDES: Final[dict[str, str]] = {"LCLICK": "L", "RCLICK": "R"}


def stick_value(dx: float, dy: float) -> tuple[int, int]:
    """中心からのドラッグ量（画面座標、y は下向き）を 0〜255 の座標にする。

    倒し量は STICK_REACH で 1 に飽和させ、向きは保つ（斜めでも円の内側）。
    計算と丸め（int）はプレビュー上のマウス操作（CaptureArea._stickXY）と揃える。
    """
    dist = math.hypot(dx, dy)
    if dist == 0:
        return 128, 128
    mag = min(1.0, dist / STICK_REACH)
    x = int(128 + mag * 127.5 * dx / dist)
    y = int(128 + mag * 127.5 * dy / dist)
    return max(0, min(255, x)), max(0, min(255, y))


def fit_scale(
    width: float,
    height: float,
    *,
    base: float,
    max_zoom: float = MAX_ZOOM,
    pad_w: int = PAD_W,
    pad_h: int = PAD_H,
) -> float:
    """置き場所の広さに合わせた倍率。縦横比を保ち、DPI 倍率の max_zoom 倍まで。

    タブに埋め込むときは max_zoom=1（等倍より大きくしない）、別窓では
    窓の大きさに合わせて大きくできるよう max_zoom を上げて呼ぶ。
    pad_w/pad_h は操作盤の種類の論理サイズ（Joy-Con は縦長）。
    """
    scale = min(width / pad_w, height / pad_h)
    return max(MIN_SCALE, min(scale, base * max_zoom))
