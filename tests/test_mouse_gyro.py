# -*- coding: utf-8 -*-
"""マウス→ジャイロ変換の契約。

角度の合計が送ったrawの積分と一致すること（端数・飽和・繰り越し）、
軸と符号、無効時の静止、設定の丸めを実機なしで確かめる。
"""

from __future__ import annotations

import configparser
import math
from dataclasses import replace
from typing import Any

import pytest
from pokecon.domain.config import complete_missing
from pokecon.domain.mouse_capture import parse_hotkey
from pokecon.domain.mouse_gyro import (
    GYRO_DPS_PER_LSB,
    MouseGyroConfig,
    MouseGyroSource,
    calibration_from_settings,
    cm_per_360_for,
    config_from_settings,
    sensitivity_for_dpi,
)

STEP_DEG = 0.005 * GYRO_DPS_PER_LSB


def _source(**kwargs: Any) -> MouseGyroSource:
    config = MouseGyroConfig(**kwargs)
    source = MouseGyroSource(config)
    source.set_enabled(True)
    return source


def _drain(source: MouseGyroSource, count: int = 64) -> list[tuple[int, ...]]:
    return [source.read() for _ in range(count)]


def _sum_axis(samples: list[tuple[int, ...]], axis: int) -> float:
    return sum(sample[3 + axis] for sample in samples) * STEP_DEG


def test_total_yaw_angle_is_preserved() -> None:
    source = _source(sensitivity=0.05, backlog_deg=100.0, yaw_axis=2, yaw_sign=1)
    source.push_motion(1000, 0)
    samples = _drain(source)
    assert sum(sample[5] for sample in samples) > 0
    assert _sum_axis(samples, 2) == pytest.approx(50.0, abs=0.01)
    # 出し切ったあとは0へ戻る（最後の値を送り続けない）。
    assert samples[-1][5] == 0


def test_pitch_ratio_and_inversion() -> None:
    source = _source(
        sensitivity=0.05,
        ratio_y=2.0,
        invert_y=True,
        yaw_axis=0,
        yaw_sign=1,
        pitch_axis=2,
        pitch_sign=1,
    )
    source.push_motion(0, 100)
    samples = _drain(source)
    assert _sum_axis(samples, 2) == pytest.approx(-10.0, abs=0.01)


def test_axis_mapping_and_signs() -> None:
    source = _source(
        sensitivity=0.05,
        yaw_axis=0,
        yaw_sign=-1,
        pitch_axis=2,
        pitch_sign=1,
    )
    source.push_motion(100, 0)
    samples = _drain(source)
    assert _sum_axis(samples, 0) == pytest.approx(-5.0, abs=0.01)
    assert _sum_axis(samples, 1) == 0.0
    assert _sum_axis(samples, 2) == 0.0


def test_invert_x_flips_yaw() -> None:
    source = _source(sensitivity=0.05, invert_x=True, yaw_axis=2, yaw_sign=1)
    source.push_motion(100, 0)
    samples = _drain(source)
    assert _sum_axis(samples, 2) == pytest.approx(-5.0, abs=0.01)


def test_small_moves_are_quantized_without_drift() -> None:
    """1countずつでも、積分は要求角度から1サンプル分以上ずれない。"""
    source = _source(sensitivity=0.001, yaw_axis=2, yaw_sign=1)
    for _ in range(100):
        source.push_motion(1, 0)
    samples = _drain(source, 400)
    assert _sum_axis(samples, 2) == pytest.approx(0.1, abs=STEP_DEG + 1e-9)


def test_backlog_limits_fling_and_reports_dropped() -> None:
    source = _source(sensitivity=0.05, backlog_deg=1.0, yaw_axis=2, yaw_sign=1)
    source.push_motion(400, 0)  # 20°要求 → 1°だけ残す
    first = source.read()  # 上限は送出時に適用される
    stats = source.stats()
    assert stats["dropped_deg"] == pytest.approx(19.0, abs=0.01)
    samples = [first, *_drain(source, 8)]
    assert _sum_axis(samples, 2) == pytest.approx(1.0, abs=0.01)


def test_large_angle_saturates_then_catches_up() -> None:
    source = _source(sensitivity=0.05, backlog_deg=100.0, yaw_axis=2, yaw_sign=1)
    source.push_motion(800, 0)  # 40°
    samples = _drain(source, 8)
    assert samples[0][5] == 32767
    assert _sum_axis(samples, 2) == pytest.approx(40.0, abs=0.01)


def test_smoothing_spreads_output_over_samples() -> None:
    source = _source(
        sensitivity=0.05, backlog_deg=100.0, smoothing=0.25, yaw_axis=2, yaw_sign=1
    )
    source.push_motion(200, 0)  # 10°
    samples = _drain(source, 200)
    first = samples[0][5] * STEP_DEG
    assert 0 < first < 10.0
    assert _sum_axis(samples, 2) == pytest.approx(10.0, abs=0.01)


def test_disabled_source_ignores_motion_and_emits_rest() -> None:
    source = MouseGyroSource(MouseGyroConfig())
    source.push_motion(1000, 1000)
    assert source.read() == (0, 0, 4096, 0, 0, 0)
    source.set_enabled(True)
    assert source.read() == (0, 0, 4096, 0, 0, 0)
    assert source.stats()["counts_x"] == 0.0
    assert source.stats()["events"] == 0.0


def test_toggle_clears_backlog() -> None:
    source = _source(sensitivity=0.05, yaw_axis=2, yaw_sign=1)
    source.push_motion(1000, 0)
    source.set_enabled(False)
    source.set_enabled(True)
    assert all(sample[5] == 0 for sample in _drain(source, 4))


def test_rest_acceleration_is_output_verbatim() -> None:
    source = _source(accel=(10, -20, 4096))
    assert source.read()[:3] == (10, -20, 4096)


def test_config_missing_bools_use_defaults() -> None:
    config = config_from_settings({})
    assert config.invert_x is False
    assert config.invert_y is False
    assert config.gravity_follow is True


def test_config_from_settings_clamps_and_reads_values() -> None:
    bad = {
        "mouse_sensitivity": "-1",
        "mouse_ratio_y": "100",
        "mouse_yaw_axis": "9",
        "mouse_yaw_sign": "0",
        "mouse_pitch_axis": "-2",
        "mouse_pitch_sign": "2",
        "mouse_backlog_deg": "-5",
        "mouse_smoothing": "0",
        "mouse_deg_per_lsb": "1",
        "mouse_interval_ms": "500",
        "mouse_invert_x": "maybe",
        "mouse_invert_y": "on",
        "mouse_gravity_follow": "maybe",
        "mouse_pitch_limit_deg": "500",
        "mouse_roll_modifier": "weird",
        "mouse_roll_axis": "9",
        "mouse_roll_sign": "0",
        "mouse_reset_hotkey": "  ",
        "acc_z": "99999",
        "mouse_hotkey": "  ",
    }
    config = config_from_settings(bad)
    assert config.sensitivity == 0.05
    assert config.ratio_y == 1.0
    assert config.yaw_axis == 2
    assert config.yaw_sign == -1
    assert config.pitch_axis == 1
    assert config.pitch_sign == 1
    assert config.backlog_deg == 30.0
    assert config.smoothing == 1.0
    assert config.deg_per_lsb == GYRO_DPS_PER_LSB
    assert config.interval_s == 0.005
    assert config.invert_x is False
    assert config.invert_y is True
    assert config.gravity_follow is True
    assert config.pitch_limit_deg == 89.0
    assert config.roll_modifier == "none"
    assert config.roll_axis == 0
    assert config.roll_sign == 1
    assert config.accel == (0, 0, 4096)
    assert config.hotkey == "ctrl+alt+m"
    assert config.reset_hotkey == "ctrl+alt+r"

    good = {
        "mouse_sensitivity": "0.032",
        "mouse_ratio_y": "0.5",
        "mouse_yaw_axis": "0",
        "mouse_yaw_sign": "1",
        "mouse_pitch_axis": "2",
        "mouse_pitch_sign": "-1",
        "mouse_backlog_deg": "12.5",
        "mouse_smoothing": "0.5",
        "mouse_deg_per_lsb": "0.07",
        "mouse_interval_ms": "10",
        "mouse_invert_x": "1",
        "mouse_invert_y": "0",
        "mouse_gravity_follow": "0",
        "mouse_pitch_limit_deg": "45",
        "mouse_roll_modifier": "shift",
        "mouse_roll_axis": "2",
        "mouse_roll_sign": "-1",
        "mouse_reset_hotkey": "ctrl+shift+r",
        "acc_x": "1",
        "acc_y": "2",
        "acc_z": "3",
        "mouse_hotkey": "shift+f2",
    }
    config = config_from_settings(good)
    assert config.sensitivity == 0.032
    assert config.ratio_y == 0.5
    assert config.yaw_axis == 0
    assert config.yaw_sign == 1
    assert config.pitch_axis == 2
    assert config.pitch_sign == -1
    assert config.backlog_deg == 12.5
    assert config.smoothing == 0.5
    assert config.deg_per_lsb == 0.07
    assert config.interval_s == 0.010
    assert config.invert_x is True
    assert config.invert_y is False
    assert config.gravity_follow is False
    assert config.pitch_limit_deg == 45.0
    assert config.roll_modifier == "shift"
    assert config.roll_axis == 2
    assert config.roll_sign == -1
    assert config.accel == (1, 2, 3)
    assert config.hotkey == "shift+f2"
    assert config.reset_hotkey == "ctrl+shift+r"


def test_sensitivity_calibration_round_trip() -> None:
    sensitivity = sensitivity_for_dpi(800, 20.0)
    assert sensitivity == pytest.approx(0.05715, rel=1e-9)
    assert cm_per_360_for(sensitivity, 800) == pytest.approx(20.0, rel=1e-9)
    assert sensitivity_for_dpi(1600, 10.0) == pytest.approx(0.05715, rel=1e-9)
    assert sensitivity_for_dpi(0, 20.0) == 0.05
    assert sensitivity_for_dpi(800, 0.0) == 0.05
    assert cm_per_360_for(0.0, 800) is None
    assert cm_per_360_for(0.05, 0) is None


def test_calibration_from_settings_defaults_and_clamps() -> None:
    assert calibration_from_settings({}) == (800, 20.0)
    assert calibration_from_settings(
        {"mouse_dpi": "1600", "mouse_cm_per_360": "10"}
    ) == (
        1600,
        10.0,
    )
    assert calibration_from_settings({"mouse_dpi": "5", "mouse_cm_per_360": "500"}) == (
        800,
        20.0,
    )


def test_complete_missing_backfills_mouse_keys() -> None:
    parser = configparser.ConfigParser()
    parser.read_dict({"IMU": {"source": "mouse"}})
    changed = complete_missing(parser)
    assert "IMU.mouse_sensitivity" in changed
    assert parser["IMU"]["source"] == "mouse"
    assert parser["IMU"]["mouse_yaw_axis"] == "2"
    assert parser["IMU"]["mouse_gravity_follow"].strip().lower() in ("1", "true")
    assert parser["IMU"]["mouse_dpi"] == "800"
    assert parser["IMU"]["mouse_cm_per_360"] == "20.0"
    assert parser["IMU"]["mouse_pitch_limit_deg"] == "89.0"
    assert parser["IMU"]["mouse_roll_modifier"] == "none"
    assert parser["IMU"]["mouse_roll_axis"] == "0"
    assert parser["IMU"]["mouse_roll_sign"] == "1"
    assert parser["IMU"]["mouse_reset_hotkey"] == "ctrl+alt+r"
    assert parser["IMU"]["mouse_hotkey"] == "ctrl+alt+m"


def test_parse_hotkey() -> None:
    assert parse_hotkey("ctrl+alt+m") == (0x0002 | 0x0001, ord("M"))
    assert parse_hotkey("Shift+F1") == (0x0004, 0x70)
    assert parse_hotkey("ctrl + alt + q") == (0x0002 | 0x0001, ord("Q"))
    assert parse_hotkey("ctrl+alt+unknown") is None
    assert parse_hotkey("ctrl+alt+") is None
    assert parse_hotkey("m") is None
    assert parse_hotkey("") is None
    assert parse_hotkey("ctrl+alt+m+n") is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ((1, 0, 0), (1, 0, 0)),
        ((-2, 3, 4), (-2, 3, 4)),
        ((40000, -40000, 0), (32767, -32768, 0)),
    ],
)
def test_accel_is_clamped_to_int16(
    raw: tuple[int, int, int], expected: tuple[int, int, int]
) -> None:
    source = _source(accel=raw)
    assert source.read()[:3] == expected


def test_update_config_applies_to_next_sample() -> None:
    source = _source(sensitivity=0.05, yaw_axis=2, yaw_sign=1)
    source.push_motion(100, 0)
    assert _sum_axis(_drain(source, 4), 2) == pytest.approx(5.0, abs=0.01)

    source.update_config(replace(source.config, sensitivity=0.1))
    source.push_motion(100, 0)
    assert _sum_axis(_drain(source, 8), 2) == pytest.approx(10.0, abs=0.01)

    # 送出周期とホットキーは実行中に変えられない（次の開始で反映）。
    old_interval = source.config.interval_s
    source.update_config(
        replace(source.config, interval_s=0.02, hotkey="ctrl+shift+f1")
    )
    assert source.config.interval_s == old_interval
    assert source.config.hotkey == "ctrl+alt+m"


def test_pitch_rotates_gravity_vector() -> None:
    """ピッチ90°で重力が機体Xへ倒れる（ゲームの水平補正対策）。"""
    source = _source(
        sensitivity=0.05,
        backlog_deg=360.0,
        pitch_axis=1,
        pitch_sign=1,
    )
    source.push_motion(0, 900)  # 45°
    samples = _drain(source, 8)
    ax, ay, az = samples[-1][:3]
    assert ax == pytest.approx(-2896, abs=3)
    assert ay == 0
    assert az == pytest.approx(2896, abs=3)


def test_pitch_sign_flips_gravity_direction() -> None:
    source = _source(
        sensitivity=0.05,
        backlog_deg=360.0,
        pitch_axis=1,
        pitch_sign=-1,
    )
    source.push_motion(0, 900)
    samples = _drain(source, 8)
    assert samples[-1][0] == pytest.approx(2896, abs=3)


def test_gravity_follow_can_be_disabled() -> None:
    source = _source(
        sensitivity=0.05,
        backlog_deg=360.0,
        pitch_axis=1,
        pitch_sign=1,
        gravity_follow=False,
    )
    source.push_motion(0, 1800)
    samples = _drain(source, 16)
    assert samples[-1][:3] == (0, 0, 4096)


def test_yaw_does_not_tilt_gravity() -> None:
    source = _source(
        sensitivity=0.05,
        backlog_deg=360.0,
        yaw_axis=2,
        yaw_sign=1,
        pitch_axis=1,
        pitch_sign=1,
    )
    source.push_motion(1800, 0)  # ヨー90°
    samples = _drain(source, 16)
    assert samples[-1][:3] == (0, 0, 4096)


def test_gravity_holds_last_pitch_while_disabled() -> None:
    source = _source(
        sensitivity=0.05,
        backlog_deg=360.0,
        pitch_axis=1,
        pitch_sign=1,
    )
    source.push_motion(0, 900)
    _drain(source, 8)
    source.set_enabled(False)
    sample = source.read()
    assert sample[3:] == (0, 0, 0)
    assert sample[0] == pytest.approx(-2896, abs=3)


def test_stats_are_finite_and_signed() -> None:
    source = _source(sensitivity=0.05, yaw_axis=2, yaw_sign=1)
    source.push_motion(-100, 50)
    _drain(source, 8)
    stats = source.stats()
    assert stats["counts_x"] == -100.0
    assert stats["counts_y"] == 50.0
    assert stats["events"] == 1.0
    assert math.isfinite(stats["emitted_yaw_deg"])
    assert stats["emitted_yaw_deg"] < 0


def test_yaw_while_pitched_induces_body_roll() -> None:
    """ピッチ45°でヨーを振ると、機体X（ロール）にも成分が出る。"""
    source = _source(
        sensitivity=0.05,
        backlog_deg=360.0,
        yaw_axis=2,
        yaw_sign=1,
        pitch_axis=1,
        pitch_sign=1,
        smoothing=1.0,
    )
    source.push_motion(0, 900)  # ピッチ45°
    _drain(source, 8)
    source.push_motion(900, 0)  # ピッチした状態でヨー45°
    samples = _drain(source, 8)
    roll = _sum_axis(samples, 0)
    pitch = _sum_axis(samples, 1)
    yaw = _sum_axis(samples, 2)
    assert abs(roll) > 5.0
    assert abs(yaw) > 5.0
    assert abs(pitch) < 1.0
    assert abs(roll) == pytest.approx(abs(yaw), rel=0.15)


def test_pitch_is_limited_near_vertical() -> None:
    source = _source(
        sensitivity=0.05,
        backlog_deg=360.0,
        pitch_axis=1,
        pitch_sign=1,
        pitch_limit_deg=45.0,
    )
    source.push_motion(0, 10_000)  # 500°要求
    samples = _drain(source, 12)
    assert source.stats()["pitch_deg"] == pytest.approx(45.0)
    ax, ay, az = samples[-1][:3]
    assert ax == pytest.approx(-2896, abs=3)
    assert ay == 0
    assert az == pytest.approx(2896, abs=3)


def test_reset_orientation_returns_level_and_forward() -> None:
    source = _source(
        sensitivity=0.05,
        backlog_deg=360.0,
        pitch_axis=1,
        pitch_sign=1,
    )
    source.push_motion(900, 900)
    _drain(source, 16)
    source.reset_orientation()
    sample = source.read()
    assert sample[:3] == (0, 0, 4096)
    assert sample[3:] == (0, 0, 0)
    assert source.stats()["pitch_deg"] == 0.0


def test_roll_modifier_rotates_around_body_x() -> None:
    source = _source(
        sensitivity=0.05,
        backlog_deg=360.0,
        roll_axis=0,
        roll_sign=1,
    )
    source.push_motion(900, 0, roll=True)  # 45°
    samples = _drain(source, 8)
    assert abs(_sum_axis(samples, 0)) == pytest.approx(45.0, abs=0.1)
    assert _sum_axis(samples, 2) == pytest.approx(0.0, abs=0.1)
    ax, ay, az = samples[-1][:3]
    assert ax == 0
    assert ay == pytest.approx(2896, abs=3)
    assert az == pytest.approx(2896, abs=3)
