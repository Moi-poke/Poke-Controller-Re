#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""imu_orientation.py - 6軸IMUから姿勢クォータニオンを積分する（Tkなし）。

Picoへ送る前の生サンプル (ax, ay, az, gx, gy, gz) をそのまま受け取り、
機体→世界の姿勢をスカラー先頭クォータニオン (w, x, y, z) で保つ。
``update`` は送出ループから5msごとに呼ばれる想定。

軸（実機IMUの右手系。X × Y = Z）:
    X=前後（ロール）, Y=左右（ピッチ）, Z=上下（ヨー）。
    世界座標は初期姿勢の機体座標（x=前, y=左, z=上）で、上は +z、
    重力加速度は -z。初期姿勢は単位クォータニオン＝水平・ヨー0。
    表示側で x=右 / y=前 / z=上 のモデル座標へ置くときは、
    body X→model +Y、body Y→model -X、body Z→model +Z の固定変換だけを
    行う（入力・送出の軸は変えない）。符号の実機確認は未実施
    （GyroView の軸対応コメント準拠）。

単位:
    ジャイロ 0.06103 deg/s/LSB（機体座標の角速度として指数写像で積分）、
    加速度 4096/g。積分後は毎回正規化する。

加速度:
    静止時の加速度計は上向きの支持力を測るので、u_meas = a/|a| が
    機体座標での上方向。重力方向に直交する最短回転だけを補正し、
    絶対方位は観測しない（ヨーはジャイロ積分だけで決まる）。
    |a| が 1g±GRAVITY_TOLERANCE_G の外（落下・加減速）とゼロノルム、
    傾きがほぼ180°で回転軸が定まらない場合は補正しない。

dt:
    0以下・非有限・MAX_STEP_S 超はポーズ（サンプル欠落）とみなし、
    姿勢を変えない。停止明けの巨大な1ステップで姿勢が飛ぶのを防ぐ。

reset:
    ヨー基準だけを初期化する（世界z軸まわりの回転を0へ戻す）。傾きは保持し、
    加速度補正をやり直さない（補正と喧嘩しない）。
"""

from __future__ import annotations

import math
from typing import Final

#: ジャイロ生値 1LSB あたりの角速度 [deg/s]（2000dps レンジ = 2000/32768）。
GYRO_DPS_PER_LSB: Final[float] = 0.06103
#: 加速度生値 1g あたりのカウント。
ACCEL_LSB_PER_G: Final[float] = 4096.0
#: 1サンプルとして受け付ける最大 dt [s]。5ms の20倍。これを超える穴は
#: 積分せずに捨てる（停止明けの姿勢飛びを防ぐ）。
MAX_STEP_S: Final[float] = 0.1
#: 傾き補正の比例ゲイン [1/s]。誤差角に対して毎秒この割合だけ詰める。
TILT_GAIN_PER_S: Final[float] = 2.0
#: 重力ノルムの許容幅 [g]。1±この値の外は動的加速度とみなして補正しない。
GRAVITY_TOLERANCE_G: Final[float] = 0.2

_DEG_TO_RAD: Final[float] = math.pi / 180.0
_EPS: Final[float] = 1e-12
_IDENTITY: Final[tuple[float, float, float, float]] = (1.0, 0.0, 0.0, 0.0)


class Orientation:
    """6軸IMUの姿勢（機体→世界、スカラー先頭クォータニオン）。

    ``update`` をサンプルごとに呼ぶ。``quaternion`` は正規化済みの
    ``(w, x, y, z)`` を返す。スレッド安全ではないので、書き込みは
    送出ループの1スレッドだけが行い、表示側は値を受け渡して読むこと。
    ``reset`` はヨーだけを0へ戻し、傾き（重力方向）は保持する。
    """

    def __init__(self) -> None:
        self._q: tuple[float, float, float, float] = _IDENTITY

    @property
    def quaternion(self) -> tuple[float, float, float, float]:
        """機体→世界の姿勢。スカラー先頭 ``(w, x, y, z)``、正規化済み。"""
        return self._q

    def reset(self) -> None:
        """ヨー基準を0へ戻す（傾きは保持し、加速度補正はそのまま続く）。"""
        w, x, y, z = self._q
        # ZYX 分解のヨー角（世界z軸まわり）。機体X軸の水平投影から求める。
        yaw = math.atan2(2.0 * (w * z + x * y), 1.0 - 2.0 * (y * y + z * z))
        if yaw == 0.0:
            return
        half = -0.5 * yaw
        qz = (math.cos(half), 0.0, 0.0, math.sin(half))
        self._q = _normalize(_mul(qz, self._q))

    def update(self, sample: tuple[int, int, int, int, int, int], dt: float) -> None:
        """1サンプルぶん姿勢を進める。

        ``sample`` は生の ``(ax, ay, az, gx, gy, gz)``。``dt`` が不正、
        または ``MAX_STEP_S`` を超える穴なら何もしない（ポーズ）。
        """
        try:
            ax, ay, az, gx, gy, gz = (float(v) for v in sample)
            dtf = float(dt)
        except (TypeError, ValueError):
            return
        if not all(math.isfinite(v) for v in (ax, ay, az, gx, gy, gz, dtf)):
            return
        if dtf <= 0.0 or dtf > MAX_STEP_S:
            return

        # ジャイロ積分（q ← q ⊗ exp(ω·dt/2)）。0なら正規化の揺らぎも避ける。
        if gx != 0.0 or gy != 0.0 or gz != 0.0:
            wx = gx * GYRO_DPS_PER_LSB * _DEG_TO_RAD
            wy = gy * GYRO_DPS_PER_LSB * _DEG_TO_RAD
            wz = gz * GYRO_DPS_PER_LSB * _DEG_TO_RAD
            self._q = _normalize(_mul(self._q, _exp_map(wx, wy, wz, dtf)))

        # 加速度による傾き補正。重力ノルムが1g近傍のときだけ効かせる。
        norm = math.hypot(ax, ay, az)
        if norm <= _EPS:
            return
        if abs(norm / ACCEL_LSB_PER_G - 1.0) > GRAVITY_TOLERANCE_G:
            return

        ux, uy, uz = ax / norm, ay / norm, az / norm
        px, py, pz = _up_in_body(self._q)
        ex = uy * pz - uz * py
        ey = uz * px - ux * pz
        ez = ux * py - uy * px
        sin_a = math.hypot(ex, ey, ez)
        if sin_a <= _EPS:
            # 既に一致、または真逆（軸が定まらない）。
            return
        cos_a = ux * px + uy * py + uz * pz
        angle = math.atan2(sin_a, cos_a)
        step = min(1.0, TILT_GAIN_PER_S * dtf) * angle
        inv = 1.0 / sin_a
        delta = _axis_angle_quat(ex * inv, ey * inv, ez * inv, step)
        self._q = _normalize(_mul(self._q, delta))


def relative_orientation(
    current: tuple[float, float, float, float],
    reference: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    """基準姿勢からの回転を返す。積分状態や送出値は変更しない。"""
    w, x, y, z = reference
    return _normalize(_mul((w, -x, -y, -z), current))


def _mul(
    a: tuple[float, float, float, float],
    b: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    """ハミルトン積 a ⊗ b（スカラー先頭）。"""
    aw, ax, ay, az = a
    bw, bx, by, bz = b
    return (
        aw * bw - ax * bx - ay * by - az * bz,
        aw * bx + ax * bw + ay * bz - az * by,
        aw * by - ax * bz + ay * bw + az * bx,
        aw * bz + ax * by - ay * bx + az * bw,
    )


def _normalize(
    q: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    """正規化する。壊れた値なら単位クォータニオンへ戻す。"""
    w, x, y, z = q
    n = math.hypot(w, x, y, z)
    if not math.isfinite(n) or n <= _EPS:
        return _IDENTITY
    inv = 1.0 / n
    return (w * inv, x * inv, y * inv, z * inv)


def _exp_map(
    wx: float, wy: float, wz: float, dt: float
) -> tuple[float, float, float, float]:
    """角速度 ω [rad/s] を dt 秒積分した回転クォータニオン。"""
    speed = math.hypot(wx, wy, wz)
    if speed <= _EPS:
        return _IDENTITY
    half = 0.5 * speed * dt
    k = math.sin(half) / speed
    return (math.cos(half), wx * k, wy * k, wz * k)


def _axis_angle_quat(
    ax: float, ay: float, az: float, angle: float
) -> tuple[float, float, float, float]:
    """単位軸 (ax, ay, az) まわりの angle [rad] 回転クォータニオン。"""
    half = 0.5 * angle
    s = math.sin(half)
    return (math.cos(half), ax * s, ay * s, az * s)


def _up_in_body(
    q: tuple[float, float, float, float],
) -> tuple[float, float, float]:
    """q の姿勢で見た世界の上方向 +z を機体座標で返す（Rᵀ·e_z）。"""
    w, x, y, z = q
    return (
        2.0 * (x * z - w * y),
        2.0 * (y * z + w * x),
        1.0 - 2.0 * (x * x + y * y),
    )
