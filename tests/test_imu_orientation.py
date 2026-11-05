"""姿勢積分（imu_orientation）の解析ケース。Tk・実機・pygame不要。

座標系は実機IMUの右手系（X=前後/ロール、Y=左右/ピッチ、Z=上下/ヨー）。
世界座標は初期姿勢の機体座標（x=前, y=左, z=上）で、重力は -z。
"""

import math

import pytest
from pokecon.domain.imu_orientation import (
    ACCEL_LSB_PER_G,
    GYRO_DPS_PER_LSB,
    MAX_STEP_S,
    Orientation,
    relative_orientation,
)

DT = 0.005
_DEG_TO_RAD = math.pi / 180.0


def _qmul(
    a: tuple[float, float, float, float],
    b: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    aw, ax, ay, az = a
    bw, bx, by, bz = b
    return (
        aw * bw - ax * bx - ay * by - az * bz,
        aw * bx + ax * bw + ay * bz - az * by,
        aw * by - ax * bz + ay * bw + az * bx,
        aw * bz + ax * by - ay * bx + az * bw,
    )


def _rotate(
    q: tuple[float, float, float, float], v: tuple[float, float, float]
) -> tuple[float, float, float]:
    """機体座標のベクトルを世界座標へ（R(q)·v）。"""
    w, x, y, z = q
    return (
        (1 - 2 * (y * y + z * z)) * v[0]
        + 2 * (x * y - w * z) * v[1]
        + 2 * (x * z + w * y) * v[2],
        2 * (x * y + w * z) * v[0]
        + (1 - 2 * (x * x + z * z)) * v[1]
        + 2 * (y * z - w * x) * v[2],
        2 * (x * z - w * y) * v[0]
        + 2 * (y * z + w * x) * v[1]
        + (1 - 2 * (x * x + y * y)) * v[2],
    )


def _up_in_body(
    q: tuple[float, float, float, float],
) -> tuple[float, float, float]:
    """世界の上 +z を機体座標で見た方向（Rᵀ·e_z）。"""
    w, x, y, z = q
    return (
        2 * (x * z - w * y),
        2 * (y * z + w * x),
        1 - 2 * (x * x + y * y),
    )


def _angle(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
    dot = sum(p * q for p, q in zip(a, b))
    na = math.hypot(*a)
    nb = math.hypot(*b)
    cos = max(-1.0, min(1.0, dot / (na * nb)))
    return math.acos(cos)


def _raw_dps(dps: float) -> int:
    return round(dps / GYRO_DPS_PER_LSB)


def test_gyro_integrates_90_degrees_per_axis_with_correct_sign():
    """各軸 +90°/s を1秒。クォータニオンと軸写像・重力方向の符号を確認する。"""
    raw = _raw_dps(90.0)
    theta = raw * GYRO_DPS_PER_LSB * _DEG_TO_RAD
    half = 0.5 * theta
    expected = {
        0: (math.cos(half), math.sin(half), 0.0, 0.0),
        1: (math.cos(half), 0.0, math.sin(half), 0.0),
        2: (math.cos(half), 0.0, 0.0, math.sin(half)),
    }
    # +90°回した機体軸がどの世界軸へ行くか（右手系の符号確認）。
    # 生値の丸めで theta は厳密な90°ではないので、期待値も theta で作る。
    maps = {
        0: ((0.0, 1.0, 0.0), (0.0, math.cos(theta), math.sin(theta))),
        1: ((0.0, 0.0, 1.0), (math.sin(theta), 0.0, math.cos(theta))),
        2: ((1.0, 0.0, 0.0), (math.cos(theta), math.sin(theta), 0.0)),
    }
    # 静止時の加速度計が読む上方向（Rᵀ·e_z）。
    up = {
        0: (0.0, math.sin(theta), math.cos(theta)),
        1: (-math.sin(theta), 0.0, math.cos(theta)),
        2: (0.0, 0.0, 1.0),
    }

    for axis in range(3):
        orientation = Orientation()
        gyro = [0, 0, 0]
        gyro[axis] = raw
        sample = (0, 0, 0, gyro[0], gyro[1], gyro[2])
        for _ in range(200):  # 1秒
            orientation.update(sample, DT)

        q = orientation.quaternion
        assert q == pytest.approx(expected[axis], abs=1e-9)
        assert math.hypot(*q) == pytest.approx(1.0, abs=1e-12)
        src, dst = maps[axis]
        assert _rotate(q, src) == pytest.approx(dst, abs=1e-6)
        assert _up_in_body(q) == pytest.approx(up[axis], abs=1e-6)


@pytest.mark.parametrize(
    ("accel", "expected"),
    [
        # ロール +30°（静止時の上方向が body +Y 側へ傾く）
        (
            (0.0, 2048.0, 3547.0),
            (math.cos(math.radians(15)), math.sin(math.radians(15)), 0.0, 0.0),
        ),
        # ロール -30°
        (
            (0.0, -2048.0, 3547.0),
            (math.cos(math.radians(15)), -math.sin(math.radians(15)), 0.0, 0.0),
        ),
        # ピッチ +30°（body -X 側へ傾く）
        (
            (-2048.0, 0.0, 3547.0),
            (math.cos(math.radians(15)), 0.0, math.sin(math.radians(15)), 0.0),
        ),
        # ロール +90°（真横）
        (
            (0.0, 4096.0, 0.0),
            (math.cos(math.radians(45)), math.sin(math.radians(45)), 0.0, 0.0),
        ),
    ],
)
def test_stationary_tilt_converges(
    accel: tuple[float, float, float],
    expected: tuple[float, float, float, float],
) -> None:
    orientation = Orientation()
    sample = (round(accel[0]), round(accel[1]), round(accel[2]), 0, 0, 0)
    for _ in range(800):  # 4秒
        orientation.update(sample, DT)

    q = orientation.quaternion
    assert q == pytest.approx(expected, abs=0.004)
    assert math.hypot(*q) == pytest.approx(1.0, abs=1e-12)
    norm = math.hypot(*accel)
    measured = (accel[0] / norm, accel[1] / norm, accel[2] / norm)
    assert _angle(_up_in_body(q), measured) < math.radians(0.5)


def test_pure_roll_tilt_correction_keeps_yaw_composition():
    """純ロールの補正は body X 軸まわり。既存のヨー姿勢に後置きされる。"""
    orientation = Orientation()
    yaw_raw = _raw_dps(45.0)
    for _ in range(200):
        orientation.update((0, 0, 0, 0, 0, yaw_raw), DT)
    yaw_q = orientation.quaternion

    # 静止した30°ロールの加速度。補正は body X 軸まわりにだけ働く。
    for _ in range(800):
        orientation.update((0, 2048, 3547, 0, 0, 0), DT)

    tilt = math.atan2(2048.0, 3547.0)
    expected = _qmul(yaw_q, (math.cos(0.5 * tilt), math.sin(0.5 * tilt), 0.0, 0.0))
    assert orientation.quaternion == pytest.approx(expected, abs=0.005)


@pytest.mark.parametrize(
    "accel",
    [
        (0, 0, 0),  # ドロップアウト
        (0, 0, 2 * int(ACCEL_LSB_PER_G)),  # 2g
        (0, 0, int(ACCEL_LSB_PER_G) // 2),  # 0.5g
        (4096, 4096, 0),  # 1.41g
    ],
)
def test_non_gravity_accel_is_ignored(accel: tuple[int, int, int]) -> None:
    orientation = Orientation()
    for _ in range(200):
        orientation.update((*accel, 0, 0, 0), DT)
    assert orientation.quaternion == (1.0, 0.0, 0.0, 0.0)


def test_dynamic_accel_does_not_move_settled_tilt():
    orientation = Orientation()
    tilt = (0, 2048, 3547, 0, 0, 0)
    for _ in range(800):
        orientation.update(tilt, DT)
    settled = orientation.quaternion
    for _ in range(400):  # 2g を2秒
        orientation.update((0, 0, 2 * int(ACCEL_LSB_PER_G), 0, 0, 0), DT)
    assert orientation.quaternion == settled


def test_quaternion_stays_normalized_over_many_updates():
    orientation = Orientation()
    raw = _raw_dps(120.0)
    sample = (0, 0, int(ACCEL_LSB_PER_G), raw, -raw // 2, raw // 3)
    for _ in range(10000):
        orientation.update(sample, DT)

    q = orientation.quaternion
    assert math.hypot(*q) == pytest.approx(1.0, abs=1e-12)
    assert all(math.isfinite(v) for v in q)
    assert all(abs(v) <= 1.0 for v in q)


@pytest.mark.parametrize(
    "dt",
    [
        0.0,
        -DT,
        float("nan"),
        float("inf"),
        float("-inf"),
        MAX_STEP_S + 1e-9,
        1.0,
    ],
)
def test_invalid_or_gap_dt_freezes_orientation(dt: float) -> None:
    orientation = Orientation()
    sample = (0, 0, int(ACCEL_LSB_PER_G), 2000, -1000, 500)
    orientation.update(sample, DT)
    before = orientation.quaternion

    orientation.update(sample, dt)
    assert orientation.quaternion == before


def test_valid_dt_after_pause_resumes():
    orientation = Orientation()
    sample = (0, 0, int(ACCEL_LSB_PER_G), 2000, 0, 0)
    orientation.update(sample, DT)
    paused = orientation.quaternion

    orientation.update(sample, 5.0)  # ポーズ中は積分しない
    assert orientation.quaternion == paused

    orientation.update(sample, DT)  # 再開
    assert orientation.quaternion != paused


def test_max_step_is_accepted():
    orientation = Orientation()
    raw = _raw_dps(100.0)
    orientation.update((0, 0, 0, raw, 0, 0), MAX_STEP_S)
    theta = raw * GYRO_DPS_PER_LSB * _DEG_TO_RAD * MAX_STEP_S
    expected = (math.cos(0.5 * theta), math.sin(0.5 * theta), 0.0, 0.0)
    assert orientation.quaternion == pytest.approx(expected, abs=1e-12)


def test_reset_on_identity_is_exact_noop():
    orientation = Orientation()
    orientation.reset()
    assert orientation.quaternion == (1.0, 0.0, 0.0, 0.0)


def test_reset_zeroes_yaw_and_preserves_tilt():
    """reset はヨーだけ0へ。傾き（重力方向）は保持し、補正と喧嘩しない。"""
    orientation = Orientation()
    yaw_raw = _raw_dps(45.0)
    tilt_raw = _raw_dps(30.0)
    for _ in range(200):
        orientation.update((0, 0, 0, 0, 0, yaw_raw), DT)
    for _ in range(200):
        orientation.update((0, 0, 0, tilt_raw, 0, 0), DT)

    before_up = _up_in_body(orientation.quaternion)
    tilt = tilt_raw * GYRO_DPS_PER_LSB * _DEG_TO_RAD
    expected = (math.cos(0.5 * tilt), math.sin(0.5 * tilt), 0.0, 0.0)

    orientation.reset()

    q = orientation.quaternion
    assert q == pytest.approx(expected, abs=1e-9)
    assert math.hypot(*q) == pytest.approx(1.0, abs=1e-12)
    # 機体座標で見た重力方向は変わらない（補正の再学習が起きない）。
    assert _up_in_body(q) == pytest.approx(before_up, abs=1e-12)


def test_reset_keeps_settled_tilt_and_correction_continues():
    orientation = Orientation()
    sample = (0, 2048, 3547, 0, 0, 0)
    for _ in range(2000):
        orientation.update(sample, DT)
    settled = orientation.quaternion

    orientation.reset()  # ヨー0なので何もしない
    assert orientation.quaternion == settled

    for _ in range(100):
        orientation.update(sample, DT)
    assert orientation.quaternion == pytest.approx(settled, abs=1e-6)


@pytest.mark.parametrize(
    "reference",
    [(0.5, 0.5, 0.5, 0.5), (0.0, 1.0, 0.0, 0.0), (0.0, 0.0, 1.0, 0.0)],
)
def test_relative_orientation_recenters_tilt_and_heading(reference):
    """水平でない持ち方も、基準にした瞬間は完全に正面へ戻る。"""
    assert relative_orientation(reference, reference) == pytest.approx(
        (1.0, 0.0, 0.0, 0.0), abs=1e-12
    )


@pytest.mark.parametrize("axis", [0, 1, 2])
def test_relative_orientation_tracks_body_rotation_from_new_reference(axis):
    """基準を取り直しても、以後のロール・ピッチ・ヨーの軸を入れ替えない。"""
    reference = (0.5, 0.5, 0.5, 0.5)
    vector = [0.0, 0.0, 0.0]
    vector[axis] = math.sin(math.pi / 8.0)
    delta = (math.cos(math.pi / 8.0), *vector)
    current = _qmul(reference, delta)
    assert relative_orientation(current, reference) == pytest.approx(delta, abs=1e-12)
