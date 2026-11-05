"""IMU 3D プレビューの GPU レンダラー（moderngl / OpenGL 3.3 core）。

``imu_preview`` の子プロセスから、pygame が作った OpenGL コンテキスト上で
``controller_mesh.build_model()`` の三角形を描く。pygame の窓・イベント・
フォント初期化は呼び出し側の責任で、このモジュールは GPU 資源だけを扱う。

描画の流れ:

1. オフスクリーン（可能なら 4x MSAA）の FBO に背景とモデルを描く。
2. 色バッファをテクスチャへ解決し、画面いっぱいの四角形で既定 FBO へ出す。
3. 透明背景の pygame サーフェス（既存 UI）を上にアルファ合成する。

オフスクリーン経由にしているのは、pygame/SDL の既定 FBO がマルチサンプルか
どうかに左右されず、アンチエイリアスと画面全体の合成を一定にするため。
既定 FBO の読み戻し（スモークテストの ``screen.read``）にもそのまま映る。
"""

from __future__ import annotations

import math
import struct
import sys
from collections.abc import Sequence
from typing import Any, Final

from pokecon.domain.controller_preview import CONTROL_NAMES, PreviewControls
from pokecon.ui.controller_mesh import (
    ATLAS_CELL as _ATLAS_CELL,
    ATLAS_COLUMNS as _ATLAS_COLUMNS,
    ATLAS_LABELS as _ATLAS_LABELS,
    ATLAS_SIZE as _ATLAS_SIZE,
    MOTION_GROUP_COUNT as _MOTION_GROUP_COUNT,
    MotionGroupSpec,
    build_model,
)

# ---------------------------------------------------------------- 固定値


#: 中立時はボタン面を正面から見る。world は X=前、Y=左、Z=上。
#: +Z 側から見下ろし、+X を画面上に置くとモデルの左右・上下が一致する。
#: 画角 38°・距離 6.9 で、傾けた場合も操作面と上下の UI の間に収まる。
_EYE: Final = (0.0, 0.0, 6.9)
_TARGET: Final = (0.0, 0.0, 0.0)
_UP: Final = (1.0, 0.0, 0.0)
_FOV_Y_DEG: Final = 38.0
_NEAR: Final = 0.1
_FAR: Final = 30.0

#: 要求する MSAA サンプル数。GPU が対応していなければ 1x へ落とす。
_AA_SAMPLES: Final = 4

#: グリフ用フォント。日本語フォントを優先し、無ければ pygame 既定へ。
_FONT_CANDIDATES: Final = (
    "Meiryo UI",
    "Meiryo",
    "Yu Gothic UI",
    "Yu Gothic",
    "MS UI Gothic",
    "MS Gothic",
    "Noto Sans JP",
    "Segoe UI",
    "Arial",
)

#: 頂点 1 個あたりの float 数（位置3 + 法線3 + 色3 + 粗さ1 + UV2 + 可動グループ1）。
_VERTEX_FLOATS: Final = 13
_VERTEX_BYTES: Final = _VERTEX_FLOATS * 4
_VERTEX_FORMAT: Final = "3f 3f 3f 1f 2f 1f"

#: PreviewControls.buttons の名前 → ビット。並びは domain 側が正。
_CONTROL_BITS: Final[dict[str, int]] = {
    name: 1 << index for index, name in enumerate(CONTROL_NAMES)
}

# ---------------------------------------------------------------- シェーダ

_BACKGROUND_VERTEX: Final = """
#version 330 core
layout(location = 0) in vec2 in_position;
layout(location = 1) in vec2 in_uv;
out vec2 v_uv;

void main() {
    v_uv = in_uv;
    gl_Position = vec4(in_position, 0.0, 1.0);
}
"""

_BACKGROUND_FRAGMENT: Final = """
#version 330 core
in vec2 v_uv;
out vec4 frag_color;

uniform vec2 u_resolution;

void main() {
    float aspect = u_resolution.x / max(u_resolution.y, 1.0);
    vec3 top = vec3(0.53, 0.56, 0.60);
    vec3 bottom = vec3(0.38, 0.41, 0.46);
    vec3 color = mix(bottom, top, smoothstep(0.0, 1.0, v_uv.y));

    vec2 glow_p = vec2((v_uv.x - 0.5) * aspect, v_uv.y - 0.51);
    color += vec3(0.060, 0.060, 0.055) * exp(-dot(glow_p, glow_p) * 2.4);

    vec2 shadow_p = vec2((v_uv.x - 0.5) / 0.27, (v_uv.y - 0.30) / 0.060);
    color -= vec3(0.045, 0.045, 0.045) * exp(-dot(shadow_p, shadow_p));

    vec2 d = v_uv - 0.5;
    color *= 1.0 - 0.18 * dot(d, d);
    frag_color = vec4(color, 1.0);
}
"""

# モデルの x=右,y=前,z=上 を生IMUの X=前,Y=左,Z=上 へ変換してから姿勢を掛ける。
# 可動グループの剛体変換は、この座標変換より前にモデル局所座標で当てる。
_SCENE_VERTEX: Final = """
#version 330 core
layout(location = 0) in vec3 in_position;
layout(location = 1) in vec3 in_normal;
layout(location = 2) in vec3 in_color;
layout(location = 3) in float in_roughness;
layout(location = 4) in vec2 in_uv;
layout(location = 5) in float in_group;

uniform mat4 u_model;
uniform mat4 u_view;
uniform mat4 u_proj;
uniform mat4 u_group[__MOTION_GROUP_COUNT__];
uniform float u_tint[__MOTION_GROUP_COUNT__];
uniform float u_scale;

out vec3 v_world_pos;
out vec3 v_normal;
out vec3 v_color;
out float v_roughness;
out vec2 v_uv;
out float v_tint;

void main() {
    int motion_index = int(in_group + 0.5);
    vec3 local_position = in_position;
    vec3 local_normal = in_normal;
    if (motion_index != 0) {
        mat4 motion = u_group[motion_index];
        local_position = (motion * vec4(in_position, 1.0)).xyz;
        local_normal = mat3(motion) * in_normal;
    }

    vec3 raw = local_position * u_scale;
    vec3 p = vec3(raw.y, -raw.x, raw.z);
    vec3 n = vec3(local_normal.y, -local_normal.x, local_normal.z);

    vec4 world = u_model * vec4(p, 1.0);
    v_world_pos = world.xyz;
    v_normal = mat3(u_model) * n;
    v_color = in_color;
    v_roughness = in_roughness;
    v_uv = in_uv;
    v_tint = u_tint[motion_index];
    gl_Position = u_proj * u_view * world;
}
""".replace("__MOTION_GROUP_COUNT__", str(_MOTION_GROUP_COUNT))

_SCENE_FRAGMENT: Final = """
#version 330 core
in vec3 v_world_pos;
in vec3 v_normal;
in vec3 v_color;
in float v_roughness;
in vec2 v_uv;
in float v_tint;
out vec4 frag_color;

uniform sampler2D u_atlas;
uniform vec3 u_camera;
uniform vec3 u_key_dir;
uniform vec3 u_fill_dir;
uniform vec3 u_back_dir;
uniform float u_dim;

const vec3 KEY_COLOR = vec3(1.00, 0.975, 0.94);
const vec3 FILL_COLOR = vec3(0.55, 0.68, 0.92);
const vec3 RIM_COLOR = vec3(0.40, 0.54, 0.78);

void main() {
    vec3 N = normalize(v_normal);
    vec3 V = normalize(u_camera - v_world_pos);
    float rough = clamp(v_roughness, 0.03, 1.0);

    vec3 base = v_color;
    bool has_decal = v_uv.x >= 0.0 && v_uv.y >= 0.0;
    vec4 glyph = vec4(1.0);
    if (has_decal) {
        glyph = texture(u_atlas, v_uv);
    }

    vec3 albedo = pow(clamp(base, 0.0, 1.0), vec3(2.2));
    if (has_decal) {
        albedo = mix(albedo, vec3(1.0), glyph.a);
    }
    // Highlight only pressed moving surfaces; fixed parts carry v_tint = 0.
    float tint = clamp(v_tint, 0.0, 1.0);
    albedo = mix(albedo, vec3(0.16, 0.42, 0.90), 0.20 * tint);

    float n_dot_key = max(dot(N, u_key_dir), 0.0);
    float n_dot_fill = max(dot(N, u_fill_dir), 0.0);
    float hemi = 0.5 + 0.5 * N.z;
    vec3 ambient = mix(
        vec3(0.014, 0.017, 0.024),
        vec3(0.048, 0.057, 0.076),
        hemi
    );
    vec3 direct = KEY_COLOR * (0.92 * n_dot_key);
    direct += FILL_COLOR * (0.34 * n_dot_fill);

    vec3 H = normalize(u_key_dir + V);
    float shininess = mix(150.0, 10.0, smoothstep(0.12, 0.95, rough));
    float specular_strength = mix(0.50, 0.025, rough);
    float fresnel = 0.04 + 0.96 * pow(1.0 - max(dot(N, V), 0.0), 5.0);
    float spec = pow(max(dot(N, H), 0.0), shininess);
    vec3 specular = vec3(1.0, 0.98, 0.95) * spec
        * specular_strength * (0.55 + 0.45 * fresnel) * n_dot_key;

    vec3 Hf = normalize(u_fill_dir + V);
    float spec_fill = pow(max(dot(N, Hf), 0.0), mix(64.0, 5.0, rough));
    specular += FILL_COLOR * spec_fill * mix(0.14, 0.008, rough) * n_dot_fill;

    float rim_amount = pow(1.0 - max(dot(N, V), 0.0), 4.0);
    float back = max(dot(N, u_back_dir), 0.0);
    vec3 rim = RIM_COLOR * (rim_amount * (0.10 + 0.30 * back));

    vec3 color = albedo * (ambient + direct) + specular + rim * (0.35 + 0.65 * albedo);
    color *= clamp(u_dim, 0.0, 1.0);

    color = vec3(1.0) - exp(-max(color, 0.0) * 1.35);
    frag_color = vec4(pow(max(color, 0.0), vec3(1.0 / 2.2)), 1.0);
}
"""

_TEXTURE_VERTEX: Final = """
#version 330 core
layout(location = 0) in vec2 in_position;
layout(location = 1) in vec2 in_uv;
out vec2 v_uv;

void main() {
    v_uv = in_uv;
    gl_Position = vec4(in_position, 0.0, 1.0);
}
"""

_TEXTURE_FRAGMENT: Final = """
#version 330 core
in vec2 v_uv;
out vec4 frag_color;

uniform sampler2D u_texture;

void main() {
    frag_color = texture(u_texture, v_uv);
}
"""


# ---------------------------------------------------------------- 小さな数学


def _normalize(vector: tuple[float, float, float]) -> tuple[float, float, float]:
    length = math.sqrt(sum(value * value for value in vector))
    if length <= 1e-9:
        return (0.0, 0.0, 1.0)
    return (vector[0] / length, vector[1] / length, vector[2] / length)


def _perspective(
    fov_y_deg: float, aspect: float, near: float, far: float
) -> tuple[float, ...]:
    """列優先（GLSL の mat4 と同じ並び）の透視投影行列。"""
    f = 1.0 / math.tan(math.radians(fov_y_deg) * 0.5)
    depth = near - far
    return (
        f / aspect,
        0.0,
        0.0,
        0.0,
        0.0,
        f,
        0.0,
        0.0,
        0.0,
        0.0,
        (far + near) / depth,
        -1.0,
        0.0,
        0.0,
        (2.0 * far * near) / depth,
        0.0,
    )


def _look_at(
    eye: tuple[float, float, float],
    target: tuple[float, float, float],
    up: tuple[float, float, float],
) -> tuple[float, ...]:
    """列優先のビュー行列。"""
    forward = _normalize((target[0] - eye[0], target[1] - eye[1], target[2] - eye[2]))
    right = _normalize(
        (
            forward[1] * up[2] - forward[2] * up[1],
            forward[2] * up[0] - forward[0] * up[2],
            forward[0] * up[1] - forward[1] * up[0],
        )
    )
    up_view = (
        right[1] * forward[2] - right[2] * forward[1],
        right[2] * forward[0] - right[0] * forward[2],
        right[0] * forward[1] - right[1] * forward[0],
    )
    return (
        right[0],
        up_view[0],
        -forward[0],
        0.0,
        right[1],
        up_view[1],
        -forward[1],
        0.0,
        right[2],
        up_view[2],
        -forward[2],
        0.0,
        -(right[0] * eye[0] + right[1] * eye[1] + right[2] * eye[2]),
        -(up_view[0] * eye[0] + up_view[1] * eye[1] + up_view[2] * eye[2]),
        forward[0] * eye[0] + forward[1] * eye[1] + forward[2] * eye[2],
        1.0,
    )


def _quaternion_matrix(
    quaternion: tuple[float, float, float, float],
) -> tuple[float, ...]:
    """(w,x,y,z) を列優先の回転行列にする。"""
    w, x, y, z = (float(value) for value in quaternion)
    length = math.sqrt(w * w + x * x + y * y + z * z)
    if length <= 1e-9:
        w, x, y, z = 1.0, 0.0, 0.0, 0.0
    else:
        w /= length
        x /= length
        y /= length
        z /= length

    xx, yy, zz = x * x, y * y, z * z
    xy, xz, yz = x * y, x * z, y * z
    wx, wy, wz = w * x, w * y, w * z
    return (
        1.0 - 2.0 * (yy + zz),
        2.0 * (xy + wz),
        2.0 * (xz - wy),
        0.0,
        2.0 * (xy - wz),
        1.0 - 2.0 * (xx + zz),
        2.0 * (yz + wx),
        0.0,
        2.0 * (xz + wy),
        2.0 * (yz - wx),
        1.0 - 2.0 * (xx + yy),
        0.0,
        0.0,
        0.0,
        0.0,
        1.0,
    )


def _quad_vertices() -> tuple[float, ...]:
    """画面全体を覆う三角形ストリップ（位置 xy, UV）。"""
    return (
        -1.0,
        -1.0,
        0.0,
        0.0,
        1.0,
        -1.0,
        1.0,
        0.0,
        -1.0,
        1.0,
        0.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
    )


# ---------------------------------------------------------------- 可動グループ


_IDENTITY_MAT4: Final[tuple[float, ...]] = (
    1.0,
    0.0,
    0.0,
    0.0,
    0.0,
    1.0,
    0.0,
    0.0,
    0.0,
    0.0,
    1.0,
    0.0,
    0.0,
    0.0,
    0.0,
    1.0,
)
_IDENTITY_MAT3: Final[tuple[float, ...]] = (
    1.0,
    0.0,
    0.0,
    0.0,
    1.0,
    0.0,
    0.0,
    0.0,
    1.0,
)


def _mat3_multiply(a: Sequence[float], b: Sequence[float]) -> tuple[float, ...]:
    """行優先 3x3 の積 a*b。"""
    result = [0.0] * 9
    for row in range(3):
        for column in range(3):
            result[row * 3 + column] = sum(
                a[row * 3 + k] * b[k * 3 + column] for k in range(3)
            )
    return tuple(result)


def _mat3_axis_angle(
    axis: tuple[float, float, float], angle: float
) -> tuple[float, ...]:
    """行優先の Rodrigues 回転。"""
    x, y, z = axis
    length = math.sqrt(x * x + y * y + z * z)
    if length <= 1e-9:
        return _IDENTITY_MAT3
    x /= length
    y /= length
    z /= length
    c = math.cos(angle)
    s = math.sin(angle)
    t = 1.0 - c
    return (
        t * x * x + c,
        t * x * y - s * z,
        t * x * z + s * y,
        t * x * y + s * z,
        t * y * y + c,
        t * y * z - s * x,
        t * x * z - s * y,
        t * y * z + s * x,
        t * z * z + c,
    )


def _mat4_motion(
    pivot: tuple[float, float, float],
    rotation: Sequence[float],
    offset: tuple[float, float, float],
) -> tuple[float, ...]:
    """ピボット回転のあと、モデル座標で offset だけ平行移動する行列。"""
    # T(pivot + offset) * R * T(-pivot) を直接組み立てる。
    # 行優先の回転を列優先へ置き、平行移動は p - R*p + offset になる。
    r = rotation
    px, py, pz = pivot
    tx = px - (r[0] * px + r[1] * py + r[2] * pz) + offset[0]
    ty = py - (r[3] * px + r[4] * py + r[5] * pz) + offset[1]
    tz = pz - (r[6] * px + r[7] * py + r[8] * pz) + offset[2]
    return (
        r[0],
        r[3],
        r[6],
        0.0,
        r[1],
        r[4],
        r[7],
        0.0,
        r[2],
        r[5],
        r[8],
        0.0,
        tx,
        ty,
        tz,
        1.0,
    )


def _unit(value: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(number):
        return 0.0
    return -1.0 if number < -1.0 else 1.0 if number > 1.0 else number


def _group_transform(
    spec: MotionGroupSpec,
    controls: PreviewControls,
) -> tuple[tuple[float, ...], float]:
    """1 グループ分の変換行列と着色量を返す。中立なら恒等行列・0。"""
    if not controls.connected:
        return _IDENTITY_MAT4, 0.0

    buttons = int(controls.buttons)
    pressed = bool(
        spec.control is not None and buttons & _CONTROL_BITS.get(spec.control, 0)
    )
    rotation = _IDENTITY_MAT3
    depth = 0.0
    tint = 0.0

    if spec.dpad:
        up = bool(buttons & _CONTROL_BITS["UP"])
        down = bool(buttons & _CONTROL_BITS["DOWN"])
        left = bool(buttons & _CONTROL_BITS["LEFT"])
        right = bool(buttons & _CONTROL_BITS["RIGHT"])
        pitch = (float(down) - float(up)) * spec.tilt_limit
        yaw = (float(right) - float(left)) * spec.tilt_limit
        if pitch or yaw:
            rotation = _mat3_multiply(
                _mat3_axis_angle(spec.tilt_axes[1], yaw),
                _mat3_axis_angle(spec.tilt_axes[0], pitch),
            )
        if up or down or left or right:
            tint = 1.0
    elif spec.stick is not None:
        if spec.stick == "left":
            x_axis = _unit(controls.lx)
            y_axis = _unit(controls.ly)
        else:
            x_axis = _unit(controls.rx)
            y_axis = _unit(controls.ry)
        if x_axis or y_axis:
            yaw = x_axis * spec.tilt_limit
            # +Y（前）へ倒すには X 軸回転を負にする。
            pitch = -y_axis * spec.tilt_limit
            rotation = _mat3_multiply(
                _mat3_axis_angle(spec.tilt_axes[1], yaw),
                _mat3_axis_angle(spec.tilt_axes[0], pitch),
            )
        if pressed:
            depth = spec.press_depth
            tint = 1.0
    elif pressed:
        depth = spec.press_depth
        tint = 1.0

    if depth == 0.0 and rotation == _IDENTITY_MAT3:
        return _IDENTITY_MAT4, tint

    offset = (
        spec.press_axis[0] * depth,
        spec.press_axis[1] * depth,
        spec.press_axis[2] * depth,
    )
    return _mat4_motion(spec.pivot, rotation, offset), tint


# ---------------------------------------------------------------- アトラス


def _cell_rect(index: int, pygame: Any) -> Any:
    column = index % _ATLAS_COLUMNS
    row = index // _ATLAS_COLUMNS
    return pygame.Rect(
        column * _ATLAS_CELL,
        row * _ATLAS_CELL,
        _ATLAS_CELL,
        _ATLAS_CELL,
    )


def _find_font_path(pygame: Any) -> str | None:
    for name in _FONT_CANDIDATES:
        try:
            path = pygame.font.match_font(name)
        except Exception:
            path = None
        if path:
            return path
    return None


def _make_icon(pygame: Any, kind: str) -> Any:
    """HOME / CAP の単純な白いアイコン。2 倍で描いて縮小する。"""
    size = _ATLAS_CELL * 2
    icon = pygame.Surface((size, size), pygame.SRCALPHA)
    # 透明部分も白にしておく。ミップマップで縁が黒ずまない。
    icon.fill((255, 255, 255, 0))
    white = (255, 255, 255, 255)
    if kind == "HOME":
        pygame.draw.polygon(
            icon,
            white,
            [(64, 14), (14, 56), (24, 70), (64, 38), (104, 70), (114, 56)],
        )
        pygame.draw.rect(icon, white, (34, 56, 60, 56), border_radius=10)
        # ドアは透明に抜く。RGB は白のまま残す。
        pygame.draw.rect(icon, (255, 255, 255, 0), (55, 80, 18, 32), border_radius=4)
    else:
        pygame.draw.rect(icon, white, (28, 28, 72, 72), width=9, border_radius=20)
        pygame.draw.circle(icon, white, (64, 64), 19, width=9)
    return pygame.transform.smoothscale(icon, (_ATLAS_CELL - 8, _ATLAS_CELL - 8))


def _render_atlas(pygame: Any) -> Any:
    if not pygame.font.get_init():
        # フォントモジュールだけを用意する。ディスプレイやハードウェアは触らない。
        pygame.font.init()

    atlas = pygame.Surface((_ATLAS_SIZE, _ATLAS_SIZE), pygame.SRCALPHA)
    # 透明部分の RGB も白に統一する。alpha 合成時に暗いフリンジを出さない。
    atlas.fill((255, 255, 255, 0))

    path = _find_font_path(pygame)
    large = pygame.font.Font(path, 42) if path else pygame.font.Font(None, 50)
    small = pygame.font.Font(path, 22) if path else pygame.font.Font(None, 28)
    large.set_bold(True)
    small.set_bold(True)

    for index, label in enumerate(_ATLAS_LABELS):
        rect = _cell_rect(index, pygame)
        if label in ("HOME", "CAP"):
            image = _make_icon(pygame, label)
        elif label in ("SL", "SR"):
            image = small.render(label, True, (255, 255, 255))
        else:
            image = large.render(label, True, (255, 255, 255))
        atlas.blit(image, image.get_rect(center=rect.center))
    return atlas


# ---------------------------------------------------------------- GPU モデル


class _GpuModel:
    __slots__ = ("vao", "vbo", "vertices", "groups")

    def __init__(
        self,
        vao: Any,
        vbo: Any,
        vertices: int,
        groups: tuple[MotionGroupSpec, ...],
    ) -> None:
        self.vao = vao
        self.vbo = vbo
        self.vertices = vertices
        self.groups = groups

    def release(self) -> None:
        self.vao.release()
        self.vbo.release()


def _release(resource: Any) -> None:
    if resource is not None:
        resource.release()


# ---------------------------------------------------------------- 本体


class PreviewRenderer:
    """pygame の OpenGL コンテキストに同居する IMU プレビュー描画器。

    呼び出し側は ``pygame.display.set_mode(..., OPENGL | DOUBLEBUF)`` の後で
    インスタンスを作る。``render()`` の後に ``pygame.display.flip()`` を呼ぶ。
    ディスプレイやハードウェアは初期化しない（フォントモジュールだけは
    アトラス生成に必要なら初期化する）。
    """

    def __init__(self, pygame: Any, size: tuple[int, int]) -> None:
        try:
            import moderngl
        except ImportError as exc:
            raise RuntimeError(
                "moderngl が見つかりません（IMU 3Dプレビューに必要です）"
            ) from exc

        self._mgl = moderngl
        self._pygame = pygame
        self._size = (int(size[0]), int(size[1]))
        self._width, self._height = self._size
        self._models: dict[int, _GpuModel] = {}
        self._closed = False

        # close() を構築途中の失敗にも安全に使えるよう、先に全部 None にする。
        self._scene_prog: Any = None
        self._background_prog: Any = None
        self._texture_prog: Any = None
        self._background_vao: Any = None
        self._texture_vao: Any = None
        self._quad_vbo: Any = None
        self._atlas_texture: Any = None
        self._scene_texture: Any = None
        self._overlay_texture: Any = None
        self._overlay_size: tuple[int, int] | None = None
        self._scene_fbo: Any = None
        self._resolve_fbo: Any = None
        self._scene_color_rb: Any = None
        self._scene_depth_rb: Any = None
        self._scene_uniforms: dict[str, Any] = {}

        try:
            self._ctx: Any = moderngl.create_context(require=330)
        except Exception as exc:
            raise RuntimeError(
                f"OpenGL 3.3 コンテキストを初期化できません: {exc}"
            ) from exc

        try:
            self._build_resources()
        except BaseException:
            self.close()
            raise

    # ------------------------------------------------------------ 資源構築

    def _program(self, vertex: str, fragment: str) -> Any:
        try:
            return self._ctx.program(vertex_shader=vertex, fragment_shader=fragment)
        except Exception as exc:
            raise RuntimeError(f"シェーダのコンパイルに失敗しました: {exc}") from exc

    def _build_resources(self) -> None:
        ctx = self._ctx
        mgl = self._mgl

        self._scene_prog = self._program(_SCENE_VERTEX, _SCENE_FRAGMENT)
        self._background_prog = self._program(_BACKGROUND_VERTEX, _BACKGROUND_FRAGMENT)
        self._texture_prog = self._program(_TEXTURE_VERTEX, _TEXTURE_FRAGMENT)

        self._quad_vbo = ctx.buffer(struct.pack("16f", *_quad_vertices()))
        attributes = [
            (self._quad_vbo, "2f 2f", "in_position", "in_uv"),
        ]
        self._background_vao = ctx.vertex_array(self._background_prog, attributes)
        self._texture_vao = ctx.vertex_array(self._texture_prog, attributes)

        # 最初のモデルだけは起動時にアップロードして、初回フレームの待ちを避ける。
        self._model_for(1)

        self._atlas_texture = self._build_atlas_texture()
        self._build_render_targets()

        self._scene_uniforms = {
            name: self._scene_prog[name]
            for name in (
                "u_model",
                "u_view",
                "u_proj",
                "u_camera",
                "u_key_dir",
                "u_fill_dir",
                "u_back_dir",
                "u_atlas",
                "u_dim",
                "u_scale",
                "u_group",
                "u_tint",
            )
        }
        self._view_bytes = struct.pack("16f", *_look_at(_EYE, _TARGET, _UP))
        self._projection_bytes = struct.pack(
            "16f",
            *_perspective(
                _FOV_Y_DEG,
                self._width / max(self._height, 1),
                _NEAR,
                _FAR,
            ),
        )
        self._background_prog["u_resolution"].value = (
            float(self._width),
            float(self._height),
        )
        self._texture_prog["u_texture"].value = 0

        # 固定ライト。world 空間で動かさないので、姿勢に応じて陰影が変わる。
        self._key_dir = _normalize((-0.42, -0.52, 0.74))
        self._fill_dir = _normalize((0.55, 0.62, 0.18))
        self._back_dir = _normalize((0.62, 0.28, 0.58))

        # カメラとライトは固定。毎フレーム変わるモデル行列と dim だけ書く。
        uniforms = self._scene_uniforms
        uniforms["u_view"].write(self._view_bytes)
        uniforms["u_proj"].write(self._projection_bytes)
        uniforms["u_camera"].value = _EYE
        uniforms["u_key_dir"].value = self._key_dir
        uniforms["u_fill_dir"].value = self._fill_dir
        uniforms["u_back_dir"].value = self._back_dir
        uniforms["u_atlas"].value = 0

        ctx.disable(mgl.BLEND)
        ctx.enable(mgl.DEPTH_TEST)
        ctx.depth_func = "<"
        ctx.front_face = "ccw"
        ctx.cull_face = "back"

    def _build_atlas_texture(self) -> Any:
        mgl = self._mgl
        ctx = self._ctx
        surface = _render_atlas(self._pygame)
        # pygame は行 0 が上端。GL は下端が v=0 なので上下反転して渡す。
        data = self._pygame.image.tostring(surface, "RGBA", True)
        texture = ctx.texture((_ATLAS_SIZE, _ATLAS_SIZE), 4, data)
        texture.filter = (mgl.LINEAR_MIPMAP_LINEAR, mgl.LINEAR)
        texture.repeat_x = False
        texture.repeat_y = False
        texture.build_mipmaps()
        if "GL_EXT_texture_filter_anisotropic" in self._ctx.extensions:
            texture.anisotropy = 4.0
        return texture

    def _build_render_targets(self) -> None:
        mgl = self._mgl
        ctx = self._ctx
        raw_max = ctx.info.get("GL_MAX_SAMPLES")
        if raw_max is None:
            max_samples = _AA_SAMPLES
        else:
            try:
                max_samples = int(raw_max)
            except (TypeError, ValueError):
                max_samples = 0
        samples = min(_AA_SAMPLES, max_samples) if max_samples >= 2 else 0

        if samples >= 2:
            try:
                self._allocate_multisample(samples)
                return
            except mgl.Error as exc:
                # MSAA 非対応でも描画自体は続けられる。起動エラーにはしない。
                try:
                    print(
                        "IMUプレビュー: MSAA を確保できないため 1x で描画"
                        f"します: {exc}",
                        file=sys.stderr,
                    )
                except (AttributeError, OSError, ValueError):
                    pass
                self._release_targets()

        self._allocate_single_sample()

    def _allocate_multisample(self, samples: int) -> None:
        mgl = self._mgl
        ctx = self._ctx
        self._scene_color_rb = ctx.renderbuffer(self._size, 4, samples=samples)
        self._scene_depth_rb = ctx.depth_renderbuffer(self._size, samples=samples)
        self._scene_fbo = ctx.framebuffer([self._scene_color_rb], self._scene_depth_rb)

        self._scene_texture = ctx.texture(self._size, 4)
        self._scene_texture.filter = (mgl.NEAREST, mgl.NEAREST)
        self._scene_texture.repeat_x = False
        self._scene_texture.repeat_y = False
        self._resolve_fbo = ctx.framebuffer([self._scene_texture])

    def _allocate_single_sample(self) -> None:
        mgl = self._mgl
        ctx = self._ctx
        self._scene_texture = ctx.texture(self._size, 4)
        self._scene_texture.filter = (mgl.NEAREST, mgl.NEAREST)
        self._scene_texture.repeat_x = False
        self._scene_texture.repeat_y = False
        self._scene_depth_rb = ctx.depth_renderbuffer(self._size)
        self._scene_fbo = ctx.framebuffer([self._scene_texture], self._scene_depth_rb)

    # ------------------------------------------------------------ モデル

    def _model_for(self, model_id: int) -> _GpuModel:
        model = self._models.get(model_id)
        if model is not None:
            return model

        ctx = self._ctx
        built = build_model(model_id)
        mesh = built.vertices
        data = mesh.tobytes(order="C")
        vbo = ctx.buffer(data)
        try:
            vao = ctx.vertex_array(
                self._scene_prog,
                [
                    (
                        vbo,
                        _VERTEX_FORMAT,
                        "in_position",
                        "in_normal",
                        "in_color",
                        "in_roughness",
                        "in_uv",
                        "in_group",
                    )
                ],
            )
        except BaseException:
            vbo.release()
            raise
        model = _GpuModel(
            vao,
            vbo,
            len(data) // _VERTEX_BYTES,
            built.groups,
        )
        self._models[model_id] = model
        return model

    # ------------------------------------------------------------ 描画

    def _write_controls(
        self,
        model: _GpuModel,
        controls: PreviewControls,
    ) -> None:
        """可動グループの小さな変換行列と着色量だけを毎フレーム書く。"""
        matrices = [_IDENTITY_MAT4] * _MOTION_GROUP_COUNT
        tints = [0.0] * _MOTION_GROUP_COUNT
        for spec in model.groups:
            index = int(spec.group)
            if 0 <= index < _MOTION_GROUP_COUNT:
                matrices[index], tints[index] = _group_transform(spec, controls)
        uniforms = self._scene_uniforms
        uniforms["u_group"].write(
            struct.pack(
                f"{_MOTION_GROUP_COUNT * 16}f",
                *(value for matrix in matrices for value in matrix),
            )
        )
        uniforms["u_tint"].write(struct.pack(f"{_MOTION_GROUP_COUNT}f", *tints))

    def render(
        self,
        quaternion: tuple[float, float, float, float],
        model_id: int,
        dim: float,
        overlay: Any,
        controls: PreviewControls,
    ) -> None:
        """シーンと pygame オーバーレイを既定フレームバッファへ描く。

        ``overlay`` は透明背景の ``pygame.Surface``、``controls`` は
        そのフレームの物理操作状態。呼び出し側が ``pygame.display.flip()``
        を行う。
        """
        if self._closed:
            raise RuntimeError("PreviewRenderer はすでに close() されています")

        mgl = self._mgl
        ctx = self._ctx
        width, height = self._size

        # ---- 1. オフスクリーンへ背景とモデル
        self._scene_fbo.use()
        ctx.viewport = (0, 0, width, height)
        self._scene_fbo.clear(0.0, 0.0, 0.0, 1.0, depth=1.0)

        ctx.disable(mgl.BLEND | mgl.DEPTH_TEST | mgl.CULL_FACE)
        self._background_vao.render(mgl.TRIANGLE_STRIP, vertices=4)

        # メッシュは外側 CCW で作られる。裏面を落として深度ソートの負荷と
        # シーム内部のちらつきを抑える。
        ctx.enable(mgl.DEPTH_TEST | mgl.CULL_FACE)
        uniforms = self._scene_uniforms
        uniforms["u_model"].write(struct.pack("16f", *_quaternion_matrix(quaternion)))
        uniforms["u_dim"].value = max(0.0, min(1.0, float(dim)))
        uniforms["u_scale"].value = 1.08 if model_id == 1 else 1.40
        self._atlas_texture.use(location=0)

        model = self._model_for(int(model_id))
        self._write_controls(model, controls)
        model.vao.render(mgl.TRIANGLES, vertices=model.vertices)

        # ---- 2. MSAA を解決し、既定 FBO へ画面いっぱいに出す
        if self._resolve_fbo is not None:
            ctx.copy_framebuffer(self._resolve_fbo, self._scene_fbo)

        ctx.screen.use()
        ctx.viewport = (0, 0, width, height)
        ctx.disable(mgl.DEPTH_TEST | mgl.CULL_FACE | mgl.BLEND)
        self._scene_texture.use(location=0)
        self._texture_vao.render(mgl.TRIANGLE_STRIP, vertices=4)

        # ---- 3. 透明 UI をアルファ合成
        if overlay is not None:
            self._upload_overlay(overlay)
            if self._overlay_texture is not None:
                ctx.enable(mgl.BLEND)
                ctx.blend_func = (mgl.SRC_ALPHA, mgl.ONE_MINUS_SRC_ALPHA)
                self._overlay_texture.use(location=0)
                self._texture_vao.render(mgl.TRIANGLE_STRIP, vertices=4)
                ctx.disable(mgl.BLEND)

        # 深度テストは次の render() の先頭で再度有効にする。
        ctx.disable(mgl.DEPTH_TEST)

    def _upload_overlay(self, overlay: Any) -> None:
        pygame = self._pygame
        ctx = self._ctx
        size = (int(overlay.get_width()), int(overlay.get_height()))
        if size != self._overlay_size:
            _release(self._overlay_texture)
            mgl = self._mgl
            self._overlay_texture = ctx.texture(size, 4)
            self._overlay_texture.filter = (mgl.NEAREST, mgl.NEAREST)
            self._overlay_texture.repeat_x = False
            self._overlay_texture.repeat_y = False
            self._overlay_size = size

        # pygame の行 0 は上端。上下反転して GL の v=0（画面下端）に合わせる。
        data = pygame.image.tostring(overlay, "RGBA", True)
        self._overlay_texture.write(data)

    # ------------------------------------------------------------ 解放

    def close(self) -> None:
        """このインスタンスが作った GPU 資源を解放する（多重呼び出し可）。"""
        if self._closed:
            return
        self._closed = True

        for model in self._models.values():
            model.release()
        self._models.clear()

        _release(self._overlay_texture)
        self._overlay_texture = None
        self._overlay_size = None
        self._release_targets()
        _release(self._atlas_texture)
        self._atlas_texture = None

        _release(self._background_vao)
        self._background_vao = None
        _release(self._texture_vao)
        self._texture_vao = None
        _release(self._quad_vbo)
        self._quad_vbo = None

        _release(self._scene_prog)
        self._scene_prog = None
        _release(self._background_prog)
        self._background_prog = None
        _release(self._texture_prog)
        self._texture_prog = None
        self._scene_uniforms.clear()

        # 既定コンテキストは pygame のもの。release すると pygame 側の
        # 描画やスモークテストの screen.read まで壊れるので触らない。
        self._ctx = None

    def _release_targets(self) -> None:
        _release(self._resolve_fbo)
        self._resolve_fbo = None
        _release(self._scene_fbo)
        self._scene_fbo = None
        _release(self._scene_color_rb)
        self._scene_color_rb = None
        _release(self._scene_depth_rb)
        self._scene_depth_rb = None
        _release(self._scene_texture)
        self._scene_texture = None


__all__ = ["PreviewRenderer"]
