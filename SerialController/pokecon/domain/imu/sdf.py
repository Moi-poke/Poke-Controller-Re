"""Switch Pro コントローラーの SDF・Surface Nets・部品生成。"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import replace
from typing import Final

import numpy as np
from pokecon.domain.imu.mesh import (
    _PRO_ACCENT,
    _PRO_DPAD_SIDE,
    _PRO_DPAD_TOP,
    _PRO_HOLE,
    _MeshBuilder,
    _RimStyle,
    _add_capture_button,
    _add_disk,
    _add_round_button,
    _add_rounded_prism,
    _add_shell,
    _add_stick,
    _add_sweep,
    _add_torus,
    _cross_outline,
    _frame_z_to,
    _outline_rounded_rect,
)
from pokecon.domain.imu.models import (
    Color,
    MotionGroup,
    Vec2,
    Vec3,
    _button_spec,
    _dpad_spec,
    _mix_color,
    _normalize2,
    _normalize3,
    _scale_color,
    _smoothstep,
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
