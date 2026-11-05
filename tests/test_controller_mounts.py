"""曲面へ取り付けた文字・スティックが、その面に追従することを検証する。"""

import math

import numpy as np
from pokecon.domain.controller_preview import CONTROL_NAMES, PreviewControls
from pokecon.ui.controller_mesh import (
    MotionGroup,
    _MeshBuilder,
    _add_stick,
    build_model,
)
from pokecon.ui.imu_preview_gpu import _group_transform


def test_button_glyphs_follow_the_button_face_normal() -> None:
    model = build_model(1)
    for spec in model.groups:
        rows = model.vertices[
            (model.vertices[:, 12] == float(spec.group))
            & (model.vertices[:, 10] >= 0.0)
        ]
        if rows.size:
            # 刻印面がキャップから浮かないよう、押し込み軸と垂直に貼る。
            facing = rows[:, 3:6] @ -np.asarray(spec.press_axis)
            np.testing.assert_allclose(facing, 1.0, atol=1e-5)


def test_stick_click_and_tilt_follow_the_installed_shaft() -> None:
    normal = np.array([0.3, 0.4, math.sqrt(0.75)])
    mesh = _MeshBuilder()
    _add_stick(
        mesh,
        0.2,
        -0.1,
        0.3,
        group=MotionGroup.STICK_L,
        stick="left",
        click="LCLICK",
        normal=(float(normal[0]), float(normal[1]), float(normal[2])),
    )
    spec = mesh.specs()[0]
    clicked, _ = _group_transform(
        spec,
        PreviewControls(buttons=1 << CONTROL_NAMES.index("LCLICK"), connected=True),
    )
    matrix = np.asarray(clicked).reshape(4, 4).T
    pivot = np.array([*spec.pivot, 1.0])
    displacement = (matrix @ pivot - pivot)[:3]
    np.testing.assert_allclose(displacement, -normal * spec.press_depth, atol=1e-8)

    tilted, _ = _group_transform(spec, PreviewControls(lx=1.0, connected=True))
    rotation = np.asarray(tilted).reshape(4, 4).T[:3, :3]
    # 最小回転で傾けた取付面の局所 +X。右倒しでこの接線側へ傾く。
    tangent = np.array([1.0, 0.0, 0.0]) - np.array(
        [normal[0] ** 2, normal[0] * normal[1], normal[0] * (1 + normal[2])]
    ) / (1 + normal[2])
    expected = normal * math.cos(spec.tilt_limit) + tangent * math.sin(spec.tilt_limit)
    np.testing.assert_allclose(rotation @ normal, expected, atol=1e-8)


def test_dpad_center_depression_is_not_occluded_by_a_flat_cap() -> None:
    from pokecon.ui.controller_mesh import _add_pro_dpad

    mesh = _MeshBuilder()
    _add_pro_dpad(mesh, (0.0, 0.0, 0.0), (0.0, 0.0, 1.0))
    rows = mesh.build()
    face = rows[(rows[:, 12] == float(MotionGroup.DPAD)) & (rows[:, 5] > 0.7)]
    radius = np.linalg.norm(face[:, :2], axis=1)
    span = max(np.ptp(face[:, 0]), np.ptp(face[:, 1]))
    center = face[radius < 1e-5, 2]
    rim = face[(radius > span * 0.11) & (radius < span * 0.15), 2]
    assert center.max() < rim.max() - 0.002
