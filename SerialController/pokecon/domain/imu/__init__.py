"""IMU 3D プレビュー用コントローラーメッシュパッケージ。"""

from __future__ import annotations

from pokecon.domain.imu.mesh import (
    _MeshBuilder as _MeshBuilder,
    _add_stick as _add_stick,
    _build_joy,
    _fit_mesh,
    _fit_specs,
)
from pokecon.domain.imu.models import (
    ATLAS_CELL as ATLAS_CELL,
    ATLAS_COLUMNS as ATLAS_COLUMNS,
    ATLAS_LABELS as ATLAS_LABELS,
    ATLAS_SIZE as ATLAS_SIZE,
    DPAD_TILT_DEG as DPAD_TILT_DEG,
    MOTION_GROUP_COUNT as MOTION_GROUP_COUNT,
    STICK_TILT_DEG as STICK_TILT_DEG,
    ControllerModel as ControllerModel,
    MotionGroup as MotionGroup,
    MotionGroupSpec as MotionGroupSpec,
)
from pokecon.domain.imu.sdf import (
    _add_pro_dpad as _add_pro_dpad,
    _build_pro,
)


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
