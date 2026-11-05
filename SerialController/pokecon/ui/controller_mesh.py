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
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
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


# ====================================================================== メッシュ


class _MeshBuilder:
    """頂点を積んで最後に float32 Nx13 へ変換する。"""

    __slots__ = ("_values", "_blocks", "_group", "_specs")

    def __init__(self) -> None:
        self._values: list[float] = []
        self._blocks: list[np.ndarray] = []
        self._group = int(MotionGroup.BODY)
        self._specs: dict[int, MotionGroupSpec] = {}

    @contextmanager
    def motion(self, spec: MotionGroupSpec) -> Iterator[None]:
        """このブロックで積む頂点を可動グループへ割り当てる。"""
        previous = self._group
        key = int(spec.group)
        existing = self._specs.get(key)
        if existing is not None and existing != spec:
            raise RuntimeError(f"可動グループ {key} の定義が重複しています")
        self._specs[key] = spec
        self._group = key
        try:
            yield
        finally:
            self._group = previous

    def specs(self) -> tuple[MotionGroupSpec, ...]:
        return tuple(self._specs[key] for key in sorted(self._specs))

    def add(
        self,
        position: Vec3,
        normal: Vec3,
        color: Color,
        roughness: float,
        uv: Vec2,
    ) -> None:
        nx, ny, nz = _normalize3(normal)
        self._values.extend(
            (
                float(position[0]),
                float(position[1]),
                float(position[2]),
                nx,
                ny,
                nz,
                _clamp01(color[0]),
                _clamp01(color[1]),
                _clamp01(color[2]),
                _clamp01(roughness),
                float(uv[0]),
                float(uv[1]),
                float(self._group),
            )
        )

    def triangle(
        self,
        p0: Vec3,
        p1: Vec3,
        p2: Vec3,
        n0: Vec3,
        n1: Vec3,
        n2: Vec3,
        c0: Color,
        c1: Color,
        c2: Color,
        r0: float,
        r1: float,
        r2: float,
        uv0: Vec2,
        uv1: Vec2,
        uv2: Vec2,
    ) -> None:
        if _length3(_cross3(_sub3(p1, p0), _sub3(p2, p0))) < 1.0e-11:
            return
        self.add(p0, n0, c0, r0, uv0)
        self.add(p1, n1, c1, r1, uv1)
        self.add(p2, n2, c2, r2, uv2)

    def oriented_triangle(
        self,
        p0: Vec3,
        p1: Vec3,
        p2: Vec3,
        n0: Vec3,
        n1: Vec3,
        n2: Vec3,
        c0: Color,
        c1: Color,
        c2: Color,
        r0: float,
        r1: float,
        r2: float,
    ) -> None:
        """頂点法線の平均に合わせて巻き方向をそろえて積む（スイープ用）。"""
        face = _cross3(_sub3(p1, p0), _sub3(p2, p0))
        average = (n0[0] + n1[0] + n2[0], n0[1] + n1[1] + n2[1], n0[2] + n1[2] + n2[2])
        if _dot3(face, average) < 0.0:
            self.triangle(
                p0, p2, p1, n0, n2, n1, c0, c2, c1, r0, r2, r1, _NO_UV, _NO_UV, _NO_UV
            )
        else:
            self.triangle(
                p0, p1, p2, n0, n1, n2, c0, c1, c2, r0, r1, r2, _NO_UV, _NO_UV, _NO_UV
            )

    def add_triangles(
        self,
        positions: np.ndarray,
        normals: np.ndarray,
        colors: np.ndarray,
        roughness: np.ndarray,
    ) -> None:
        """三角形をまとめて積む（各配列は頂点順に (3T, 3) / (3T,)）。

        UV は ``(-1, -1)``、motion_group は現在のグループ。
        """
        count = positions.shape[0]
        if count == 0:
            return
        if count % 3:
            raise RuntimeError("三角形の頂点数が 3 の倍数ではありません")
        lengths = np.linalg.norm(normals, axis=1, keepdims=True)
        unit = np.where(
            lengths > _EPS, normals / np.maximum(lengths, _EPS), (0.0, 0.0, 1.0)
        )
        block = np.empty((count, _FLOATS_PER_VERTEX), dtype=np.float32)
        block[:, 0:3] = positions
        block[:, 3:6] = unit
        block[:, 6:9] = np.clip(colors, 0.0, 1.0)
        block[:, 9] = np.clip(roughness, 0.0, 1.0)
        block[:, 10:12] = -1.0
        block[:, 12] = float(self._group)
        self._blocks.append(block)

    def build(self) -> np.ndarray:
        if len(self._values) % _FLOATS_PER_VERTEX:
            raise RuntimeError("メッシュ頂点の float 数が 13 の倍数ではありません")
        parts = list(self._blocks)
        if self._values:
            parts.append(
                np.asarray(self._values, dtype=np.float32).reshape(
                    (-1, _FLOATS_PER_VERTEX)
                )
            )
        if not parts:
            return np.zeros((0, _FLOATS_PER_VERTEX), dtype=np.float32)
        array = np.concatenate(parts, axis=0)
        if not np.all(np.isfinite(array)):
            raise RuntimeError("メッシュに非有限の値が含まれています")
        return np.ascontiguousarray(array, dtype=np.float32)


class _Transform:
    """局所座標の点と法線をワールドへ移す（平行移動＋回転）。"""

    __slots__ = ("_matrix", "_offset")

    def __init__(
        self,
        matrix: tuple[Vec3, Vec3, Vec3] = (
            (1.0, 0.0, 0.0),
            (0.0, 1.0, 0.0),
            (0.0, 0.0, 1.0),
        ),
        offset: Vec3 = (0.0, 0.0, 0.0),
    ) -> None:
        self._matrix = matrix
        self._offset = offset

    def point(self, p: Vec3) -> Vec3:
        m = self._matrix
        return (
            m[0][0] * p[0] + m[0][1] * p[1] + m[0][2] * p[2] + self._offset[0],
            m[1][0] * p[0] + m[1][1] * p[1] + m[1][2] * p[2] + self._offset[1],
            m[2][0] * p[0] + m[2][1] * p[1] + m[2][2] * p[2] + self._offset[2],
        )

    def normal(self, n: Vec3) -> Vec3:
        m = self._matrix
        return _normalize3(
            (
                m[0][0] * n[0] + m[0][1] * n[1] + m[0][2] * n[2],
                m[1][0] * n[0] + m[1][1] * n[1] + m[1][2] * n[2],
                m[2][0] * n[0] + m[2][1] * n[1] + m[2][2] * n[2],
            )
        )


def _translate(x: float, y: float, z: float) -> _Transform:
    return _Transform(offset=(x, y, z))


def _tilt_x(angle: float, offset: Vec3) -> _Transform:
    """X 軸まわりに angle だけ傾けてから offset へ移す。"""
    c = math.cos(angle)
    s = math.sin(angle)
    return _Transform(
        matrix=((1.0, 0.0, 0.0), (0.0, c, -s), (0.0, s, c)),
        offset=offset,
    )


def _frame_z_to(normal: Vec3, offset: Vec3) -> _Transform:
    """局所 +Z を normal へ向ける最小回転（Rodrigues）＋平行移動。"""
    nx, ny, nz = _normalize3(normal)
    if nz < -0.999999:
        return _Transform(
            matrix=((1.0, 0.0, 0.0), (0.0, -1.0, 0.0), (0.0, 0.0, -1.0)),
            offset=offset,
        )
    # k = z × n = (-ny, nx, 0)。R = I + K + K^2 / (1 + nz)
    kx, ky = -ny, nx
    f = 1.0 / (1.0 + nz)
    matrix = (
        (1.0 - f * ky * ky, f * kx * ky, ky),
        (f * kx * ky, 1.0 - f * kx * kx, -kx),
        (-ky, kx, 1.0 - f * (kx * kx + ky * ky)),
    )
    return _Transform(matrix=matrix, offset=offset)


# ====================================================================== 輪郭


def _outline_rounded_rect(
    half_x: float,
    half_y: float,
    radius: float,
    segments_per_corner: int = 6,
) -> list[Vec2]:
    """角丸長方形の外周を CCW で返す。"""
    return _outline_rounded_rect_corners(
        half_x, half_y, (radius, radius, radius, radius), segments_per_corner
    )


def _outline_rounded_rect_corners(
    half_x: float,
    half_y: float,
    radii: tuple[float, float, float, float],
    segments_per_corner: int = 6,
) -> list[Vec2]:
    """角ごとに R を変えた角丸長方形（右上・左上・左下・右下の順）。CCW。"""
    limit = min(half_x, half_y)
    r_tr, r_tl, r_bl, r_br = (max(0.0, min(r, limit)) for r in radii)
    corners = (
        (half_x - r_tr, half_y - r_tr, r_tr, 0.0),
        (-half_x + r_tl, half_y - r_tl, r_tl, math.pi * 0.5),
        (-half_x + r_bl, -half_y + r_bl, r_bl, math.pi),
        (half_x - r_br, -half_y + r_br, r_br, math.pi * 1.5),
    )
    steps = max(2, int(segments_per_corner))
    points: list[Vec2] = []
    for cx, cy, radius, start in corners:
        # R が大きい角ほど分割を増やし、密度をそろえる。
        local_steps = max(
            2, int(round(steps * max(0.5, radius / max(limit * 0.5, _EPS))))
        )
        local_steps = min(local_steps, steps * 3)
        for index in range(local_steps + 1):
            angle = start + (math.pi * 0.5) * (index / local_steps)
            points.append(
                (cx + radius * math.cos(angle), cy + radius * math.sin(angle))
            )
    # 長い直線辺を分割し、法線補間と色の勾配を安定させる。
    return _densify(points, max_step=0.06)


def _densify(points: Sequence[Vec2], max_step: float) -> list[Vec2]:
    """長い辺に中間点を足す（閉じた輪郭）。重複点は落とす。"""
    result: list[Vec2] = []
    count = len(points)
    for index in range(count):
        x0, y0 = points[index]
        x1, y1 = points[(index + 1) % count]
        length = math.hypot(x1 - x0, y1 - y0)
        if length < 1.0e-7:
            continue
        pieces = max(1, int(math.ceil(length / max_step)))
        for k in range(pieces):
            t = k / pieces
            result.append((x0 + (x1 - x0) * t, y0 + (y1 - y0) * t))
    return result


def _outline_normals(outline: Sequence[Vec2]) -> list[Vec2]:
    """CCW 輪郭の外向き単位法線（前後の点の差分から）。"""
    count = len(outline)
    normals: list[Vec2] = []
    for index in range(count):
        px, py = outline[index - 1]
        nx_, ny_ = outline[(index + 1) % count]
        tx = nx_ - px
        ty = ny_ - py
        normals.append(_normalize2((ty, -tx)))
    return normals


def _offset_outline(
    outline: Sequence[Vec2], normals: Sequence[Vec2], distance: float
) -> list[Vec2]:
    """法線方向へ distance だけずらす（負で内側）。"""
    return [
        (x + nx * distance, y + ny * distance)
        for (x, y), (nx, ny) in zip(outline, normals)
    ]


def _scale_about(points: Sequence[Vec2], center: Vec2, factor: float) -> list[Vec2]:
    cx, cy = center
    return [(cx + (x - cx) * factor, cy + (y - cy) * factor) for x, y in points]


def _cross_outline(
    half_length: float,
    half_width: float,
    corner: float,
    segments: int,
) -> list[Vec2]:
    """十字キーの輪郭。角丸長方形 2 本の union を放射方向で切る。"""

    def rounded_box_sdf(x: float, y: float, hx: float, hy: float, r: float) -> float:
        qx = abs(x) - (hx - r)
        qy = abs(y) - (hy - r)
        outside = math.hypot(max(qx, 0.0), max(qy, 0.0))
        inside = min(max(qx, qy), 0.0)
        return outside + inside - r

    def sdf(x: float, y: float) -> float:
        return min(
            rounded_box_sdf(x, y, half_length, half_width, corner),
            rounded_box_sdf(x, y, half_width, half_length, corner),
        )

    points: list[Vec2] = []
    for index in range(segments):
        angle = 2.0 * math.pi * index / segments
        dx = math.cos(angle)
        dy = math.sin(angle)
        low = 0.0
        high = max(half_length, half_width) * 1.4
        for _ in range(40):
            middle = (low + high) * 0.5
            if sdf(dx * middle, dy * middle) <= 0.0:
                low = middle
            else:
                high = middle
        radius = (low + high) * 0.5
        points.append((dx * radius, dy * radius))
    return points


# ====================================================================== シェル


@dataclass(frozen=True, slots=True)
class _RimStyle:
    """シェル外周の断面形状。

    ``back_radius`` は背面側の R、``face_radius`` はフェイス側の R。
    ``bulge`` は側面中央の外への膨らみ、``seam_*`` はパーティング
    ラインの溝（側面の高さ比・幅・深さ）。
    """

    back_radius: float
    face_radius: float
    bulge: float = 0.0
    seam: bool = False
    seam_fraction: float = 0.52
    seam_width: float = 0.005
    seam_depth: float = 0.004
    back_steps: int = 5
    face_steps: int = 3
    side_steps: int = 4


def _add_shell(
    mesh: _MeshBuilder,
    outline: Sequence[Vec2],
    center: Vec2,
    z_side_bottom: Callable[[float, float], float],
    z_side_top: Callable[[float, float], float],
    z_face: Callable[[float, float], float],
    z_back: Callable[[float, float], float],
    rim: _RimStyle,
    color_fn: ColorFn,
    rough_fn: RoughFn,
    *,
    bottom_scales: Sequence[float] = (0.10, 0.30, 0.55, 0.78),
    top_scales: Sequence[float] = (0.78, 0.55, 0.30, 0.10),
    transform: _Transform | None = None,
    uv: Vec2 = _NO_UV,
) -> None:
    """角丸の閉じたシェルを作る。

    輪郭は CCW。下→外側→上のリング順なので、四角形の巻き方向は
    下面で -Z、側面で外向き、上面で +Z になる。

    断面は「背面フィレット（R=back_radius）→ 樽型の側面（溝つき）→
    フェイスフィレット（R=face_radius）」。縁の内側へのずらしは
    輪郭の法線方向で行うので、凹んだ輪郭でも幅がそろう。
    """
    outline = list(outline)
    count = len(outline)
    if count < 3:
        return
    normals2d = _outline_normals(outline)
    if not _is_star_shaped(
        _offset_outline(outline, normals2d, -max(rim.back_radius, rim.face_radius)),
        center,
    ):
        # 凹部で内側へずらした輪郭の角度順が入れ替わる場合（Pro の
        # グリップ付け根など）は、中心からの放射方向でずらす。縁の幅は
        # 少し不ぞろいになるが、帯が折れ返らない。
        normals2d = [_normalize2((x - center[0], y - center[1])) for x, y in outline]

    zb = [float(z_side_bottom(x, y)) for x, y in outline]
    zt = [float(z_side_top(x, y)) for x, y in outline]
    min_height = min(zt[i] - zb[i] for i in range(count))
    if min_height <= 1.0e-5:
        return
    back_radius = rim.back_radius
    face_radius = rim.face_radius
    # 薄い小物でも上下の R が交差しないように縮める。
    total = back_radius + face_radius
    if total > min_height * 0.92:
        factor = min_height * 0.92 / max(total, _EPS)
        back_radius *= factor
        face_radius *= factor
    back_radius = max(back_radius, 0.0005)
    face_radius = max(face_radius, 0.0005)

    rings: list[list[Vec3]] = []
    ring_colors: list[list[Color]] = []
    ring_roughs: list[list[float]] = []

    def append_ring(points3d: Sequence[Vec3], role: str) -> None:
        colors: list[Color] = []
        roughs: list[float] = []
        for position in points3d:
            colors.append(color_fn(position, role))
            roughs.append(rough_fn(position, role))
        rings.append(list(points3d))
        ring_colors.append(colors)
        ring_roughs.append(roughs)

    def rim_ring(offset: float, z_of: Callable[[int], float]) -> list[Vec3]:
        return [
            (
                outline[i][0] + normals2d[i][0] * offset,
                outline[i][1] + normals2d[i][1] * offset,
                z_of(i),
            )
            for i in range(count)
        ]

    back_inset = _offset_outline(outline, normals2d, -back_radius)
    face_inset = _offset_outline(outline, normals2d, -face_radius)
    # 内側のリングは縁のリングを中心へ縮めて作る。凹部で角度順が
    # 入れ替わる輪郭（Pro の付け根など）は、同じ中心距離を元の輪郭の
    # 放射方向に取り直してから縮める（帯が折れないように）。
    back_core = _star_safe(outline, back_inset, center)
    face_core = _star_safe(outline, face_inset, center)

    # 下面（内側→外側）。リング順が逆なので巻き方向は -Z になる。
    for factor in bottom_scales:
        points2d = _scale_about(back_core, center, factor)
        append_ring(
            [(x, y, float(z_back(x, y))) for x, y in points2d],
            "back",
        )

    # 背面フィレット（四分円）。a=0 が下面側、a=pi/2 が側面側。
    back_steps = max(2, rim.back_steps)
    for k in range(back_steps + 1):
        a = (math.pi * 0.5) * k / back_steps
        offset = -back_radius * (1.0 - math.sin(a))
        lift = back_radius * (1.0 - math.cos(a))
        role = "back" if k < back_steps * 0.5 else "side"
        append_ring(rim_ring(offset, lambda i: zb[i] + lift), role)

    # 側面（下から上）。樽型の膨らみと、パーティングラインの溝。
    def side_offset(s: float) -> float:
        return rim.bulge * math.sin(math.pi * _clamp01(s))

    def side_z(i: int, s: float) -> float:
        low = zb[i] + back_radius
        high = zt[i] - face_radius
        return low + (high - low) * s

    span_ok = (
        min(zt[i] - zb[i] - back_radius - face_radius for i in range(count)) > 0.03
    )
    side_samples: list[tuple[float, float, str]] = []
    steps = max(2, rim.side_steps)
    for k in range(1, steps):
        side_samples.append((k / steps, 0.0, "side"))
    if rim.seam and span_ok:
        min_span = min(zt[i] - zb[i] - back_radius - face_radius for i in range(count))
        width = rim.seam_width / max(min_span, _EPS)
        lip = width * 0.45
        f = rim.seam_fraction
        side_samples = [
            s for s in side_samples if abs(s[0] - (f + width * 0.5)) > width * 2.5
        ]
        side_samples += [
            (f - lip, 0.0, "side"),
            (f, -rim.seam_depth, "seam"),
            (f + width, -rim.seam_depth, "seam"),
            (f + width + lip, 0.0, "side"),
        ]
        side_samples.sort(key=lambda item: item[0])
    for s, extra, role in side_samples:
        append_ring(
            rim_ring(side_offset(s) + extra, lambda i: side_z(i, s)),
            role,
        )

    # フェイスフィレット（四分円）。a=0 が側面側、a=pi/2 が上面側。
    face_steps = max(2, rim.face_steps)
    for k in range(face_steps + 1):
        a = (math.pi * 0.5) * k / face_steps
        offset = -face_radius * (1.0 - math.cos(a))
        drop = face_radius * (1.0 - math.sin(a))
        role = "face" if k == face_steps else "side"
        append_ring(rim_ring(offset, lambda i: zt[i] - drop), role)

    # 上面（外側→内側）。
    for factor in top_scales:
        points2d = _scale_about(face_core, center, factor)
        append_ring(
            [(x, y, float(z_face(x, y))) for x, y in points2d],
            "face",
        )

    _emit_ring_shell(
        mesh,
        rings,
        ring_colors,
        ring_roughs,
        center,
        z_face,
        z_back,
        color_fn,
        rough_fn,
        transform,
        uv,
    )


def _radial_match(
    outline: Sequence[Vec2], inset: Sequence[Vec2], center: Vec2
) -> list[Vec2]:
    """inset と同じ中心距離を、元の輪郭点の放射方向に取り直す。"""
    cx, cy = center
    result: list[Vec2] = []
    for (ox, oy), (ix, iy) in zip(outline, inset):
        dx = ox - cx
        dy = oy - cy
        length = math.hypot(dx, dy)
        if length < _EPS:
            result.append((ox, oy))
            continue
        factor = max(0.02, math.hypot(ix - cx, iy - cy) / length)
        result.append((cx + dx * factor, cy + dy * factor))
    return result


def _star_safe(
    outline: Sequence[Vec2], inset: Sequence[Vec2], center: Vec2
) -> list[Vec2]:
    if _is_star_shaped(inset, center):
        return list(inset)
    return _radial_match(outline, inset, center)


def _is_star_shaped(points: Sequence[Vec2], center: Vec2) -> bool:
    """center から見て点の角度が CCW に単調に進むか。"""
    cx, cy = center
    total = 0.0
    count = len(points)
    for index in range(count):
        x0, y0 = points[index]
        x1, y1 = points[(index + 1) % count]
        a0 = math.atan2(y0 - cy, x0 - cx)
        a1 = math.atan2(y1 - cy, x1 - cx)
        delta = (a1 - a0 + math.pi) % (2.0 * math.pi) - math.pi
        if delta <= 0.0:
            return False
        total += delta
    return abs(total - 2.0 * math.pi) < 1.0e-3


def _emit_ring_shell(
    mesh: _MeshBuilder,
    rings: list[list[Vec3]],
    ring_colors: list[list[Color]],
    ring_roughs: list[list[float]],
    center: Vec2,
    z_face: Callable[[float, float], float],
    z_back: Callable[[float, float], float],
    color_fn: ColorFn,
    rough_fn: RoughFn,
    transform: _Transform | None,
    uv: Vec2,
) -> None:
    """リング列を下面ファン・帯・上面ファンとして三角形化する。"""
    count = len(rings[0])
    bottom_center = (
        float(center[0]),
        float(center[1]),
        float(z_back(center[0], center[1])),
    )
    top_center = (
        float(center[0]),
        float(center[1]),
        float(z_face(center[0], center[1])),
    )
    bottom_color = color_fn(bottom_center, "back")
    bottom_rough = rough_fn(bottom_center, "back")
    top_color = color_fn(top_center, "face")
    top_rough = rough_fn(top_center, "face")

    # 頂点法線を面法線の平均で求める（numpy でまとめて計算）。
    grid = np.asarray(rings, dtype=np.float64)  # (R, C, 3)
    nxt = np.roll(np.arange(count), -1)
    a = grid[:-1, :, :]
    b = grid[:-1, nxt, :]
    c = grid[1:, nxt, :]
    d = grid[1:, :, :]
    first = np.cross(b - a, c - a)
    second = np.cross(c - a, d - a)
    acc = np.zeros_like(grid)
    acc[:-1, :, :] += first + second
    acc[:-1, nxt, :] += first
    acc[1:, nxt, :] += first + second
    acc[1:, :, :] += second

    bc = np.asarray(bottom_center)
    tc = np.asarray(top_center)
    ring0 = grid[0]
    cap_b = np.cross(ring0[nxt] - bc, ring0 - bc)
    acc[0] += cap_b
    acc[0, nxt] += cap_b
    ring_last = grid[-1]
    cap_t = np.cross(ring_last - tc, ring_last[nxt] - tc)
    acc[-1] += cap_t
    acc[-1, nxt] += cap_t

    lengths = np.linalg.norm(acc, axis=2, keepdims=True)
    unit = np.where(
        lengths > _EPS, acc / np.maximum(lengths, _EPS), np.array([0.0, 0.0, 1.0])
    )
    unit_normals = [
        [(float(n[0]), float(n[1]), float(n[2])) for n in row] for row in unit
    ]
    bottom_sum = cap_b.sum(axis=0)
    top_sum = cap_t.sum(axis=0)
    unit_bottom = _normalize3(
        (float(bottom_sum[0]), float(bottom_sum[1]), float(bottom_sum[2])),
        (0.0, 0.0, -1.0),
    )
    unit_top = _normalize3(
        (float(top_sum[0]), float(top_sum[1]), float(top_sum[2])),
        (0.0, 0.0, 1.0),
    )

    def point(p: Vec3) -> Vec3:
        return transform.point(p) if transform is not None else p

    def normal(n: Vec3) -> Vec3:
        return transform.normal(n) if transform is not None else n

    first_ring = rings[0]
    last_ring = rings[-1]
    for index in range(count):
        nx_ = (index + 1) % count
        mesh.triangle(
            point(bottom_center),
            point(first_ring[nx_]),
            point(first_ring[index]),
            normal(unit_bottom),
            normal(unit_normals[0][nx_]),
            normal(unit_normals[0][index]),
            bottom_color,
            ring_colors[0][nx_],
            ring_colors[0][index],
            bottom_rough,
            ring_roughs[0][nx_],
            ring_roughs[0][index],
            uv,
            uv,
            uv,
        )

    for band in range(len(rings) - 1):
        lower = rings[band]
        upper = rings[band + 1]
        ln = unit_normals[band]
        un = unit_normals[band + 1]
        lc = ring_colors[band]
        uc = ring_colors[band + 1]
        lr = ring_roughs[band]
        ur = ring_roughs[band + 1]
        for index in range(count):
            nx_ = (index + 1) % count
            mesh.triangle(
                point(lower[index]),
                point(lower[nx_]),
                point(upper[nx_]),
                normal(ln[index]),
                normal(ln[nx_]),
                normal(un[nx_]),
                lc[index],
                lc[nx_],
                uc[nx_],
                lr[index],
                lr[nx_],
                ur[nx_],
                uv,
                uv,
                uv,
            )
            mesh.triangle(
                point(lower[index]),
                point(upper[nx_]),
                point(upper[index]),
                normal(ln[index]),
                normal(un[nx_]),
                normal(un[index]),
                lc[index],
                uc[nx_],
                uc[index],
                lr[index],
                ur[nx_],
                ur[index],
                uv,
                uv,
                uv,
            )

    for index in range(count):
        nx_ = (index + 1) % count
        mesh.triangle(
            point(top_center),
            point(last_ring[index]),
            point(last_ring[nx_]),
            normal(unit_top),
            normal(unit_normals[-1][index]),
            normal(unit_normals[-1][nx_]),
            top_color,
            ring_colors[-1][index],
            ring_colors[-1][nx_],
            top_rough,
            ring_roughs[-1][index],
            ring_roughs[-1][nx_],
            uv,
            uv,
            uv,
        )


# ====================================================================== スイープ


def _add_sweep(
    mesh: _MeshBuilder,
    path: Sequence[Vec2],
    path_normals: Sequence[Vec2],
    z_center: Callable[[float], float],
    width: float,
    height: float,
    radius: float,
    color_fn: ColorFn,
    rough_fn: RoughFn,
    *,
    taper: float = 0.18,
    profile_segments: int = 4,
) -> None:
    """角丸長方形の断面を 2D パスに沿って掃引する（L/R・ZL/ZR 用）。

    断面は (外向き u, 高さ v) 平面の角丸長方形。``z_center(t)`` は
    パス位置 t∈[0,1] での断面中心の高さ。両端 ``taper`` 区間で
    断面を丸く絞り、端面は小さなファンで閉じる。
    """
    profile = _outline_rounded_rect(width * 0.5, height * 0.5, radius, profile_segments)
    profile_normals = _outline_normals(profile)
    count = len(path)
    if count < 2:
        return

    def frame_point(k: int, u: float, v: float) -> Vec3:
        t = k / (count - 1)
        px, py = path[k]
        nx_, ny_ = path_normals[k]
        return (px + nx_ * u, py + ny_ * u, z_center(t) + v)

    def end_scale(k: int) -> float:
        t = k / (count - 1)
        edge = min(t, 1.0 - t)
        if taper <= 0.0 or edge >= taper:
            return 1.0
        x = edge / taper
        return 0.55 + 0.45 * math.sqrt(max(0.0, 1.0 - (1.0 - x) ** 2))

    sections: list[list[Vec3]] = []
    section_normals: list[list[Vec3]] = []
    for k in range(count):
        scale = end_scale(k)
        nx_, ny_ = path_normals[k]
        pts: list[Vec3] = []
        nrm: list[Vec3] = []
        for (u, v), (nu, nv) in zip(profile, profile_normals):
            pts.append(frame_point(k, u * scale, v * scale))
            nrm.append(_normalize3((nx_ * nu, ny_ * nu, nv)))
        sections.append(pts)
        section_normals.append(nrm)

    def role_of(n: Vec3) -> str:
        if n[2] > 0.55:
            return "face"
        if n[2] < -0.55:
            return "back"
        return "side"

    columns = len(profile)
    for k in range(count - 1):
        for j in range(columns):
            jn = (j + 1) % columns
            quad = (
                (sections[k][j], section_normals[k][j]),
                (sections[k][jn], section_normals[k][jn]),
                (sections[k + 1][jn], section_normals[k + 1][jn]),
                (sections[k + 1][j], section_normals[k + 1][j]),
            )
            cols = [color_fn(p, role_of(n)) for p, n in quad]
            rgh = [rough_fn(p, role_of(n)) for p, n in quad]
            for i0, i1, i2 in ((0, 1, 2), (0, 2, 3)):
                mesh.oriented_triangle(
                    quad[i0][0],
                    quad[i1][0],
                    quad[i2][0],
                    quad[i0][1],
                    quad[i1][1],
                    quad[i2][1],
                    cols[i0],
                    cols[i1],
                    cols[i2],
                    rgh[i0],
                    rgh[i1],
                    rgh[i2],
                )

    # 端面。パスの接線方向を法線にする。
    for k in (0, count - 1):
        other = 1 if k == 0 else count - 2
        tx = path[k][0] - path[other][0]
        ty = path[k][1] - path[other][1]
        cap_normal = _normalize3((tx, ty, 0.0))
        ring = sections[k]
        cx = sum(p[0] for p in ring) / columns
        cy = sum(p[1] for p in ring) / columns
        cz = sum(p[2] for p in ring) / columns
        cap_center = (cx, cy, cz)
        color = color_fn(cap_center, "side")
        rough = rough_fn(cap_center, "side")
        for j in range(columns):
            jn = (j + 1) % columns
            # 端面の縁は側面法線と混ぜて、角を少し丸く見せる。
            n0 = _normalize3(
                (
                    cap_normal[0] * 0.7 + section_normals[k][j][0] * 0.3,
                    cap_normal[1] * 0.7 + section_normals[k][j][1] * 0.3,
                    cap_normal[2] * 0.7 + section_normals[k][j][2] * 0.3,
                )
            )
            n1 = _normalize3(
                (
                    cap_normal[0] * 0.7 + section_normals[k][jn][0] * 0.3,
                    cap_normal[1] * 0.7 + section_normals[k][jn][1] * 0.3,
                    cap_normal[2] * 0.7 + section_normals[k][jn][2] * 0.3,
                )
            )
            mesh.oriented_triangle(
                cap_center,
                ring[j],
                ring[jn],
                cap_normal,
                n0,
                n1,
                color,
                color,
                color,
                rough,
                rough,
                rough,
            )


def _outline_path(
    outline: Sequence[Vec2],
    keep: Callable[[float, float], bool],
    offset: float,
    *,
    smooth: int = 2,
) -> tuple[list[Vec2], list[Vec2]]:
    """輪郭の一部（keep が真の連続区間）を外へ offset したパス。"""
    count = len(outline)
    normals = _outline_normals(outline)
    flags = [keep(x, y) for x, y in outline]
    if all(flags) or not any(flags):
        raise ValueError("輪郭の部分区間を特定できません")
    start = next(i for i in range(count) if flags[i] and not flags[i - 1])
    indices: list[int] = []
    i = start
    while flags[i]:
        indices.append(i)
        i = (i + 1) % count
    # 法線を少しならし、パスのうねりを抑える。
    smoothed: list[Vec2] = []
    for idx in indices:
        sx = 0.0
        sy = 0.0
        for d in range(-smooth, smooth + 1):
            n = normals[(idx + d) % count]
            sx += n[0]
            sy += n[1]
        smoothed.append(_normalize2((sx, sy)))
    path = [
        (outline[idx][0] + n[0] * offset, outline[idx][1] + n[1] * offset)
        for idx, n in zip(indices, smoothed)
    ]
    return path, smoothed


# ====================================================================== 旋盤・小物


def _add_lathe(
    mesh: _MeshBuilder,
    center: Vec2,
    profile: Sequence[tuple[float, float]],
    segments: int,
    colors: Sequence[Color],
    roughs: Sequence[float],
    *,
    closed_profile: bool = False,
    transform: _Transform | None = None,
    uv: Vec2 = _NO_UV,
) -> None:
    """(半径, z) のプロファイルを Z 軸まわりに回す。

    プロファイルは下から外側を通って上へ進む向き。法線は
    ``(dz, -dr)`` から解析的に求める。
    """
    if len(profile) < 2 or segments < 3:
        return
    if len(colors) != len(profile) or len(roughs) != len(profile):
        raise ValueError("旋盤プロファイルの色・粗さの数が一致しません")

    def point(p: Vec3) -> Vec3:
        return transform.point(p) if transform is not None else p

    def normal(n: Vec3) -> Vec3:
        return transform.normal(n) if transform is not None else n

    count = len(profile)
    segment_normals: list[tuple[float, float]] = []
    segment_count = count if closed_profile else count - 1
    for index in range(segment_count):
        r0, z0 = profile[index]
        r1, z1 = profile[(index + 1) % count]
        segment_normals.append(_normalize2((z1 - z0, -(r1 - r0))))

    vertex_normals: list[tuple[float, float]] = []
    for index in range(count):
        if closed_profile:
            previous = segment_normals[(index - 1) % segment_count]
            following = segment_normals[index % segment_count]
            nr = previous[0] + following[0]
            nz = previous[1] + following[1]
        elif index == 0:
            nr, nz = segment_normals[0]
        elif index == count - 1:
            nr, nz = segment_normals[-1]
        else:
            previous = segment_normals[index - 1]
            following = segment_normals[index]
            nr = previous[0] + following[0]
            nz = previous[1] + following[1]
        vertex_normals.append(_normalize2((nr, nz)))

    rings: list[list[Vec3] | None] = []
    ring_normals: list[list[Vec3] | None] = []
    for (radius, z), (nr, nz) in zip(profile, vertex_normals):
        if abs(radius) < 1.0e-5:
            rings.append(None)
            ring_normals.append(None)
            continue
        points: list[Vec3] = []
        normals: list[Vec3] = []
        for index in range(segments):
            angle = 2.0 * math.pi * index / segments
            c = math.cos(angle)
            s = math.sin(angle)
            points.append((center[0] + radius * c, center[1] + radius * s, z))
            normals.append(_normalize3((nr * c, nr * s, nz)))
        rings.append(points)
        ring_normals.append(normals)

    def center_point(index: int) -> Vec3:
        _, z = profile[index]
        return (center[0], center[1], z)

    def center_normal(index: int) -> Vec3:
        _, nz = vertex_normals[index]
        return (0.0, 0.0, 1.0 if nz >= 0.0 else -1.0)

    for index in range(segment_count):
        next_index = (index + 1) % count
        first = rings[index]
        second = rings[next_index]
        if first is None and second is None:
            continue
        if first is None:
            # 中心→リング: 下面キャップ。
            cap_center = center_point(index)
            second_normals = ring_normals[next_index]
            assert second is not None and second_normals is not None
            for j in range(segments):
                jn = (j + 1) % segments
                mesh.triangle(
                    point(cap_center),
                    point(second[jn]),
                    point(second[j]),
                    normal(center_normal(index)),
                    normal(second_normals[jn]),
                    normal(second_normals[j]),
                    colors[index],
                    colors[next_index],
                    colors[next_index],
                    roughs[index],
                    roughs[next_index],
                    roughs[next_index],
                    uv,
                    uv,
                    uv,
                )
            continue
        if second is None:
            # リング→中心: 上面キャップ。
            cap_center = center_point(next_index)
            first_normals = ring_normals[index]
            assert first_normals is not None
            for j in range(segments):
                jn = (j + 1) % segments
                mesh.triangle(
                    point(cap_center),
                    point(first[j]),
                    point(first[jn]),
                    normal(center_normal(next_index)),
                    normal(first_normals[j]),
                    normal(first_normals[jn]),
                    colors[next_index],
                    colors[index],
                    colors[index],
                    roughs[next_index],
                    roughs[index],
                    roughs[index],
                    uv,
                    uv,
                    uv,
                )
            continue

        first_normals = ring_normals[index]
        second_normals = ring_normals[next_index]
        assert first_normals is not None and second_normals is not None
        for j in range(segments):
            jn = (j + 1) % segments
            mesh.triangle(
                point(first[j]),
                point(first[jn]),
                point(second[jn]),
                normal(first_normals[j]),
                normal(first_normals[jn]),
                normal(second_normals[jn]),
                colors[index],
                colors[index],
                colors[next_index],
                roughs[index],
                roughs[index],
                roughs[next_index],
                uv,
                uv,
                uv,
            )
            mesh.triangle(
                point(first[j]),
                point(second[jn]),
                point(second[j]),
                normal(first_normals[j]),
                normal(second_normals[jn]),
                normal(second_normals[j]),
                colors[index],
                colors[next_index],
                colors[next_index],
                roughs[index],
                roughs[next_index],
                roughs[next_index],
                uv,
                uv,
                uv,
            )


def _add_torus(
    mesh: _MeshBuilder,
    center: Vec2,
    major_radius: float,
    minor_radius: float,
    z_center: float,
    color: Color,
    roughness: float,
    *,
    segments: int = 32,
    tube_segments: int = 8,
    transform: _Transform | None = None,
) -> None:
    profile: list[tuple[float, float]] = []
    for index in range(tube_segments):
        angle = 2.0 * math.pi * index / tube_segments
        profile.append(
            (
                major_radius + minor_radius * math.cos(angle),
                z_center + minor_radius * math.sin(angle),
            )
        )
    _add_lathe(
        mesh,
        center,
        profile,
        segments,
        [color] * tube_segments,
        [roughness] * tube_segments,
        closed_profile=True,
        transform=transform,
    )


def _add_disk(
    mesh: _MeshBuilder,
    center: Vec2,
    radius: float,
    z: float,
    color: Color,
    roughness: float,
    *,
    normal_z: float = 1.0,
    segments: int = 28,
    transform: _Transform | None = None,
) -> None:
    def point(p: Vec3) -> Vec3:
        return transform.point(p) if transform is not None else p

    def normal(n: Vec3) -> Vec3:
        return transform.normal(n) if transform is not None else n

    center_point = (center[0], center[1], z)
    normal_value = (0.0, 0.0, 1.0 if normal_z >= 0.0 else -1.0)
    for index in range(segments):
        angle0 = 2.0 * math.pi * index / segments
        angle1 = 2.0 * math.pi * (index + 1) / segments
        p0 = (
            center[0] + radius * math.cos(angle0),
            center[1] + radius * math.sin(angle0),
            z,
        )
        p1 = (
            center[0] + radius * math.cos(angle1),
            center[1] + radius * math.sin(angle1),
            z,
        )
        a, b = (p0, p1) if normal_z >= 0.0 else (p1, p0)
        mesh.triangle(
            point(center_point),
            point(a),
            point(b),
            normal(normal_value),
            normal(normal_value),
            normal(normal_value),
            color,
            color,
            color,
            roughness,
            roughness,
            roughness,
            _NO_UV,
            _NO_UV,
            _NO_UV,
        )


def _add_decal(
    mesh: _MeshBuilder,
    center: Vec2,
    size: float,
    label: str,
    z: float,
    *,
    base_color: Color = (0.0, 0.0, 0.0),
    roughness: float = 0.38,
    transform: _Transform | None = None,
) -> None:
    u0, v0, u1, v1 = _cell_uv(label)
    half = size * 0.5
    p0 = (center[0] - half, center[1] - half, z)
    p1 = (center[0] + half, center[1] - half, z)
    p2 = (center[0] + half, center[1] + half, z)
    p3 = (center[0] - half, center[1] + half, z)
    normal = (0.0, 0.0, 1.0)
    if transform is not None:
        p0, p1, p2, p3 = (transform.point(p) for p in (p0, p1, p2, p3))
        normal = transform.normal(normal)
    c = base_color
    r = roughness
    mesh.triangle(
        p0,
        p1,
        p2,
        normal,
        normal,
        normal,
        c,
        c,
        c,
        r,
        r,
        r,
        (u0, v0),
        (u1, v0),
        (u1, v1),
    )
    mesh.triangle(
        p0,
        p2,
        p3,
        normal,
        normal,
        normal,
        c,
        c,
        c,
        r,
        r,
        r,
        (u0, v0),
        (u1, v1),
        (u0, v1),
    )


def _add_rounded_prism(
    mesh: _MeshBuilder,
    outline: Sequence[Vec2],
    center: Vec2,
    z_bottom: float,
    z_top: float,
    bevel: float,
    color_fn: ColorFn,
    rough_fn: RoughFn,
    *,
    face_bevel: float | None = None,
    transform: _Transform | None = None,
) -> None:
    # 小物の輪郭は原点基準。リングを縮める中心と同じ座標へ移してから渡す。
    outline = [(x + center[0], y + center[1]) for x, y in outline]
    _add_shell(
        mesh,
        outline,
        center,
        lambda _x, _y: z_bottom,
        lambda _x, _y: z_top,
        lambda _x, _y: z_top,
        lambda _x, _y: z_bottom,
        _RimStyle(
            back_radius=bevel,
            face_radius=bevel if face_bevel is None else face_bevel,
            back_steps=2,
            face_steps=2,
            side_steps=2,
        ),
        color_fn,
        rough_fn,
        bottom_scales=(0.5,),
        top_scales=(0.5,),
        transform=transform,
    )


# ====================================================================== 共通パーツ（ボタン・スティック）


_PRO_BUTTON_SIDE: Final = (0.050, 0.053, 0.060)
_PRO_BUTTON_TOP: Final = (0.020, 0.021, 0.025)
_PRO_BEZEL: Final = (0.010, 0.011, 0.013)
_PRO_HOLE: Final = (0.004, 0.004, 0.005)
_PRO_RUBBER: Final = (0.040, 0.042, 0.048)
_PRO_RUBBER_LIP: Final = (0.085, 0.090, 0.100)
_PRO_RUBBER_DARK: Final = (0.022, 0.023, 0.027)
_PRO_STICK_STEM: Final = (0.65, 0.68, 0.72)
_PRO_DPAD_SIDE: Final = (0.040, 0.043, 0.049)
_PRO_DPAD_TOP: Final = (0.070, 0.074, 0.083)
_PRO_ACCENT: Final = (0.300, 0.720, 0.960)


def _add_stick(
    mesh: _MeshBuilder,
    cx: float,
    cy: float,
    face_z: float,
    *,
    group: MotionGroup,
    stick: str,
    click: str,
    scale: float = 1.0,
    tilt: float = 0.0,
    press_depth: float = 0.012,
    lip_color: Color = _PRO_RUBBER_LIP,
    normal: Vec3 | None = None,
    well: bool = True,
    stem_lift: float = 0.0,
) -> None:
    """スティック。``normal`` を渡すとその向きに立てる（tilt は無視）。

    ``well=False`` は筐体側に穴を掘ってある場合（Pro）で、掘り込みの
    円盤とリングを作らない。``stem_lift`` は首を伸ばしてキャップを
    高くする量。
    """
    well_radius = 0.178 * scale
    origin = (cx, cy, face_z)
    transform = (
        _frame_z_to(normal, origin) if normal is not None else _tilt_x(tilt, origin)
    )
    if well:
        _add_stick_well(mesh, well_radius, scale, transform)

    s = scale
    spec = replace(
        _stick_spec(group, origin, stick, click, press_depth=press_depth),
        press_axis=transform.normal((0.0, 0.0, -1.0)),
        tilt_axes=(
            transform.normal((1.0, 0.0, 0.0)),
            transform.normal((0.0, 1.0, 0.0)),
        ),
    )
    with mesh.motion(spec):
        _add_stick_cap(mesh, s, stem_lift, lip_color, transform)


def _add_stick_well(
    mesh: _MeshBuilder, well_radius: float, scale: float, transform: _Transform
) -> None:
    # 掘り込み: 暗い穴＋縁の面取りリング。
    _add_disk(
        mesh,
        (0.0, 0.0),
        well_radius,
        0.0040,
        _PRO_HOLE,
        0.70,
        normal_z=1.0,
        segments=48,
        transform=transform,
    )
    _add_lathe(
        mesh,
        (0.0, 0.0),
        [
            (well_radius + 0.014 * scale, 0.0010),
            (well_radius + 0.006 * scale, 0.0060),
            (well_radius + 0.000 * scale, 0.0050),
            (well_radius - 0.004 * scale, 0.0000),
        ],
        48,
        [_PRO_BEZEL, (0.060, 0.064, 0.072), _PRO_BEZEL, _PRO_HOLE],
        [0.40, 0.30, 0.45, 0.70],
        transform=transform,
    )


def _add_stick_cap(
    mesh: _MeshBuilder,
    s: float,
    stem_lift: float,
    lip_color: Color,
    transform: _Transform,
) -> None:
    # 首の根元のドーム（傾けたときに見える部分）。
    _add_lathe(
        mesh,
        (0.0, 0.0),
        [
            (0.125 * s, 0.002),
            (0.112 * s, 0.020 * s),
            (0.085 * s, 0.034 * s),
            (0.050 * s, 0.040 * s),
            (0.000, 0.042 * s),
        ],
        40,
        [_PRO_STICK_STEM] * 5,
        [0.45, 0.40, 0.38, 0.38, 0.38],
        transform=transform,
    )
    # キャップ: 細い首→外へ張り出す肩→ローレット風の縁→浅い皿。
    profile = [
        (0.000, 0.030),
        (0.046, 0.030),
        (0.044, 0.060),
        (0.060, 0.070),
        (0.112, 0.076),
        (0.142, 0.084),
        (0.156, 0.094),
        (0.160, 0.104),
        (0.157, 0.114),
        (0.146, 0.121),
        (0.128, 0.122),
        (0.110, 0.117),
        (0.080, 0.111),
        (0.040, 0.108),
        (0.000, 0.107),
    ]
    profile = [
        (r * s, z * s + (stem_lift if index >= 2 else 0.0))
        for index, (r, z) in enumerate(profile)
    ]
    dark = _PRO_RUBBER_DARK
    rubber = _PRO_RUBBER
    colors: list[Color] = [
        _PRO_STICK_STEM,
        _PRO_STICK_STEM,
        _PRO_STICK_STEM,
        # キャップ下面の丸い肩も軸と同色にし、黒い指当てと区別する。
        _PRO_STICK_STEM,
        _PRO_STICK_STEM,
        _PRO_STICK_STEM,
        _PRO_STICK_STEM,
        lip_color,
        lip_color,
        lip_color,
        rubber,
        dark,
        dark,
        dark,
        dark,
    ]
    roughs = [
        0.40,
        0.40,
        0.40,
        0.82,
        0.82,
        0.80,
        0.70,
        0.55,
        0.55,
        0.60,
        0.80,
        0.92,
        0.94,
        0.95,
        0.95,
    ]
    _add_lathe(mesh, (0.0, 0.0), profile, 48, colors, roughs, transform=transform)
    # 皿の外周に細い溝を 1 本（指の当たり面の縁取り）。
    _add_torus(
        mesh,
        (0.0, 0.0),
        0.118 * s,
        0.0030 * s,
        0.1150 * s + stem_lift,
        (0.012, 0.013, 0.015),
        0.90,
        segments=48,
        tube_segments=6,
        transform=transform,
    )


def _add_round_button(
    mesh: _MeshBuilder,
    cx: float,
    cy: float,
    face_z: float,
    radius: float,
    *,
    group: MotionGroup | None = None,
    label: str | None = None,
    decal_size: float = 0.0,
    top_color: Color = _PRO_BUTTON_TOP,
    side_color: Color = _PRO_BUTTON_SIDE,
    height: float = 0.030,
    segments: int = 40,
    press_depth: float = 0.010,
    tilt: float = 0.0,
    normal: Vec3 | None = None,
) -> None:
    """丸ボタン。フェイスに開いた穴（暗い隙間＋面取りの縁）と凸の天面。

    ``normal`` を渡すと面の法線に沿って立て、押し込みも法線方向にする。
    """
    origin = (cx, cy, face_z)
    transform = (
        _frame_z_to(normal, origin) if normal is not None else _tilt_x(tilt, origin)
    )
    press_axis = transform.normal((0.0, 0.0, -1.0))
    gap = 0.006
    # 穴の縁（面取り）と、隙間の暗い底。
    _add_lathe(
        mesh,
        (0.0, 0.0),
        [
            (radius + gap + 0.010, 0.0012),
            (radius + gap + 0.003, 0.0040),
            (radius + gap, 0.0010),
            (radius + gap - 0.001, -0.004),
        ],
        segments,
        [_PRO_BEZEL, (0.050, 0.053, 0.060), _PRO_BEZEL, _PRO_HOLE],
        [0.45, 0.32, 0.50, 0.80],
        transform=transform,
    )
    _add_disk(
        mesh,
        (0.0, 0.0),
        radius + gap,
        -0.0035,
        _PRO_HOLE,
        0.85,
        segments=segments,
        transform=transform,
    )
    base = -0.006
    dome = min(0.006, radius * 0.08)
    profile: list[tuple[float, float]] = [
        (0.0, base),
        (radius * 0.94, base),
        (radius, base + 0.006),
        (radius * 1.004, height * 0.62),
        (radius * 0.975, height * 0.88),
        (radius * 0.90, height),
        (radius * 0.62, height + dome * 0.70),
        (radius * 0.30, height + dome * 0.95),
        (0.0, height + dome),
    ]
    k = (0.008, 0.009, 0.010)
    colors: list[Color] = [
        k,
        k,
        side_color,
        side_color,
        side_color,
        top_color,
        top_color,
        top_color,
        top_color,
    ]
    roughs = [0.80, 0.80, 0.50, 0.46, 0.40, 0.28, 0.26, 0.26, 0.26]
    resolved = group
    if resolved is None and label is not None:
        resolved = _BUTTON_GROUP.get(label)

    def emit() -> None:
        _add_lathe(
            mesh, (0.0, 0.0), profile, segments, colors, roughs, transform=transform
        )
        if label is not None and decal_size > 0.0:
            _add_decal(
                mesh,
                (0.0, 0.0),
                decal_size,
                label,
                height + dome + 0.0012,
                transform=transform,
            )

    if resolved is None:
        emit()
        return
    pivot = transform.point((0.0, 0.0, height))
    with mesh.motion(
        _button_spec(resolved, pivot, press_depth=press_depth, press_axis=press_axis)
    ):
        emit()


def _add_capture_button(
    mesh: _MeshBuilder,
    cx: float,
    cy: float,
    face_z: float,
    *,
    half: float = 0.044,
    height: float = 0.022,
    top_color: Color = _PRO_BUTTON_TOP,
    side_color: Color = _PRO_BUTTON_SIDE,
    decal_size: float = 0.052,
    press_depth: float = 0.010,
    normal: Vec3 | None = None,
) -> None:
    """角丸四角のキャプチャボタン（穴の縁＋本体）。"""
    outline = _outline_rounded_rect(half, half, half * 0.32, 6)
    hole = half + 0.010
    n = _normalize3(normal) if normal is not None else (0.0, 0.0, 1.0)
    frame = _frame_z_to(n, (cx, cy, face_z))
    _add_rounded_prism(
        mesh,
        _outline_rounded_rect(hole, hole, hole * 0.34, 6),
        (0.0, 0.0),
        -0.004,
        0.0025,
        0.002,
        lambda _p, role: _PRO_HOLE if role == "face" else _PRO_BEZEL,
        lambda _p, role: 0.80,
        transform=frame,
    )
    top = frame.point((0.0, 0.0, height))
    with mesh.motion(
        _button_spec(
            MotionGroup.CAPTURE,
            top,
            press_depth=press_depth,
            press_axis=(0.0 - n[0], 0.0 - n[1], 0.0 - n[2]),
        )
    ):
        _add_rounded_prism(
            mesh,
            outline,
            (0.0, 0.0),
            -0.004,
            height,
            0.003,
            lambda _p, role: top_color if role in ("face", "seam") else side_color,
            lambda _p, role: 0.28 if role == "face" else 0.48,
            face_bevel=0.006,
            transform=frame,
        )
        _add_decal(
            mesh,
            (0.0, 0.0),
            decal_size,
            "CAP",
            height + 0.0015,
            transform=frame,
        )


# ====================================================================== Switch Pro
#
# 筐体は実寸（mm）の符号付き距離関数（SDF）で作り、Surface Nets で
# メッシュ化する。外形の基準は公式仕様の 幅152 × 高さ106 × 厚さ60 mm。
#
# - 本体: 正面から見て上辺がわずかに凸の角丸長方形を、前側 R7・
#   背面側 R11 で丸めた厚板。前面はゆるいドーム、背面はほぼ平ら。
# - グリップ: 上端 R21 → 先端 R17.5 のラウンドコーン。先端は外へ開き、
#   背面側へ下がる。本体とは k=9 mm のスムーズユニオン。
# - スティックの穴は筐体側に実際に掘る。
# - 前面シェル（艶あり）と背面シェル（マット）は、法線の向きで塗り分ける
#   （正面から見た外形線が分割線になる）。
#
# 配置は公式の図に合わせる: 前面は 左スティック・－・キャプチャ・HOME・
# ＋・ABXY・十字キー・右スティック。上面は USB-C、その -X 側に SYNC、
# +X 側に充電 LED。プレイヤー LED は本体の下側（両グリップの間）。

_PRO_MM: Final = 1.0 / 66.0
#: Surface Nets の格子間隔（mm）。小さいほど滑らかで頂点が増える。
_PRO_GRID_MM: Final = 1.7

_PRO_LEFT_STICK: Final = (-35.0, 19.0)
_PRO_RIGHT_STICK: Final = (21.0, -1.0)
_PRO_STICK_HOLE_RADIUS: Final = 12.0
_PRO_STICK_HOLE_DEPTH: Final = 2.2
_PRO_DPAD_CENTER: Final = (-21.0, -1.0)
_PRO_ABXY_CENTER: Final = (35.0, 19.0)
_PRO_ABXY_SPACING: Final = 10.2
_PRO_MINUS: Final = (-16.0, 33.0)
_PRO_PLUS: Final = (16.0, 33.0)
_PRO_CAPTURE: Final = (-10.0, 21.0)
_PRO_HOME: Final = (10.0, 21.0)

# 前面シェルはスモークの艶あり、背面シェルはマット。
_PRO_FRONT: Final = (0.070, 0.074, 0.084)
_PRO_FRONT_GLOSS: Final = (0.098, 0.104, 0.118)
_PRO_BACK_SHELL: Final = (0.040, 0.042, 0.047)
_PRO_BACK_GRIP: Final = (0.030, 0.031, 0.035)

_PRO_SEAM: Final = (0.012, 0.013, 0.016)


def _mm(value: float) -> float:
    return value * _PRO_MM


def _mm3(p: Sequence[float]) -> Vec3:
    return (p[0] * _PRO_MM, p[1] * _PRO_MM, p[2] * _PRO_MM)


# ---------------------------------------------------------------- SDF 部品


def _np_smoothstep(edge0: float, edge1: float, value: np.ndarray) -> np.ndarray:
    t = np.clip((value - edge0) / (edge1 - edge0), 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


def _smin(a: np.ndarray, b: np.ndarray, k: float) -> np.ndarray:
    """多項式スムーズ min（k は丸みの幅）。"""
    h = np.maximum(k - np.abs(a - b), 0.0) / k
    return np.minimum(a, b) - h * h * k * 0.25


def _smax(a: np.ndarray, b: np.ndarray, k: float) -> np.ndarray:
    return -_smin(-a, -b, k)


def _sd_round_cone(
    px: np.ndarray,
    py: np.ndarray,
    pz: np.ndarray,
    a: Vec3,
    b: Vec3,
    r1: float,
    r2: float,
) -> np.ndarray:
    """端点 a（半径 r1）と b（半径 r2）を結ぶラウンドコーンの SDF。"""
    bax, bay, baz = b[0] - a[0], b[1] - a[1], b[2] - a[2]
    l2 = bax * bax + bay * bay + baz * baz
    rr = r1 - r2
    a2 = l2 - rr * rr
    il2 = 1.0 / l2
    pax = px - a[0]
    pay = py - a[1]
    paz = pz - a[2]
    yv = pax * bax + pay * bay + paz * baz
    zv = yv - l2
    wx = pax * l2 - bax * yv
    wy = pay * l2 - bay * yv
    wz = paz * l2 - baz * yv
    x2 = wx * wx + wy * wy + wz * wz
    y2 = yv * yv * l2
    z2 = zv * zv * l2
    k = math.copysign(1.0, rr) * rr * rr * x2
    d_end = np.sqrt(x2 + z2) * il2 - r2
    d_start = np.sqrt(x2 + y2) * il2 - r1
    d_side = (np.sqrt(np.maximum(x2 * a2 * il2, 0.0)) + yv * rr) * il2 - r1
    return np.where(
        np.sign(zv) * a2 * z2 > k,
        d_end,
        np.where(np.sign(yv) * a2 * y2 < k, d_start, d_side),
    )


def _pro_base_sdf(x: np.ndarray, y: np.ndarray, z: np.ndarray) -> np.ndarray:
    """穴を掘る前の Pro 筐体（mm）。"""
    ax = np.abs(x)
    # 正面の外形: 上辺が少し凸の角丸長方形。
    yb = y + 0.0011 * x * x
    half_x, half_y, center_y, corner = 57.0, 32.0, 12.0, 17.0
    qx = ax - (half_x - corner)
    qy = np.abs(yb - center_y) - (half_y - corner)
    d2 = (
        np.hypot(np.maximum(qx, 0.0), np.maximum(qy, 0.0))
        + np.minimum(np.maximum(qx, qy), 0.0)
        - corner
    )
    # 厚み: 前面はドーム、背面はほぼ平ら。
    top = 13.5 - 3.5 * (x / 57.0) ** 2 - 2.0 * ((y - 16.0) / 34.0) ** 2
    bottom = -15.0 + 2.5 * (x / 57.0) ** 2 + 1.0 * ((y - 12.0) / 32.0) ** 2
    zc = (top + bottom) * 0.5
    hz = (top - bottom) * 0.5
    # 縁の R は前側 7 mm、背面側 11 mm（z=zc で連続）。
    r = np.where(z > zc, 7.0, 11.0)
    a = d2 + r
    b = np.abs(z - zc) - hz + r
    body = (
        np.hypot(np.maximum(a, 0.0), np.maximum(b, 0.0))
        + np.minimum(np.maximum(a, b), 0.0)
        - r
    )
    # グリップ（左右対称なので |x| で 1 本だけ評価）。
    grip = _sd_round_cone(ax, y, z, (43.0, 6.0, -9.5), (61.0, -44.0, -13.5), 20.5, 16.0)
    return _smin(body, grip, 9.0)


class _ProShape:
    """Pro 筐体の SDF と、表面上の点・法線の問い合わせ（すべて mm）。"""

    def __init__(self) -> None:
        self._holes: list[tuple[np.ndarray, np.ndarray, float]] = []
        self.stick_frames: dict[str, tuple[Vec3, Vec3]] = {}
        for name, (x, y) in (("left", _PRO_LEFT_STICK), ("right", _PRO_RIGHT_STICK)):
            point, normal = self.face_point(x, y, base=True)
            floor = tuple(
                point[i] - normal[i] * _PRO_STICK_HOLE_DEPTH for i in range(3)
            )
            self._holes.append(
                (np.asarray(floor), np.asarray(normal), _PRO_STICK_HOLE_RADIUS)
            )
            self.stick_frames[name] = (floor, normal)  # type: ignore[assignment]

    def sdf(
        self, x: np.ndarray, y: np.ndarray, z: np.ndarray, *, base: bool = False
    ) -> np.ndarray:
        d = _pro_base_sdf(x, y, z)
        if base:
            return d
        for center, normal, radius in self._holes:
            px = x - center[0]
            py = y - center[1]
            pz = z - center[2]
            along = px * normal[0] + py * normal[1] + pz * normal[2]
            rx = px - along * normal[0]
            ry = py - along * normal[1]
            rz = pz - along * normal[2]
            radial = np.sqrt(rx * rx + ry * ry + rz * rz)
            hole = np.maximum(radial - radius, -along)
            d = _smax(d, -hole, 2.2)
        return d

    def gradient(
        self, points: np.ndarray, *, base: bool = False, eps: float = 0.05
    ) -> np.ndarray:
        x, y, z = points[:, 0], points[:, 1], points[:, 2]
        gx = self.sdf(x + eps, y, z, base=base) - self.sdf(x - eps, y, z, base=base)
        gy = self.sdf(x, y + eps, z, base=base) - self.sdf(x, y - eps, z, base=base)
        gz = self.sdf(x, y, z + eps, base=base) - self.sdf(x, y, z - eps, base=base)
        return np.stack([gx, gy, gz], axis=1) / (2.0 * eps)

    def normal_at(self, point: Sequence[float], *, base: bool = False) -> Vec3:
        g = self.gradient(np.asarray([point], dtype=np.float64), base=base)[0]
        return _normalize3((float(g[0]), float(g[1]), float(g[2])))

    def _march_many(
        self,
        starts: np.ndarray,
        directions: np.ndarray,
        distance: float,
        *,
        base: bool,
    ) -> np.ndarray:
        """各 start（内側）から direction へ進み、表面との交点を二分法で求める。"""

        def value(t: np.ndarray) -> np.ndarray:
            p = starts + directions * t[:, None]
            return self.sdf(p[:, 0], p[:, 1], p[:, 2], base=base)

        count = starts.shape[0]
        low = np.zeros(count)
        high = np.full(count, distance)
        if np.any(value(low) >= 0.0) or np.any(value(high) <= 0.0):
            raise RuntimeError("Pro 筐体の表面が見つかりません")
        for _ in range(48):
            middle = (low + high) * 0.5
            inside = value(middle) < 0.0
            low = np.where(inside, middle, low)
            high = np.where(inside, high, middle)
        return starts + directions * ((low + high) * 0.5)[:, None]

    def _march(
        self,
        start: Vec3,
        direction: Vec3,
        distance: float,
        *,
        base: bool,
    ) -> Vec3:
        p = self._march_many(
            np.asarray([start], dtype=np.float64),
            np.asarray([direction], dtype=np.float64),
            distance,
            base=base,
        )[0]
        return (float(p[0]), float(p[1]), float(p[2]))

    def face_point(
        self, x: float, y: float, *, base: bool = False
    ) -> tuple[Vec3, Vec3]:
        """前面（+Z 側）の表面点と外向き法線。"""
        point = self._march((x, y, -6.0), (0.0, 0.0, 1.0), 40.0, base=base)
        return point, self.normal_at(point, base=base)

    def edge_point(self, x: float, z: float, direction_y: float) -> tuple[Vec3, Vec3]:
        """上辺（+1）・下辺（-1）の表面点と外向き法線。"""
        point = self._march((x, 8.0, z), (0.0, direction_y, 0.0), 70.0, base=False)
        return point, self.normal_at(point)

    def contour(
        self,
        z: float,
        angles_deg: Sequence[float],
        center: Vec2 = (0.0, 8.0),
    ) -> tuple[list[Vec2], list[Vec2]]:
        """高さ z の断面外形を、center からの放射方向で拾う（点と外向き法線）。"""
        rad = np.radians(np.asarray(angles_deg, dtype=np.float64))
        directions = np.stack([np.cos(rad), np.sin(rad), np.zeros_like(rad)], axis=1)
        starts = np.tile(np.asarray([center[0], center[1], z]), (len(rad), 1))
        hits = self._march_many(starts, directions, 120.0, base=False)
        grads = self.gradient(hits)
        points = [(float(p[0]), float(p[1])) for p in hits]
        normals = [_normalize2((float(g[0]), float(g[1]))) for g in grads]
        return points, normals


# ---------------------------------------------------------------- Surface Nets


def _surface_nets(
    sdf: Callable[[np.ndarray, np.ndarray, np.ndarray], np.ndarray],
    gradient: Callable[[np.ndarray], np.ndarray],
    low: Vec3,
    high: Vec3,
    step: float,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """SDF の 0 等値面を Surface Nets で三角形化する。

    戻り値は (頂点位置 Vx3, 頂点法線 Vx3, 三角形の頂点番号 Tx3)。
    巻き方向は外向き（SDF の勾配方向）にそろえる。
    """
    xs = np.arange(low[0], high[0] + step * 0.5, step)
    ys = np.arange(low[1], high[1] + step * 0.5, step)
    zs = np.arange(low[2], high[2] + step * 0.5, step)
    grid_x, grid_y, grid_z = np.meshgrid(xs, ys, zs, indexing="ij")
    field = sdf(grid_x, grid_y, grid_z)
    inside = field < 0.0
    cells = (len(xs) - 1, len(ys) - 1, len(zs) - 1)
    sums = np.zeros(cells + (3,), dtype=np.float64)
    counts = np.zeros(cells, dtype=np.int32)
    quad_cells: list[np.ndarray] = []
    axes_values = (xs, ys, zs)

    for axis in range(3):
        lower = [slice(None)] * 3
        upper = [slice(None)] * 3
        lower[axis] = slice(0, -1)
        upper[axis] = slice(1, None)
        f0 = field[tuple(lower)]
        f1 = field[tuple(upper)]
        crossing = inside[tuple(lower)] != inside[tuple(upper)]
        edge = np.nonzero(crossing)
        if len(edge[0]) == 0:
            continue
        a = f0[edge]
        b = f1[edge]
        t = a / (a - b)
        point = np.stack(
            [axes_values[0][edge[0]], axes_values[1][edge[1]], axes_values[2][edge[2]]],
            axis=1,
        )
        point[:, axis] += t * step
        other1, other2 = (k for k in range(3) if k != axis)
        around: list[list[np.ndarray]] = []
        for d1, d2 in ((-1, -1), (0, -1), (0, 0), (-1, 0)):
            index = [edge[0].copy(), edge[1].copy(), edge[2].copy()]
            index[other1] += d1
            index[other2] += d2
            around.append(index)
        all_valid = np.ones(len(a), dtype=bool)
        for index in around:
            valid = np.ones(len(a), dtype=bool)
            for k in range(3):
                valid &= (index[k] >= 0) & (index[k] < cells[k])
            all_valid &= valid
            target = (index[0][valid], index[1][valid], index[2][valid])
            np.add.at(sums, target, point[valid])
            np.add.at(counts, target, 1)
        flat = [
            np.ravel_multi_index(
                (index[0][all_valid], index[1][all_valid], index[2][all_valid]), cells
            )
            for index in around
        ]
        quad_cells.append(np.stack(flat, axis=1))

    active = counts > 0
    vertex_id = -np.ones(cells, dtype=np.int64)
    vertex_id[active] = np.arange(int(active.sum()))
    vertices = sums[active] / counts[active][:, None]
    quads = vertex_id.reshape(-1)[np.concatenate(quad_cells, axis=0)]

    # 頂点を等値面へ寄せる（ニュートン法、1 ステップは格子間隔まで）。
    for _ in range(3):
        value = sdf(vertices[:, 0], vertices[:, 1], vertices[:, 2])
        grad = gradient(vertices)
        g2 = np.maximum((grad * grad).sum(axis=1), 1.0e-12)
        move = -(value / g2)[:, None] * grad
        length = np.linalg.norm(move, axis=1, keepdims=True)
        move *= np.minimum(1.0, step / np.maximum(length, 1.0e-12))
        vertices = vertices + move
    normals = gradient(vertices)
    normals /= np.maximum(np.linalg.norm(normals, axis=1, keepdims=True), 1.0e-12)

    # 四角形を短い対角線で 2 分割する。
    p = vertices[quads]
    split_ac = np.linalg.norm(p[:, 0] - p[:, 2], axis=1) <= np.linalg.norm(
        p[:, 1] - p[:, 3], axis=1
    )
    first = np.where(split_ac[:, None], quads[:, [0, 1, 2]], quads[:, [0, 1, 3]])
    second = np.where(split_ac[:, None], quads[:, [0, 2, 3]], quads[:, [1, 2, 3]])
    triangles = np.concatenate([first, second], axis=0)

    # 外向きにそろえ、潰れた三角形を落とす。
    tp = vertices[triangles]
    face = np.cross(tp[:, 1] - tp[:, 0], tp[:, 2] - tp[:, 0])
    area = np.linalg.norm(face, axis=1)
    facing = (face * normals[triangles].sum(axis=1)).sum(axis=1)
    flip = facing < 0.0
    triangles[flip] = triangles[flip][:, [0, 2, 1]]
    triangles = triangles[area > 1.0e-6 * step * step]
    return vertices, normals, triangles


def _pro_shell_material(
    positions: np.ndarray, normals: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    """筐体の頂点色と粗さ（mm 座標・単位法線）。"""
    x = positions[:, 0]
    y = positions[:, 1]
    nz = normals[:, 2]
    front = _np_smoothstep(-0.32, -0.02, nz)
    grip = _np_smoothstep(28.0, 46.0, np.abs(x)) * _np_smoothstep(2.0, -24.0, y)

    gloss = np.clip(1.0 - (x / 52.0) ** 2 - ((y - 16.0) / 30.0) ** 2, 0.0, 1.0) * 0.6
    front_color = (
        np.asarray(_PRO_FRONT)[None, :] * (1.0 - gloss[:, None])
        + np.asarray(_PRO_FRONT_GLOSS)[None, :] * gloss[:, None]
    )
    back_color = (
        np.asarray(_PRO_BACK_SHELL)[None, :] * (1.0 - grip[:, None])
        + np.asarray(_PRO_BACK_GRIP)[None, :] * grip[:, None]
    )
    color = back_color * (1.0 - front[:, None]) + front_color * front[:, None]
    rough = (0.60 + 0.28 * grip) * (1.0 - front) + (0.17 + 0.06 * grip) * front

    # 前後シェルの分割線（外形線の少し背面寄り）を細く暗くする。
    seam = np.exp(-(((nz + 0.20) / 0.07) ** 2))
    color *= (1.0 - 0.55 * seam)[:, None]
    rough = rough + (0.85 - rough) * seam

    # スティックの穴の内側は暗く。
    for cx, cy in (_PRO_LEFT_STICK, _PRO_RIGHT_STICK):
        radial = np.hypot(x - cx, y - cy)
        hole = _np_smoothstep(
            _PRO_STICK_HOLE_RADIUS + 1.4, _PRO_STICK_HOLE_RADIUS - 0.2, radial
        )
        hole *= _np_smoothstep(0.2, 0.6, nz)
        color = (
            color * (1.0 - hole[:, None])
            + np.asarray(_PRO_HOLE)[None, :] * hole[:, None]
        )
        rough = rough + (0.75 - rough) * hole
    return color, rough


def _add_pro_shell(mesh: _MeshBuilder, shape: _ProShape) -> None:
    step = _PRO_GRID_MM
    vertices, normals, triangles = _surface_nets(
        lambda x, y, z: shape.sdf(x, y, z),
        lambda p: shape.gradient(p),
        (-82.0, -66.0, -36.0),
        (82.0, 50.0, 19.0),
        step,
    )
    colors, roughs = _pro_shell_material(vertices, normals)
    order = triangles.reshape(-1)
    mesh.add_triangles(
        vertices[order] * _PRO_MM,
        normals[order],
        colors[order],
        roughs[order],
    )


# ---------------------------------------------------------------- 前面パーツ


def _add_pro_dpad(mesh: _MeshBuilder, origin: Vec3, normal: Vec3) -> None:
    """十字キー（腕の長さ 24 mm）。origin は前面上の中心（モデル単位）。"""
    frame = _frame_z_to(normal, origin)
    k = 12.0 * _PRO_MM / 0.150
    half_length = 0.150 * k
    half_width = 0.050 * k
    outline = _cross_outline(half_length, half_width, 0.020 * k, 96)

    # 十字の穴（暗い隙間）を一回り大きく敷く。
    _add_shell(
        mesh,
        [
            (x * 1.07, y * 1.07)
            for x, y in _cross_outline(half_length, half_width, 0.024 * k, 96)
        ],
        (0.0, 0.0),
        lambda _x, _y: -0.004,
        lambda _x, _y: 0.0030,
        lambda _x, _y: 0.0030,
        lambda _x, _y: -0.004,
        _RimStyle(
            back_radius=0.001,
            face_radius=0.002,
            back_steps=2,
            face_steps=2,
            side_steps=2,
        ),
        lambda _p, _r: _PRO_HOLE,
        lambda _p, _r: 0.85,
        bottom_scales=(0.5,),
        top_scales=(0.6,),
        transform=frame,
    )

    height = 0.040

    def top_z(x: float, y: float) -> float:
        # 腕の先がわずかに盛り上がる（押す位置の目印）。
        reach = max(abs(x), abs(y)) / half_length
        # 別の面を下へ重ねず、閉じた天面そのものを中央でくぼませる。
        depression = 0.0042 * (1.0 - _smoothstep(0.0, 0.040 * k, math.hypot(x, y)))
        return height + 0.005 * _smoothstep(0.45, 1.0, reach) - depression

    def base_z(x: float, y: float) -> float:
        return -0.010

    def color(position: Vec3, role: str) -> Color:
        if role == "seam":
            return _PRO_SEAM
        if role == "back":
            return _PRO_HOLE
        if role == "side":
            return _PRO_DPAD_SIDE
        weight = 1.0 - _smoothstep(0.0, 0.040 * k, math.hypot(position[0], position[1]))
        return _mix_color(_PRO_DPAD_TOP, (0.045, 0.048, 0.054), weight)

    def roughness(position: Vec3, role: str) -> float:
        return 0.70 if role == "back" else 0.30 if role == "face" else 0.46

    pivot = frame.point((0.0, 0.0, height))
    spec = replace(
        _dpad_spec(MotionGroup.DPAD, pivot),
        tilt_axes=(
            frame.normal((1.0, 0.0, 0.0)),
            frame.normal((0.0, 1.0, 0.0)),
        ),
    )
    with mesh.motion(spec):
        _add_shell(
            mesh,
            outline,
            (0.0, 0.0),
            base_z,
            lambda _x, _y: height,
            top_z,
            base_z,
            _RimStyle(
                back_radius=0.004,
                face_radius=0.010,
                back_steps=2,
                face_steps=3,
                side_steps=2,
            ),
            color,
            roughness,
            bottom_scales=(0.62, 0.86),
            top_scales=(0.86, 0.66, 0.42, 0.20),
            transform=frame,
        )
        # 腕の先の小さな三角（4 方向）。
        mark = (0.030, 0.032, 0.037)
        n = (0.0, 0.0, 1.0)
        for angle in (0.0, 90.0, 180.0, 270.0):
            rad = math.radians(angle)
            ux = math.cos(rad)
            uy = math.sin(rad)
            tip = 0.124 * k
            back = 0.100 * k
            half = 0.016 * k
            z = top_z(ux * 0.112 * k, uy * 0.112 * k) + 0.0008
            p0 = (ux * tip, uy * tip, z)
            p1 = (ux * back - uy * half, uy * back + ux * half, z)
            p2 = (ux * back + uy * half, uy * back - ux * half, z)
            mesh.oriented_triangle(
                frame.point(p0),
                frame.point(p1),
                frame.point(p2),
                frame.normal(n),
                frame.normal(n),
                frame.normal(n),
                mark,
                mark,
                mark,
                0.60,
                0.60,
                0.60,
            )


def _add_pro_face_parts(mesh: _MeshBuilder, shape: _ProShape) -> None:
    # スティック（筐体に掘った穴の底から立てる）。
    for name, group, click in (
        ("left", MotionGroup.STICK_L, "LCLICK"),
        ("right", MotionGroup.STICK_R, "RCLICK"),
    ):
        floor, normal = shape.stick_frames[name]
        p = _mm3(floor)
        _add_stick(
            mesh,
            p[0],
            p[1],
            p[2],
            group=group,
            stick=name,
            click=click,
            scale=0.88,
            normal=normal,
            well=False,
            stem_lift=_mm(4.5),
        )
        # 穴の縁の細いリング（Surface Nets の段差を隠す面取り）。
        x, y = _PRO_LEFT_STICK if name == "left" else _PRO_RIGHT_STICK
        rim_point, rim_normal = shape.face_point(x, y, base=True)
        _add_torus(
            mesh,
            (0.0, 0.0),
            _mm(_PRO_STICK_HOLE_RADIUS + 0.4),
            _mm(0.9),
            _mm(-0.35),
            (0.050, 0.053, 0.060),
            0.28,
            segments=64,
            tube_segments=8,
            transform=_frame_z_to(rim_normal, _mm3(rim_point)),
        )

    # 十字キー。
    point, normal = shape.face_point(*_PRO_DPAD_CENTER)
    _add_pro_dpad(mesh, _mm3(point), normal)

    # ABXY（X 上 / A 右 / B 下 / Y 左）。
    cx, cy = _PRO_ABXY_CENTER
    d = _PRO_ABXY_SPACING
    for label, dx, dy in (("X", 0.0, d), ("A", d, 0.0), ("B", 0.0, -d), ("Y", -d, 0.0)):
        point, normal = shape.face_point(cx + dx, cy + dy)
        p = _mm3(point)
        _add_round_button(
            mesh,
            p[0],
            p[1],
            p[2],
            _mm(5.0),
            label=label,
            decal_size=_mm(5.6),
            height=_mm(2.6),
            normal=normal,
        )

    # － / ＋。
    for label, (x, y) in (("-", _PRO_MINUS), ("+", _PRO_PLUS)):
        point, normal = shape.face_point(x, y)
        p = _mm3(point)
        _add_round_button(
            mesh,
            p[0],
            p[1],
            p[2],
            _mm(3.4),
            label=label,
            decal_size=_mm(3.6),
            height=_mm(1.6),
            segments=32,
            normal=normal,
        )

    # キャプチャ（角丸四角）。
    point, normal = shape.face_point(*_PRO_CAPTURE)
    p = _mm3(point)
    _add_capture_button(
        mesh,
        p[0],
        p[1],
        p[2],
        half=_mm(3.3),
        height=_mm(1.5),
        decal_size=_mm(4.0),
        normal=normal,
    )

    # HOME（丸＋お知らせ LED のリング）。
    point, normal = shape.face_point(*_PRO_HOME)
    p = _mm3(point)
    _add_round_button(
        mesh,
        p[0],
        p[1],
        p[2],
        _mm(4.4),
        label="HOME",
        decal_size=_mm(4.6),
        height=_mm(1.6),
        top_color=(0.030, 0.032, 0.037),
        segments=36,
        normal=normal,
    )
    _add_torus(
        mesh,
        (0.0, 0.0),
        _mm(4.4) + 0.0135,
        0.0026,
        0.0032,
        _scale_color(_PRO_ACCENT, 0.55),
        0.30,
        segments=48,
        tube_segments=6,
        transform=_frame_z_to(normal, p),
    )


# ---------------------------------------------------------------- 上面・下面


def _add_pro_edge_parts(mesh: _MeshBuilder, shape: _ProShape) -> None:
    # 上面中央の USB-C。厚みの中央あたりに開ける。
    z_mid = -1.5
    point, normal = shape.edge_point(0.0, z_mid, 1.0)
    frame = _frame_z_to(normal, _mm3(point))
    _add_rounded_prism(
        mesh,
        _outline_rounded_rect(_mm(5.6), _mm(2.0), _mm(1.4), 5),
        (0.0, 0.0),
        -0.010,
        0.0020,
        0.0015,
        lambda _p, role: (0.050, 0.053, 0.060),
        lambda _p, role: 0.36,
        transform=frame,
    )
    _add_rounded_prism(
        mesh,
        _outline_rounded_rect(_mm(4.4), _mm(1.2), _mm(1.0), 5),
        (0.0, 0.0),
        -0.008,
        0.0026,
        0.0010,
        lambda _p, role: _PRO_HOLE,
        lambda _p, role: 0.80,
        transform=frame,
    )

    # SYNC（USB-C の -X 側）。固定扱い。
    point, normal = shape.edge_point(-10.0, z_mid, 1.0)
    p = _mm3(point)
    _add_round_button(
        mesh,
        p[0],
        p[1],
        p[2],
        _mm(1.7),
        height=_mm(0.6),
        segments=20,
        normal=normal,
    )

    # 充電 LED（USB-C の +X 側）。
    point, normal = shape.edge_point(9.0, z_mid, 1.0)
    _add_disk(
        mesh,
        (0.0, 0.0),
        _mm(0.9),
        0.0012,
        (0.120, 0.060, 0.030),
        0.25,
        segments=16,
        transform=_frame_z_to(normal, _mm3(point)),
    )

    # プレイヤー LED（下側、両グリップの間）。暗い窓＋ 4 灯。
    point, normal = shape.edge_point(0.0, z_mid, -1.0)
    frame = _frame_z_to(normal, _mm3(point))
    _add_rounded_prism(
        mesh,
        _outline_rounded_rect(_mm(11.0), _mm(1.9), _mm(1.0), 5),
        (0.0, 0.0),
        -0.010,
        0.0012,
        0.0008,
        lambda _p, role: (0.010, 0.011, 0.013),
        lambda _p, role: 0.18,
        transform=frame,
    )
    for index in range(4):
        x = -7.5 + 5.0 * index
        point, normal = shape.edge_point(x, z_mid, -1.0)
        _add_rounded_prism(
            mesh,
            _outline_rounded_rect(_mm(1.5), _mm(0.7), _mm(0.4), 4),
            (0.0, 0.0),
            0.0004,
            0.0026,
            0.0006,
            lambda _p, role: _PRO_ACCENT,
            lambda _p, role: 0.20,
            transform=_frame_z_to(normal, _mm3(point)),
        )


# ---------------------------------------------------------------- 肩


def _add_pro_shoulders(mesh: _MeshBuilder, shape: _ProShape) -> None:
    """L/R と ZL/ZR。上側の角に沿って曲がるスイープ。

    L/R は前寄り（厚みの前半）、ZL/ZR はその背面側で一回り大きく、
    少し外へ張り出す。
    """

    def bumper_color(position: Vec3, role: str) -> Color:
        if role == "face":
            return (0.088, 0.093, 0.105)
        if role == "back":
            return (0.030, 0.032, 0.037)
        return (0.062, 0.066, 0.075)

    def trigger_color(position: Vec3, role: str) -> Color:
        if role == "face":
            return (0.056, 0.059, 0.067)
        if role == "back":
            return (0.024, 0.025, 0.029)
        return (0.044, 0.046, 0.053)

    steps = 28
    for side in (-1.0, 1.0):
        angles = [18.0 + (70.0 - 18.0) * i / steps for i in range(steps + 1)]
        if side < 0.0:
            angles = [180.0 - a for a in reversed(angles)]
        bumper_group = MotionGroup.L if side < 0.0 else MotionGroup.R
        trigger_group = MotionGroup.ZL if side < 0.0 else MotionGroup.ZR

        for group, z_center, width, height, radius, embed, color_fn, rough in (
            (bumper_group, 2.5, 10.0, 10.0, 3.6, 4.0, bumper_color, 0.30),
            (trigger_group, -11.0, 13.0, 13.0, 5.0, 4.0, trigger_color, 0.55),
        ):
            contour, normals = shape.contour(z_center, angles)
            offset = width * 0.5 - embed
            path = [
                (_mm(x + n[0] * offset), _mm(y + n[1] * offset))
                for (x, y), n in zip(contour, normals)
            ]
            mid = path[len(path) // 2]
            with mesh.motion(
                _button_spec(
                    group,
                    (mid[0], mid[1], _mm(z_center)),
                    press_depth=_mm(1.2),
                    press_axis=(0.0, -1.0, 0.0),
                )
            ):
                _add_sweep(
                    mesh,
                    path,
                    normals,
                    lambda _t: _mm(z_center),
                    _mm(width),
                    _mm(height),
                    _mm(radius),
                    color_fn,
                    lambda _p, role: rough if role == "face" else rough + 0.12,
                    taper=0.14,
                    profile_segments=5,
                )


def _build_pro() -> _MeshBuilder:
    mesh = _MeshBuilder()
    shape = _ProShape()
    _add_pro_shell(mesh, shape)
    _add_pro_face_parts(mesh, shape)
    _add_pro_edge_parts(mesh, shape)
    _add_pro_shoulders(mesh, shape)
    return mesh


# ====================================================================== Joy-Con


_JOY_L_BASE: Final = (0.000, 0.690, 0.820)
_JOY_R_BASE: Final = (0.940, 0.205, 0.140)
_JOY_RAIL: Final = (0.300, 0.318, 0.348)
_JOY_RAIL_DARK: Final = (0.060, 0.064, 0.072)
_JOY_BUTTON_SIDE: Final = (0.050, 0.053, 0.060)
_JOY_BUTTON_TOP: Final = (0.016, 0.017, 0.020)
_JOY_DPAD_SIDE: Final = (0.050, 0.053, 0.060)
_JOY_DPAD_TOP: Final = (0.030, 0.032, 0.037)

_JOY_HALF_X: Final = 0.260
_JOY_HALF_Y: Final = 0.790
#: 外側（レールと反対側）の角 R と、レール側の角 R。
_JOY_OUTER_RADIUS: Final = 0.225
_JOY_INNER_RADIUS: Final = 0.085

_JOY_RIM: Final = _RimStyle(
    back_radius=0.075,
    face_radius=0.020,
    bulge=0.006,
    seam=True,
    seam_fraction=0.46,
    seam_width=0.004,
    seam_depth=0.0035,
    back_steps=6,
    face_steps=4,
    side_steps=4,
)


def _joy_side_top(x: float, y: float) -> float:
    return 0.105


def _joy_side_bottom(x: float, y: float) -> float:
    return -0.105


def _joy_face_z(x: float, y: float) -> float:
    dome = 0.012 * max(0.0, 1.0 - (x / 0.30) ** 2 - (y / 0.84) ** 2)
    return 0.105 + dome


def _joy_back_z(x: float, y: float) -> float:
    q = math.hypot(x / 0.26, y / 0.80)
    belly = 0.024 * _smoothstep(1.0, 0.10, q)
    return -0.105 - belly


def _joy_face_slope(x: float, y: float) -> float:
    return (_joy_face_z(x, y + 0.08) - _joy_face_z(x, y - 0.08)) / 0.16


def _make_joy_colors(base: Color) -> tuple[ColorFn, RoughFn]:
    sheen = _mix_color(base, (1.0, 1.0, 1.0), 0.10)

    def color(position: Vec3, role: str) -> Color:
        if role == "seam":
            return _scale_color(base, 0.22)
        if role == "back":
            return _scale_color(base, 0.66)
        if role == "side":
            return _scale_color(base, 0.86)
        x, y, _z = position
        glow = 0.6 * max(0.0, 1.0 - (x / 0.24) ** 2 - ((y - 0.10) / 0.70) ** 2)
        return _mix_color(base, sheen, glow)

    def roughness(position: Vec3, role: str) -> float:
        if role == "seam":
            return 0.85
        if role == "back":
            return 0.56
        if role == "side":
            return 0.40
        return 0.30

    return color, roughness


def _joy_rail_sign(model_id: int) -> float:
    # Joy-Con L の内側レールは +X、R は -X。
    return 1.0 if model_id == 2 else -1.0


def _joy_outline(sign: float) -> list[Vec2]:
    outer = _JOY_OUTER_RADIUS
    inner = _JOY_INNER_RADIUS
    if sign > 0.0:
        radii = (inner, outer, outer, inner)
    else:
        radii = (outer, inner, inner, outer)
    return _outline_rounded_rect_corners(_JOY_HALF_X, _JOY_HALF_Y, radii, 12)


def _add_joy_rail(mesh: _MeshBuilder, sign: float) -> None:
    rail_x = 0.272 * sign
    _add_rounded_prism(
        mesh,
        _outline_rounded_rect(0.032, 0.680, 0.014, 5),
        (rail_x, 0.0),
        -0.075,
        0.075,
        0.008,
        lambda _p, role: _JOY_RAIL
        if role in ("face", "side")
        else _scale_color(_JOY_RAIL, 0.62),
        lambda _p, role: 0.30,
    )
    # レール外面の溝。
    _add_rounded_prism(
        mesh,
        _outline_rounded_rect(0.003, 0.600, 0.002, 3),
        (rail_x + 0.031 * sign, 0.0),
        -0.050,
        0.050,
        0.0008,
        lambda _p, role: _JOY_RAIL_DARK,
        lambda _p, role: 0.70,
    )
    # SL / SR の上面ラベル（+Z の小さな棚）。
    # 実機では左右で上下が入れ替わる（L は上=SL、R は上=SR）。
    top_label = "SL" if sign > 0.0 else "SR"
    bottom_label = "SR" if sign > 0.0 else "SL"
    for label, y in ((top_label, 0.520), (bottom_label, -0.520)):
        group = MotionGroup.SL if label == "SL" else MotionGroup.SR
        with mesh.motion(_button_spec(group, (rail_x, y, 0.110), press_depth=0.006)):
            _add_rounded_prism(
                mesh,
                _outline_rounded_rect(0.044, 0.050, 0.016, 5),
                (rail_x, y),
                0.070,
                0.110,
                0.004,
                lambda _p, role: _JOY_BUTTON_TOP
                if role in ("face", "seam")
                else _JOY_BUTTON_SIDE,
                lambda _p, role: 0.28 if role == "face" else 0.48,
                face_bevel=0.008,
            )
            _add_decal(mesh, (rail_x, y), 0.050, label, 0.1115)


def _add_joy_stick(mesh: _MeshBuilder, cx: float, cy: float, model_id: int) -> None:
    group = MotionGroup.STICK_L if model_id == 2 else MotionGroup.STICK_R
    stick = "left" if model_id == 2 else "right"
    click = "LCLICK" if model_id == 2 else "RCLICK"
    _add_stick(
        mesh,
        cx,
        cy,
        _joy_face_z(cx, cy),
        group=group,
        stick=stick,
        click=click,
        scale=0.82,
        tilt=math.atan(_joy_face_slope(cx, cy)),
        press_depth=0.010,
    )


def _add_joy_dpad(mesh: _MeshBuilder, cx: float, cy: float) -> None:
    for dx, dy, group in (
        (0.0, 0.115, MotionGroup.DPAD_UP),
        (0.115, 0.0, MotionGroup.DPAD_RIGHT),
        (0.0, -0.115, MotionGroup.DPAD_DOWN),
        (-0.115, 0.0, MotionGroup.DPAD_LEFT),
    ):
        x = cx + dx
        y = cy + dy
        frame = _tilt_x(math.atan(_joy_face_slope(x, y)), (x, y, _joy_face_z(x, y)))
        _add_round_button(
            mesh,
            x,
            y,
            _joy_face_z(x, y),
            0.050,
            group=group,
            top_color=_JOY_DPAD_TOP,
            side_color=_JOY_DPAD_SIDE,
            height=0.022,
            segments=32,
            press_depth=0.008,
            tilt=math.atan(_joy_face_slope(x, y)),
        )
        # 方向を示す小さな三角（ボタン天面に刻む）。
        top = 0.022 + 0.0042
        length = math.hypot(dx, dy)
        ux = dx / length
        uy = dy / length
        tip = 0.020
        half = 0.014
        p0 = frame.point((ux * tip, uy * tip, top))
        p1 = frame.point((-ux * 0.004 - uy * half, -uy * 0.004 + ux * half, top))
        p2 = frame.point((-ux * 0.004 + uy * half, -uy * 0.004 - ux * half, top))
        n = frame.normal((0.0, 0.0, 1.0))
        mark = (0.150, 0.158, 0.175)
        with mesh.motion(
            _button_spec(
                group,
                frame.point((0.0, 0.0, 0.022)),
                press_depth=0.008,
                press_axis=frame.normal((0.0, 0.0, -1.0)),
            )
        ):
            mesh.oriented_triangle(p0, p1, p2, n, n, n, mark, mark, mark, 0.4, 0.4, 0.4)


def _add_joy_abxy(mesh: _MeshBuilder, cx: float, cy: float) -> None:
    diamond = 0.120
    for label, dx, dy in (
        ("X", 0.0, diamond),
        ("A", diamond, 0.0),
        ("B", 0.0, -diamond),
        ("Y", -diamond, 0.0),
    ):
        x = cx + dx
        y = cy + dy
        _add_round_button(
            mesh,
            x,
            y,
            _joy_face_z(x, y),
            0.050,
            label=label,
            decal_size=0.056,
            top_color=_JOY_BUTTON_TOP,
            side_color=_JOY_BUTTON_SIDE,
            height=0.022,
            segments=32,
            press_depth=0.008,
            tilt=math.atan(_joy_face_slope(x, y)),
        )


def _add_joy_special_buttons(mesh: _MeshBuilder, model_id: int) -> None:
    sign = _joy_rail_sign(model_id)
    x = 0.130 * sign
    # 内側レール側の上端は -/+。
    top_label = "-" if model_id == 2 else "+"
    y = 0.630
    _add_round_button(
        mesh,
        x,
        y,
        _joy_face_z(x, y),
        0.036,
        label=top_label,
        decal_size=0.040,
        top_color=_JOY_BUTTON_TOP,
        side_color=_JOY_BUTTON_SIDE,
        height=0.018,
        segments=28,
        press_depth=0.007,
    )
    # 下端は L=キャプチャ（角丸四角）、R=HOME（丸＋縁取り）。
    y = -0.630
    if model_id == 2:
        _add_capture_button(
            mesh,
            x,
            y,
            _joy_face_z(x, y),
            half=0.032,
            height=0.018,
            top_color=_JOY_BUTTON_TOP,
            side_color=_JOY_BUTTON_SIDE,
            decal_size=0.040,
            press_depth=0.007,
        )
    else:
        _add_round_button(
            mesh,
            x,
            y,
            _joy_face_z(x, y),
            0.038,
            label="HOME",
            decal_size=0.044,
            top_color=_JOY_BUTTON_TOP,
            side_color=_JOY_BUTTON_SIDE,
            height=0.018,
            segments=28,
            press_depth=0.007,
        )
        _add_torus(
            mesh,
            (x, y),
            0.038 + 0.0135,
            0.0026,
            _joy_face_z(x, y) + 0.0035,
            (0.180, 0.190, 0.210),
            0.25,
            segments=36,
            tube_segments=6,
        )


def _add_joy_shoulders(
    mesh: _MeshBuilder, model_id: int, outline: Sequence[Vec2]
) -> None:
    """L/R と ZL/ZR。外側上端の大きな角に沿って曲がるスイープ。"""
    sign = _joy_rail_sign(model_id)
    bumper_face = (0.070, 0.074, 0.084)
    bumper_side = (0.040, 0.043, 0.049)
    trigger_face = (0.050, 0.053, 0.060)
    trigger_side = (0.028, 0.030, 0.034)

    def bumper_color(position: Vec3, role: str) -> Color:
        return bumper_face if role == "face" else bumper_side

    def trigger_color(position: Vec3, role: str) -> Color:
        return trigger_face if role == "face" else trigger_side

    def keep(x: float, y: float) -> bool:
        return y > 0.50 and sign * x < 0.200

    path, normals = _outline_path(outline, keep, 0.003, smooth=1)
    mid = path[len(path) // 2]
    bumper_group = MotionGroup.L if model_id == 2 else MotionGroup.R
    trigger_group = MotionGroup.ZL if model_id == 2 else MotionGroup.ZR
    with mesh.motion(
        _button_spec(
            bumper_group,
            (mid[0], mid[1], 0.055),
            press_depth=0.010,
            press_axis=(0.0, -1.0, 0.0),
        )
    ):
        _add_sweep(
            mesh,
            path,
            normals,
            lambda _t: 0.060,
            0.070,
            0.070,
            0.026,
            bumper_color,
            lambda _p, role: 0.32 if role == "face" else 0.44,
            taper=0.12,
        )
    trigger_path = [
        (x + n[0] * 0.010, y + n[1] * 0.010) for (x, y), n in zip(path, normals)
    ]
    with mesh.motion(
        _button_spec(
            trigger_group,
            (mid[0], mid[1], -0.060),
            press_depth=0.012,
            press_axis=(0.0, -1.0, 0.0),
        )
    ):
        _add_sweep(
            mesh,
            trigger_path,
            normals,
            lambda _t: -0.058,
            0.085,
            0.090,
            0.032,
            trigger_color,
            lambda _p, role: 0.58,
            taper=0.12,
        )


def _add_joy_back_button(mesh: _MeshBuilder, sign: float) -> None:
    # 背面のレール解除ボタン（レール寄りの上端）。
    x = 0.200 * sign
    y = 0.660
    z = _joy_back_z(x, y)
    _add_disk(
        mesh,
        (x, y),
        0.026,
        z - 0.0035,
        (0.040, 0.043, 0.050),
        0.50,
        normal_z=-1.0,
        segments=24,
    )
    _add_torus(
        mesh,
        (x, y),
        0.030,
        0.0045,
        z - 0.0015,
        _JOY_RAIL,
        0.32,
        segments=24,
        tube_segments=6,
    )


def _build_joy(model_id: int) -> _MeshBuilder:
    base = _JOY_L_BASE if model_id == 2 else _JOY_R_BASE
    color_fn, rough_fn = _make_joy_colors(base)
    sign = _joy_rail_sign(model_id)
    mesh = _MeshBuilder()
    outline = _joy_outline(sign)
    _add_shell(
        mesh,
        outline,
        (0.0, 0.0),
        _joy_side_bottom,
        _joy_side_top,
        _joy_face_z,
        _joy_back_z,
        _JOY_RIM,
        color_fn,
        rough_fn,
        bottom_scales=(0.12, 0.34, 0.56, 0.74, 0.88),
        top_scales=(0.90, 0.76, 0.58, 0.38, 0.16),
    )

    _add_joy_rail(mesh, sign)

    if model_id == 2:
        _add_joy_stick(mesh, 0.0, 0.340, model_id)
        _add_joy_dpad(mesh, 0.0, -0.300)
    else:
        _add_joy_abxy(mesh, 0.0, 0.340)
        _add_joy_stick(mesh, 0.0, -0.300, model_id)

    _add_joy_special_buttons(mesh, model_id)
    _add_joy_shoulders(mesh, model_id, outline)
    _add_joy_back_button(mesh, sign)
    return mesh


# ====================================================================== 仕上げ


def _fit_mesh(array: np.ndarray) -> tuple[np.ndarray, float, float, float]:
    """x/y を原点へ寄せ、半径を 1.34 以下にする。移動量と倍率も返す。"""
    if array.size == 0:
        return array, 0.0, 0.0, 1.0
    positions = array[:, 0:3]
    min_xy = positions[:, 0:2].min(axis=0)
    max_xy = positions[:, 0:2].max(axis=0)
    shift_x = -(float(min_xy[0]) + float(max_xy[0])) * 0.5
    shift_y = -(float(min_xy[1]) + float(max_xy[1])) * 0.5
    if abs(shift_x) > _EPS or abs(shift_y) > _EPS:
        positions[:, 0] += shift_x
        positions[:, 1] += shift_y
    radius = float(np.sqrt((positions * positions).sum(axis=1)).max())
    scale = 1.0
    limit = 1.34
    if radius > limit:
        scale = limit / radius
        positions *= scale
    return np.ascontiguousarray(array, dtype=np.float32), shift_x, shift_y, scale


def _fit_specs(
    specs: Sequence[MotionGroupSpec],
    shift_x: float,
    shift_y: float,
    scale: float,
) -> tuple[MotionGroupSpec, ...]:
    """``_fit_mesh`` と同じ移動・倍率をピボットと押し込み量へ当てる。"""
    fitted: list[MotionGroupSpec] = []
    for spec in specs:
        pivot = (
            (spec.pivot[0] + shift_x) * scale,
            (spec.pivot[1] + shift_y) * scale,
            spec.pivot[2] * scale,
        )
        fitted.append(replace(spec, pivot=pivot, press_depth=spec.press_depth * scale))
    return tuple(fitted)


def _build_mesh(model_id: int) -> _MeshBuilder:
    if model_id == 2:
        return _build_joy(2)
    if model_id == 3:
        return _build_joy(3)
    return _build_pro()


def build_model(model_id: int) -> ControllerModel:
    """頂点列と可動グループのメタデータをまとめて返す。

    ``model_id`` は 1=Pro コントローラー、2=Joy-Con (L)、3=Joy-Con (R)。
    それ以外は Pro として扱う。
    """
    mesh = _build_mesh(model_id)
    vertices, shift_x, shift_y, scale = _fit_mesh(mesh.build())
    return ControllerModel(
        vertices=vertices,
        groups=_fit_specs(mesh.specs(), shift_x, shift_y, scale),
    )


__all__ = [
    "ATLAS_CELL",
    "ATLAS_COLUMNS",
    "ATLAS_LABELS",
    "ATLAS_SIZE",
    "ControllerModel",
    "DPAD_TILT_DEG",
    "MOTION_GROUP_COUNT",
    "MotionGroup",
    "MotionGroupSpec",
    "STICK_TILT_DEG",
    "build_model",
]
