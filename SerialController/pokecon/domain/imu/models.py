"""IMU 3D プレビュー用のコントローラーメッシュ（numpy のみ）。

``build_model(model_id)`` は、GPU へ渡す **float32 の Nx13 頂点列**と
同じ座標系の可動部メタデータを返す。

頂点レイアウト（13 float）::

    position.xyz, normal.xyz, color.rgb, roughness, uv.xy, motion_group

- モデル座標は x=右, y=前（上端）, z=上。3 モデルともボタン面が +Z、
  上端（ショルダー側）が +Y。
- 法線は曲面で滑らか、角では別頂点にして硬いエッジを保つ。
- デカール以外の UV は ``(-1, -1)``。デカールは +Z 向きの四角形で、
  アトラスのセル UV を持つ。
- ``motion_group`` は ``MotionGroup`` の整数値。0 は筐体・ソケットなど
  動かない面。GPU 側はこの番号で小さな剛体変換だけを当てる。
- ``build_model(model_id)`` は頂点列に加えて、各可動グループの
  ピボット・軸・押し込み量を持つ ``MotionGroupSpec`` を返す。
- 外部モデル・画像・pygame には依存しない。形状はすべて手続き生成。

形状の方針（v2）:

- 筐体の縁は「背面側は大きな R、フェイス側は小さな R」の非対称断面。
  フェイスの縁に細いハイライトが立ち、背面は手に馴染む丸みになる。
- 側面はわずかに膨らませ（樽型）、パーティングラインは色ではなく
  実際の溝として掘る。
- Pro は実寸（mm）の符号付き距離関数で本体とグリップを一体に作り、
  Surface Nets でメッシュ化する（``_PRO_GRID_MM`` で密度を調整）。
- Joy-Con は外側の角を大きく、レール側の角を小さくする。
- L/R・ZL/ZR は筐体の角に沿って曲がるスイープ形状にする。

デカールアトラスは 256px、4x4 セル（各 64px）。セル順は
``ATLAS_LABELS``。UV は数学的な下向き（v=0 が下端）で、row 0 が
アトラス上端なので v は 0.75..1.0 になる。
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from enum import IntEnum
from typing import Final

import numpy as np

Vec2 = tuple[float, float]
Vec3 = tuple[float, float, float]
Color = tuple[float, float, float]
ColorFn = Callable[[Vec3, str], Color]
RoughFn = Callable[[Vec3, str], float]

#: デカールアトラスのセル順（row 0 が上端）。
ATLAS_LABELS: Final = (
    "A",
    "B",
    "X",
    "Y",
    "+",
    "-",
    "HOME",
    "CAP",
    "SL",
    "SR",
)
ATLAS_SIZE: Final = 256
ATLAS_CELL: Final = 64
ATLAS_COLUMNS: Final = 4

_FLOATS_PER_VERTEX: Final = 13
_EPS: Final = 1.0e-9
_NO_UV: Final[Vec2] = (-1.0, -1.0)


# ====================================================================== 可動グループ


class MotionGroup(IntEnum):
    """頂点の motion_group 値。0 は固定（筐体・ソケット・レール）。"""

    BODY = 0
    A = 1
    B = 2
    X = 3
    Y = 4
    PLUS = 5
    MINUS = 6
    HOME = 7
    CAPTURE = 8
    L = 9
    R = 10
    ZL = 11
    ZR = 12
    DPAD = 13
    DPAD_UP = 14
    DPAD_DOWN = 15
    DPAD_LEFT = 16
    DPAD_RIGHT = 17
    STICK_L = 18
    STICK_R = 19
    SL = 20
    SR = 21


MOTION_GROUP_COUNT: Final = len(MotionGroup)

#: ボタンのデカール名から可動グループへ。SL/SR は棚ごとに明示する。
_BUTTON_GROUP: Final[dict[str, MotionGroup]] = {
    "A": MotionGroup.A,
    "B": MotionGroup.B,
    "X": MotionGroup.X,
    "Y": MotionGroup.Y,
    "+": MotionGroup.PLUS,
    "-": MotionGroup.MINUS,
    "HOME": MotionGroup.HOME,
    "CAP": MotionGroup.CAPTURE,
}

#: グループ → PreviewControls のボタン名。Pro の十字キーだけは dpad=True。
_GROUP_CONTROL: Final[dict[MotionGroup, str]] = {
    MotionGroup.A: "A",
    MotionGroup.B: "B",
    MotionGroup.X: "X",
    MotionGroup.Y: "Y",
    MotionGroup.PLUS: "PLUS",
    MotionGroup.MINUS: "MINUS",
    MotionGroup.HOME: "HOME",
    MotionGroup.CAPTURE: "CAPTURE",
    MotionGroup.L: "L",
    MotionGroup.R: "R",
    MotionGroup.ZL: "ZL",
    MotionGroup.ZR: "ZR",
    MotionGroup.DPAD_UP: "UP",
    MotionGroup.DPAD_DOWN: "DOWN",
    MotionGroup.DPAD_LEFT: "LEFT",
    MotionGroup.DPAD_RIGHT: "RIGHT",
    MotionGroup.SL: "SL",
    MotionGroup.SR: "SR",
}

#: スティックの最大傾き（度）。実機の可動域より控えめにして干渉を避ける。
STICK_TILT_DEG: Final = 22.0
#: Pro 十字キーの最大傾き（度）。
DPAD_TILT_DEG: Final = 8.0


@dataclass(frozen=True, slots=True)
class MotionGroupSpec:
    """1 つの可動グループの剛体変換パラメータ（最終モデル座標）。

    GPU 側は ``pivot`` を中心に ``tilt_axes[0]`` / ``tilt_axes[1]`` の
    回転を合成し、``press_axis * press_depth`` だけ平行移動する。
    ``control`` は押下・着色に使う PreviewControls のボタン名。
    ``stick`` は "left" / "right"、``dpad`` は Pro 十字キーの傾き入力。
    """

    group: MotionGroup
    pivot: Vec3
    control: str | None = None
    stick: str | None = None
    dpad: bool = False
    press_axis: Vec3 = (0.0, 0.0, -1.0)
    press_depth: float = 0.0
    tilt_axes: tuple[Vec3, Vec3] = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0))
    tilt_limit: float = 0.0


@dataclass(frozen=True, slots=True)
class ControllerModel:
    """GPU へ一度だけ渡す頂点列と、可動グループのメタデータ。"""

    vertices: np.ndarray
    groups: tuple[MotionGroupSpec, ...]


def _button_spec(
    group: MotionGroup,
    pivot: Vec3,
    *,
    press_depth: float,
    press_axis: Vec3 = (0.0, 0.0, -1.0),
) -> MotionGroupSpec:
    return MotionGroupSpec(
        group=group,
        pivot=pivot,
        control=_GROUP_CONTROL.get(group),
        press_axis=press_axis,
        press_depth=press_depth,
    )


def _stick_spec(
    group: MotionGroup,
    pivot: Vec3,
    stick: str,
    click: str,
    *,
    press_depth: float,
) -> MotionGroupSpec:
    return MotionGroupSpec(
        group=group,
        pivot=pivot,
        control=click,
        stick=stick,
        press_depth=press_depth,
        tilt_limit=math.radians(STICK_TILT_DEG),
    )


def _dpad_spec(group: MotionGroup, pivot: Vec3) -> MotionGroupSpec:
    return MotionGroupSpec(
        group=group,
        pivot=pivot,
        dpad=True,
        tilt_limit=math.radians(DPAD_TILT_DEG),
    )


# ====================================================================== 小さな数学


def _clamp(value: float, low: float, high: float) -> float:
    return low if value < low else high if value > high else value


def _clamp01(value: float) -> float:
    return _clamp(float(value), 0.0, 1.0)


def _smoothstep(edge0: float, edge1: float, value: float) -> float:
    if abs(edge1 - edge0) < _EPS:
        return 0.0 if value < edge0 else 1.0
    t = _clamp((value - edge0) / (edge1 - edge0), 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


def _mix_color(a: Color, b: Color, t: float) -> Color:
    t = _clamp01(t)
    return (
        a[0] + (b[0] - a[0]) * t,
        a[1] + (b[1] - a[1]) * t,
        a[2] + (b[2] - a[2]) * t,
    )


def _scale_color(color: Color, factor: float) -> Color:
    return (
        _clamp01(color[0] * factor),
        _clamp01(color[1] * factor),
        _clamp01(color[2] * factor),
    )


def _sub3(a: Vec3, b: Vec3) -> Vec3:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _cross3(a: Vec3, b: Vec3) -> Vec3:
    return (
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    )


def _dot3(a: Vec3, b: Vec3) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _length3(a: Vec3) -> float:
    return math.sqrt(a[0] * a[0] + a[1] * a[1] + a[2] * a[2])


def _normalize3(a: Vec3, fallback: Vec3 = (0.0, 0.0, 1.0)) -> Vec3:
    length = _length3(a)
    if length < _EPS:
        return fallback
    return (a[0] / length, a[1] / length, a[2] / length)


def _normalize2(a: tuple[float, float]) -> tuple[float, float]:
    length = math.hypot(a[0], a[1])
    if length < _EPS:
        return (0.0, 0.0)
    return (a[0] / length, a[1] / length)


def _cell_uv(label: str) -> tuple[float, float, float, float]:
    """セルの (u0, v0, u1, v1)。row 0 はアトラス上端。"""
    index = ATLAS_LABELS.index(label)
    column = index % ATLAS_COLUMNS
    row = index // ATLAS_COLUMNS
    u0 = column / ATLAS_COLUMNS
    u1 = (column + 1) / ATLAS_COLUMNS
    # 画像の row 0 が上端。数学的な v は下から数える。
    v1 = 1.0 - row / ATLAS_COLUMNS
    v0 = v1 - 1.0 / ATLAS_COLUMNS
    return (u0, v0, u1, v1)
