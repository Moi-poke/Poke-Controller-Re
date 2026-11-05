"""実機・OpenGLなしで、回転時の欠面と可動グループの整合を検証する。"""

import numpy as np
import pytest
from pokecon.domain.controller_preview import CONTROL_NAMES, PreviewControls
from pokecon.ui.controller_mesh import (
    MOTION_GROUP_COUNT,
    MotionGroup,
    build_model,
)
from pokecon.ui.imu_preview_gpu import _group_transform


@pytest.mark.parametrize("model_id", [1, 2, 3])
def test_controller_surfaces_have_outward_unit_normals(model_id: int) -> None:
    vertices = build_model(model_id).vertices
    assert vertices.shape[1] == 13
    assert np.isfinite(vertices).all()
    normals = vertices[:, 3:6]
    np.testing.assert_allclose(np.linalg.norm(normals, axis=1), 1.0, atol=0.002)

    triangles = vertices.reshape(-1, 3, 13)
    geometric_normals = np.cross(
        triangles[:, 1, :3] - triangles[:, 0, :3],
        triangles[:, 2, :3] - triangles[:, 0, :3],
    )
    nondegenerate = np.linalg.norm(geometric_normals, axis=1) > 1e-10
    facing = np.einsum(
        "ij,ij->i",
        geometric_normals[nondegenerate],
        triangles[nondegenerate, :, 3:6].mean(axis=1),
    )
    assert np.all(facing >= -1e-8), "裏向きの三角形は背面カリングで欠面になる"


@pytest.mark.parametrize("model_id", [1, 2, 3])
def test_motion_group_column_is_integral_and_in_range(model_id: int) -> None:
    vertices = build_model(model_id).vertices
    groups = vertices[:, 12]
    assert np.all(groups == np.rint(groups))
    assert np.all(groups >= 0.0)
    assert np.all(groups < float(MOTION_GROUP_COUNT))
    assert np.any(groups == 0.0), "筐体（固定面）のグループ 0 が必要"


def _mask(*names: str) -> int:
    return sum(1 << CONTROL_NAMES.index(name) for name in names)


def _poses(model_id: int, controls: PreviewControls) -> dict[MotionGroup, np.ndarray]:
    return {
        spec.group: np.asarray(_group_transform(spec, controls)[0]).reshape(4, 4).T
        for spec in build_model(model_id).groups
    }


@pytest.mark.parametrize("model_id", [1, 2, 3])
def test_disconnected_input_releases_every_part(model_id: int) -> None:
    poses = _poses(
        model_id,
        PreviewControls(buttons=_mask("A", "UP", "LCLICK"), lx=1.0, connected=False),
    )
    for matrix in poses.values():
        np.testing.assert_array_equal(matrix, np.eye(4))


def test_left_stick_tilts_right_without_moving_right_stick() -> None:
    poses = _poses(1, PreviewControls(lx=1.0, connected=True))
    matrix = poses[MotionGroup.STICK_L]
    # 軸の先端が +X 側へ傾く。軸長は維持する。
    axis = matrix[:3, :3] @ np.array([0.0, 0.0, 1.0])
    assert axis[0] > 0.1
    assert axis[2] > 0.8
    np.testing.assert_allclose(np.linalg.norm(axis), 1.0)
    np.testing.assert_array_equal(poses[MotionGroup.STICK_R], np.eye(4))


def test_stick_click_preserves_tilt_and_depresses_the_pivot() -> None:
    model = build_model(1)
    spec = next(g for g in model.groups if g.group == MotionGroup.STICK_R)
    tilted = _poses(1, PreviewControls(rx=-0.8, ry=0.4, connected=True))[spec.group]
    clicked = _poses(
        1,
        PreviewControls(buttons=_mask("RCLICK"), rx=-0.8, ry=0.4, connected=True),
    )[spec.group]
    np.testing.assert_array_equal(clicked[:3, :3], tilted[:3, :3])
    pivot = np.array([*spec.pivot, 1.0])
    assert (clicked @ pivot)[2] < (tilted @ pivot)[2] - 0.001


def test_opposite_dpad_directions_cancel_but_up_tilts_front_down() -> None:
    model = build_model(1)
    spec = next(g for g in model.groups if g.group == MotionGroup.DPAD)
    opposed = _poses(
        1, PreviewControls(buttons=_mask("UP", "DOWN", "LEFT", "RIGHT"), connected=True)
    )
    np.testing.assert_array_equal(opposed[MotionGroup.DPAD], np.eye(4))
    up = _poses(1, PreviewControls(buttons=_mask("UP"), connected=True))[spec.group]
    front = np.array([spec.pivot[0], spec.pivot[1] + 0.1, spec.pivot[2], 1.0])
    assert (up @ front)[2] < front[2] - 0.001
