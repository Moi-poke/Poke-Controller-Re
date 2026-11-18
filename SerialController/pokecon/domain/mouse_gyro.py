#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mouse_gyro.py - マウスの相対移動をジャイロの生値へ配分する（Tkなし）。

ホスト側のIMU送出ループ（BCONの ``T_IMU_SAMPLE``、5ms間隔）へ渡す
6値を、マウスの移動量から作る。ファームウェアは受け取った値を
そのまま0x30の3スロットへ載せるだけなので、角度の累積・配分は
すべてここで行う。

方式:
    マウスの移動量を「角度の増分」へ直し、まだ送っていない角度
    （``_angles``）へ足す。送出のたびにその角度をジャイロrawへ
    量子化し、出した分だけ ``_angles`` から引く。合計角度は
    「送ったraw×係数×周期」の積分と一致する（端数は次以降の
    サンプルへ繰り越す）。

軸:
    既定は機体座標の Z=ヨー（左右）・Y=ピッチ（上下）。
    ``imu_orientation`` の右手系（X=前後、Y=左右、Z=上下）に合わせ、
    右へのマウス移動が右向きのヨー（-Z）になる符号を既定にしている。
    実機の符号は個体・ゲームで変わりうるため、設定で反転できる。

端数について:
    計画書の ``residual`` は「角度の端数」を二重に持つことになり、
    1LSB未満の入力で ±1LSB の往復（ジッタ）を生む。ここでは
    ``_angles`` 自体が実数（度）なので、残差は持たずに
    量子化後の角度を引くだけで合計を保存する。残る誤差は
    1サンプル分の半LSB以下（0.06103dps/LSB×5ms/2≒0.00015°）。
"""

from __future__ import annotations

import math
import threading
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Final

from pokecon.domain.imu_orientation import GYRO_DPS_PER_LSB

#: int16へ収める。実際の±2000dpsもこの範囲に収まる。
RAW_MAX: Final[int] = 32767
#: 既定のマウス感度 [deg/count]。800DPIで約23cmで360°。
DEFAULT_SENSITIVITY: Final[float] = 0.05
#: 既定のキャプチャ解除ホットキー。
DEFAULT_HOTKEY: Final[str] = "ctrl+alt+m"
#: 既定の姿勢リセットホットキー。
DEFAULT_RESET_HOTKEY: Final[str] = "ctrl+alt+r"
#: 送出周期。BCONの0x30が5ms×3サンプルなので、これに合わせる。
DEFAULT_INTERVAL_S: Final[float] = 0.005
#: 繰り越し角度の既定上限 [度]。速いフリックで遅れて回り続けるのを防ぐ。
DEFAULT_BACKLOG_DEG: Final[float] = 30.0
#: ピッチの既定上限 [度]。真上・真下でヨー軸が定まらないのを避ける。
DEFAULT_PITCH_LIMIT_DEG: Final[float] = 89.0
#: ロール操作に使う修飾。none / right（右ボタン） / shift。
ROLL_MODIFIERS: Final[tuple[str, ...]] = ("none", "right", "shift")


@dataclass(frozen=True, slots=True)
class MouseGyroConfig:
    """マウス→ジャイロ変換の設定。値は検証済みで持つ。"""

    sensitivity: float = DEFAULT_SENSITIVITY
    ratio_y: float = 1.0
    invert_x: bool = False
    invert_y: bool = False
    yaw_axis: int = 2
    yaw_sign: int = -1
    pitch_axis: int = 1
    pitch_sign: int = 1
    backlog_deg: float = DEFAULT_BACKLOG_DEG
    smoothing: float = 1.0
    deg_per_lsb: float = GYRO_DPS_PER_LSB
    interval_s: float = DEFAULT_INTERVAL_S
    accel: tuple[int, int, int] = (0, 0, 4096)
    gravity_follow: bool = True
    pitch_limit_deg: float = DEFAULT_PITCH_LIMIT_DEG
    roll_modifier: str = "none"
    roll_axis: int = 0
    roll_sign: int = 1
    hotkey: str = DEFAULT_HOTKEY
    reset_hotkey: str = DEFAULT_RESET_HOTKEY


def _read_float(
    settings: Mapping[str, str], key: str, default: float, low: float, high: float
) -> float:
    """設定の実数を範囲内で読む。読めない・範囲外は既定。"""
    try:
        value = float(settings.get(key, ""))
    except (TypeError, ValueError):
        return default
    if not math.isfinite(value) or not low <= value <= high:
        return default
    return value


def _read_int(
    settings: Mapping[str, str], key: str, default: int, low: int, high: int
) -> int:
    """設定の整数を範囲内で読む。読めない・範囲外は既定。"""
    try:
        value = int(str(settings.get(key, "")).strip())
    except (TypeError, ValueError):
        return default
    return value if low <= value <= high else default


def _read_bool(settings: Mapping[str, str], key: str, default: bool) -> bool:
    """設定の真偽値を読む。空・読めない値は既定。"""
    text = str(settings.get(key, "")).strip().lower()
    if not text:
        return default
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off"):
        return False
    return default


def _read_sign(settings: Mapping[str, str], key: str, default: int) -> int:
    """符号（+1/-1）を読む。他の値は既定。"""
    value = _read_int(settings, key, default, -1, 1)
    return value if value in (-1, 1) else default


def _read_accel(settings: Mapping[str, str], key: str, default: int) -> int:
    """加速度の生値をint16で読む。"""
    return _read_int(settings, key, default, -32768, 32767)


def config_from_settings(settings: Mapping[str, str]) -> MouseGyroConfig:
    """IMU節の設定から変換設定を作る。不正値は安全な既定へ寄せる。"""
    interval_ms = _read_int(settings, "mouse_interval_ms", 5, 1, 20)
    hotkey = str(settings.get("mouse_hotkey", "") or "").strip() or DEFAULT_HOTKEY
    reset_hotkey = (
        str(settings.get("mouse_reset_hotkey", "") or "").strip()
        or DEFAULT_RESET_HOTKEY
    )
    roll_modifier = str(settings.get("mouse_roll_modifier", "") or "").strip().lower()
    if roll_modifier not in ROLL_MODIFIERS:
        roll_modifier = "none"
    return MouseGyroConfig(
        sensitivity=_read_float(
            settings, "mouse_sensitivity", DEFAULT_SENSITIVITY, 0.0001, 10.0
        ),
        ratio_y=_read_float(settings, "mouse_ratio_y", 1.0, 0.05, 10.0),
        invert_x=_read_bool(settings, "mouse_invert_x", False),
        invert_y=_read_bool(settings, "mouse_invert_y", False),
        yaw_axis=_read_int(settings, "mouse_yaw_axis", 2, 0, 2),
        yaw_sign=_read_sign(settings, "mouse_yaw_sign", -1),
        pitch_axis=_read_int(settings, "mouse_pitch_axis", 1, 0, 2),
        pitch_sign=_read_sign(settings, "mouse_pitch_sign", 1),
        backlog_deg=_read_float(
            settings, "mouse_backlog_deg", DEFAULT_BACKLOG_DEG, 0.0, 360.0
        ),
        smoothing=_read_float(settings, "mouse_smoothing", 1.0, 0.05, 1.0),
        deg_per_lsb=_read_float(
            settings, "mouse_deg_per_lsb", GYRO_DPS_PER_LSB, 0.01, 0.2
        ),
        interval_s=interval_ms / 1000.0,
        accel=(
            _read_accel(settings, "acc_x", 0),
            _read_accel(settings, "acc_y", 0),
            _read_accel(settings, "acc_z", 4096),
        ),
        gravity_follow=_read_bool(settings, "mouse_gravity_follow", True),
        pitch_limit_deg=_read_float(
            settings,
            "mouse_pitch_limit_deg",
            DEFAULT_PITCH_LIMIT_DEG,
            1.0,
            89.0,
        ),
        roll_modifier=roll_modifier,
        roll_axis=_read_int(settings, "mouse_roll_axis", 0, 0, 2),
        roll_sign=_read_sign(settings, "mouse_roll_sign", 1),
        hotkey=hotkey,
        reset_hotkey=reset_hotkey,
    )


def _round_half_away(value: float) -> int:
    """0.5を遠ざかる側へ丸める（Pythonのroundは偶数丸め）。"""
    if value >= 0:
        return int(math.floor(value + 0.5))
    return int(math.ceil(value - 0.5))


def sensitivity_for_dpi(dpi: float, cm_per_360: float) -> float:
    """DPIと「360°に必要な距離」から感度[deg/count]を求める。

    Raw InputのcountはマウスのDPIそのもの（Windowsのポインタ速度・加速は
    乗らない）。counts/360 = DPI/2.54 × cm なので、感度はその逆数。
    """
    if not (100.0 <= dpi <= 32000.0) or not (1.0 <= cm_per_360 <= 100.0):
        return DEFAULT_SENSITIVITY
    value = 360.0 * 2.54 / (dpi * cm_per_360)
    return min(10.0, max(0.0001, value))


def cm_per_360_for(sensitivity: float, dpi: float) -> float | None:
    """現在の感度とDPIで、マウス何cmで360°回るかを返す。"""
    if sensitivity <= 0.0 or dpi <= 0.0:
        return None
    value = 360.0 * 2.54 / (sensitivity * dpi)
    return value if math.isfinite(value) else None


def calibration_from_settings(
    settings: Mapping[str, str],
) -> tuple[int, float]:
    """感度補助のDPIと目標距離を読む。不正値は既定へ寄せる。"""
    dpi = _read_int(settings, "mouse_dpi", 800, 100, 32000)
    cm = _read_float(settings, "mouse_cm_per_360", 20.0, 1.0, 100.0)
    return (dpi, cm)


def _clamp_i16(value: int | float) -> int:
    """int16へ飽和させる。"""
    return max(-32768, min(32767, int(value)))


def _quat_mul(
    a: tuple[float, float, float, float],
    b: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    """クォータニオンの積（a→bの順に回す）。"""
    aw, ax, ay, az = a
    bw, bx, by, bz = b
    return (
        aw * bw - ax * bx - ay * by - az * bz,
        aw * bx + ax * bw + ay * bz - az * by,
        aw * by - ax * bz + ay * bw + az * bx,
        aw * bz + ax * by - ay * bx + az * bw,
    )


def _quat_normalize(
    q: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    """単位クォータニオンへ正規化する。"""
    w, x, y, z = q
    length = math.sqrt(w * w + x * x + y * y + z * z)
    if length < 1e-12:
        return (1.0, 0.0, 0.0, 0.0)
    return (w / length, x / length, y / length, z / length)


def _quat_from_rotation(
    rx: float, ry: float, rz: float
) -> tuple[float, float, float, float]:
    """回転ベクトル（軸×角度[rad]）からクォータニオンを作る。"""
    angle = math.sqrt(rx * rx + ry * ry + rz * rz)
    if angle < 1e-12:
        return (1.0, 0.0, 0.0, 0.0)
    half = angle * 0.5
    scale = math.sin(half) / angle
    return (math.cos(half), rx * scale, ry * scale, rz * scale)


def _rotate_by_quat(
    q: tuple[float, float, float, float],
    vector: tuple[float, float, float],
) -> tuple[float, float, float]:
    """ベクトルをクォータニオンqで回す（qは単位長）。"""
    w, x, y, z = q
    vx, vy, vz = vector
    tx = 2.0 * (y * vz - z * vy)
    ty = 2.0 * (z * vx - x * vz)
    tz = 2.0 * (x * vy - y * vx)
    return (
        vx + w * tx + (y * tz - z * ty),
        vy + w * ty + (z * tx - x * tz),
        vz + w * tz + (x * ty - y * tx),
    )


def _quat_conj(
    q: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    """共役（逆回転）。単位クォータニオン専用。"""
    return (q[0], -q[1], -q[2], -q[3])


def _rotation_vector(
    q: tuple[float, float, float, float],
) -> tuple[float, float, float]:
    """クォータニオンを最短の回転ベクトル（軸×角度[rad]）へ直す。"""
    w, x, y, z = q
    if w < 0.0:
        w, x, y, z = -w, -x, -y, -z
    sin_half = math.sqrt(x * x + y * y + z * z)
    if sin_half < 1e-12:
        return (0.0, 0.0, 0.0)
    angle = 2.0 * math.atan2(sin_half, w)
    scale = angle / sin_half
    return (x * scale, y * scale, z * scale)


def _axis_quat(index: int, angle_rad: float) -> tuple[float, float, float, float]:
    """機体/世界の1軸まわりの回転クォータニオン。"""
    vector = [0.0, 0.0, 0.0]
    vector[max(0, min(2, int(index)))] = float(angle_rad)
    return _quat_from_rotation(*vector)


class MouseGyroSource:
    """マウスの移動から仮想姿勢を作り、その差分を3軸ジャイロへ直す。

    書き込みは ``push_motion``（Raw Inputのスレッド）、読み出しは
    ``read``（IMU送出ループ）から来る。両方を1本のロックで保護する。
    ``read`` は6値 ``(ax, ay, az, gx, gy, gz)`` を返す。

    ``_target`` はマウス入力が要求する姿勢（機体→世界）、``_output`` は
    実際に送ったrawをゲームが積分した姿勢。差分 ``output^-1 * target`` を
    機体座標の回転ベクトルへ直し、5msあたりのrawへ量子化して送る。
    出し切れなかった分は次サンプルの差分に残るので、合計角度は保たれる。
    """

    def __init__(self, config: MouseGyroConfig) -> None:
        self._config = config
        self._lock = threading.Lock()
        self._enabled = False
        self._target: tuple[float, float, float, float] = (1.0, 0.0, 0.0, 0.0)
        self._output: tuple[float, float, float, float] = (1.0, 0.0, 0.0, 0.0)
        # ピッチ制御の累積角（度）。±limitで止める。
        self._pitch_deg = 0.0
        self._counts = [0, 0]
        self._emitted = [0.0, 0.0]
        self._dropped_deg = 0.0
        self._samples = 0
        self._events = 0

    @property
    def config(self) -> MouseGyroConfig:
        """現在の設定（開始時のスナップショット）。"""
        return self._config

    @property
    def interval_s(self) -> float:
        """送出周期 [秒]。"""
        return self._config.interval_s

    @property
    def orientation(self) -> tuple[float, float, float, float]:
        """ゲームが積分している姿勢（機体→世界）。診断用。"""
        with self._lock:
            return self._output

    def update_config(self, config: MouseGyroConfig) -> None:
        """送出中に設定を差し替える。次のサンプルから効く。

        周期・ホットキーは実行中に変えられない（次の開始で反映）。
        ピッチ軸・符号が変わったら、現在の姿勢から制御角を読み直す。
        """
        old = self._config
        if config.interval_s != old.interval_s:
            config = replace(config, interval_s=old.interval_s)
        if config.hotkey != old.hotkey:
            config = replace(config, hotkey=old.hotkey)
        if config.reset_hotkey != old.reset_hotkey:
            config = replace(config, reset_hotkey=old.reset_hotkey)
        with self._lock:
            self._config = config
            if (
                config.pitch_axis != old.pitch_axis
                or config.pitch_sign != old.pitch_sign
            ):
                self._pitch_deg = self._orientation_pitch_deg()
            limit = config.pitch_limit_deg
            self._pitch_deg = max(-limit, min(limit, self._pitch_deg))

    def set_enabled(self, enabled: bool) -> None:
        """入力を有効/無効にする。未送出の角度は捨て、姿勢は保持する。"""
        with self._lock:
            if bool(enabled) == self._enabled:
                return
            self._enabled = bool(enabled)
            # 再開時に古い未送出分が飛ばないよう、目標を出力へ合わせる。
            self._target = self._output
            if not self._enabled:
                limit = self._config.pitch_limit_deg
                self._pitch_deg = max(-limit, min(limit, self._orientation_pitch_deg()))

    def enabled(self) -> bool:
        """入力が有効か。"""
        with self._lock:
            return self._enabled

    def clear(self) -> None:
        """未送出の角度を捨てる（姿勢と累積統計は残す）。"""
        with self._lock:
            self._target = self._output

    def reset_orientation(self) -> None:
        """仮想姿勢を水平・正面へ戻す（重力も静止値へ戻る）。"""
        with self._lock:
            self._target = (1.0, 0.0, 0.0, 0.0)
            self._output = (1.0, 0.0, 0.0, 0.0)
            self._pitch_deg = 0.0

    def push_motion(self, dx: int, dy: int, roll: bool = False) -> None:
        """生の移動量 [count] を目標姿勢へ足す。無効時は無視。

        roll=True（キャプチャ側の修飾キー/右ボタン）のとき、dxはワールド
        ヨーではなく機体のロール軸へ回す。dyのピッチは常に有効。
        """
        if not dx and not dy:
            return
        try:
            dx_i = int(dx)
            dy_i = int(dy)
        except (TypeError, ValueError):
            return
        with self._lock:
            if not self._enabled:
                return
            self._events += 1
            cfg = self._config
            inv_x = -1.0 if cfg.invert_x else 1.0
            inv_y = -1.0 if cfg.invert_y else 1.0
            self._counts[0] += dx_i
            self._counts[1] += dy_i

            if dx_i:
                if roll:
                    roll_deg = dx_i * cfg.sensitivity * inv_x * cfg.roll_sign
                    q = _axis_quat(cfg.roll_axis, math.radians(roll_deg))
                    self._target = _quat_normalize(_quat_mul(self._target, q))
                else:
                    # マウスXはワールドの鉛直軸まわり（水平に振り向く）。
                    yaw_deg = dx_i * cfg.sensitivity * inv_x * cfg.yaw_sign
                    q = _axis_quat(cfg.yaw_axis, math.radians(yaw_deg))
                    self._target = _quat_normalize(_quat_mul(q, self._target))

            if dy_i:
                # マウスYは機体の横軸まわり。±limitで止める。
                pitch_delta = (
                    dy_i * cfg.sensitivity * cfg.ratio_y * inv_y * cfg.pitch_sign
                )
                limit = cfg.pitch_limit_deg
                new_pitch = max(-limit, min(limit, self._pitch_deg + pitch_delta))
                applied = new_pitch - self._pitch_deg
                self._pitch_deg = new_pitch
                if applied:
                    q = _axis_quat(cfg.pitch_axis, math.radians(applied))
                    self._target = _quat_normalize(_quat_mul(self._target, q))

    def _orientation_pitch_deg(self) -> float:
        """現在の姿勢の前後傾き（度）。ピッチ制限の再同期用。"""
        forward = _rotate_by_quat(self._output, (1.0, 0.0, 0.0))
        return math.degrees(math.atan2(forward[2], math.hypot(forward[0], forward[1])))

    def _remaining_rotation(self) -> tuple[float, float, float]:
        """出力姿勢から目標姿勢への機体座標回転ベクトル[rad]。"""
        return _rotation_vector(_quat_mul(_quat_conj(self._output), self._target))

    def _accel_for_config(self, cfg: MouseGyroConfig) -> tuple[int, int, int]:
        """姿勢に追従した加速度を返す。

        ゲームは加速度を「どちらが上か」の観測として使い、ピッチを水平へ
        戻そうとする。固定重力のままだと上下の照準が戻るため、ゲームが
        積分している姿勢の逆回転で重力ベクトルを機体座標へ移す。
        """
        if not cfg.gravity_follow:
            return (
                _clamp_i16(cfg.accel[0]),
                _clamp_i16(cfg.accel[1]),
                _clamp_i16(cfg.accel[2]),
            )
        w, x, y, z = self._output
        rx, ry, rz = _rotate_by_quat(
            (w, -x, -y, -z),
            (float(cfg.accel[0]), float(cfg.accel[1]), float(cfg.accel[2])),
        )
        return (_clamp_i16(round(rx)), _clamp_i16(round(ry)), _clamp_i16(round(rz)))

    def read(self) -> tuple[int, int, int, int, int, int]:
        """1サンプル分の6値を返す。無効時はジャイロ0（加速度は姿勢を維持）。"""
        gyro = [0, 0, 0]
        with self._lock:
            self._samples += 1
            cfg = self._config
            if self._enabled:
                dt = cfg.interval_s
                step_rad = dt * math.radians(cfg.deg_per_lsb)
                rotation = self._remaining_rotation()
                mag = math.sqrt(
                    rotation[0] * rotation[0]
                    + rotation[1] * rotation[1]
                    + rotation[2] * rotation[2]
                )
                cap = math.radians(cfg.backlog_deg)
                if cap <= 0.0:
                    if mag:
                        self._dropped_deg += math.degrees(mag)
                        rotation = (0.0, 0.0, 0.0)
                        mag = 0.0
                        self._target = self._output
                elif mag > cap:
                    # 出し切れない分は捨てる（目標も切り詰める）。
                    self._dropped_deg += math.degrees(mag - cap)
                    scale = cap / mag
                    rotation = (
                        rotation[0] * scale,
                        rotation[1] * scale,
                        rotation[2] * scale,
                    )
                    self._target = _quat_normalize(
                        _quat_mul(self._output, _quat_from_rotation(*rotation))
                    )
                    mag = cap
                if mag > 0.0:
                    target = (
                        rotation[0] * cfg.smoothing,
                        rotation[1] * cfg.smoothing,
                        rotation[2] * cfg.smoothing,
                    )
                    raw = [
                        max(
                            -RAW_MAX,
                            min(
                                RAW_MAX,
                                _round_half_away(target[i] / step_rad),
                            ),
                        )
                        for i in range(3)
                    ]
                    applied = (
                        raw[0] * step_rad,
                        raw[1] * step_rad,
                        raw[2] * step_rad,
                    )
                    before = self._output
                    self._output = _quat_normalize(
                        _quat_mul(before, _quat_from_rotation(*applied))
                    )
                    world = _rotate_by_quat(before, applied)
                    self._emitted[0] += math.degrees(world[cfg.yaw_axis])
                    self._emitted[1] += math.degrees(applied[cfg.pitch_axis])
                    gyro = raw
            ax, ay, az = self._accel_for_config(cfg)
        return (
            ax,
            ay,
            az,
            _clamp_i16(gyro[0]),
            _clamp_i16(gyro[1]),
            _clamp_i16(gyro[2]),
        )

    def stats(self) -> dict[str, float]:
        """表示用の累積値。"""
        with self._lock:
            cfg = self._config
            pending = self._remaining_rotation() if self._enabled else (0.0, 0.0, 0.0)
            pending_mag = math.sqrt(
                pending[0] * pending[0]
                + pending[1] * pending[1]
                + pending[2] * pending[2]
            )
            world = _rotate_by_quat(self._output, pending)
            return {
                "counts_x": float(self._counts[0]),
                "counts_y": float(self._counts[1]),
                "emitted_yaw_deg": self._emitted[0],
                "emitted_pitch_deg": self._emitted[1],
                "dropped_deg": self._dropped_deg,
                "samples": float(self._samples),
                "events": float(self._events),
                "backlog_yaw_deg": math.degrees(world[cfg.yaw_axis]),
                "backlog_pitch_deg": math.degrees(pending[cfg.pitch_axis]),
                "pending_deg": math.degrees(pending_mag),
                "pitch_deg": self._pitch_deg,
            }
