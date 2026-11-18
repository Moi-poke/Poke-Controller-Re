"""IMU 3D プレビュー用のメッシュプリミティブと Joy-Con ビルダー。"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from typing import Final

import numpy as np
from pokecon.domain.imu.models import (
    _BUTTON_GROUP,
    _EPS,
    _FLOATS_PER_VERTEX,
    _NO_UV,
    Color,
    ColorFn,
    MotionGroup,
    MotionGroupSpec,
    RoughFn,
    Vec2,
    Vec3,
    _button_spec,
    _cell_uv,
    _clamp01,
    _cross3,
    _dot3,
    _length3,
    _mix_color,
    _normalize2,
    _normalize3,
    _scale_color,
    _smoothstep,
    _stick_spec,
    _sub3,
)

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
