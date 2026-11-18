#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""imu_service.py - ジャイロ送出の入力源構築・軸変換・波形トリムを担う runtime 層。

tkinter を一切触らず、入力源（HID直結 / SDL / マウス / 記録再生 / 波形）の
構築・加工値計算・記録/再生ライフサイクルを受け持つ。
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

# 通常運用では使わない入力源。デバッグ時だけ選べる。
DEBUG_SOURCES = frozenset({"sine", "step", "rest", "manual"})

SOURCE_LABELS: tuple[tuple[str, str], ...] = (
    ("sensor", "パッドAPI"),
    ("hid", "HID直結"),
    ("mouse", "マウス"),
    ("file", "記録の再生"),
    ("sine", "SINE波形"),
    ("step", "STEP波形"),
    ("rest", "静止値"),
    ("manual", "手動入力"),
)

# PC 側で重ねる反転の組み合わせ（FW 側の変換を打ち消す実測用）。
# 値は (accel_signs, gyro_signs)。符号は 1 か -1 のみ。
INVERT_LABELS: tuple[tuple[str, str], ...] = (
    ("none", "なし"),
    ("accel", "加速度"),
    ("gyro", "ジャイロ"),
    ("both", "両方"),
)

# マウスのロール操作に使う修飾キー。
ROLL_LABELS: tuple[tuple[str, str], ...] = (
    ("none", "なし"),
    ("right", "右ボタン"),
    ("shift", "Shift"),
)

INVERT_SIGNS: dict[str, tuple[tuple[int, int, int], tuple[int, int, int]]] = {
    "none": ((1, 1, 1), (1, 1, 1)),
    "accel": ((1, -1, -1), (1, 1, 1)),
    "gyro": ((1, 1, 1), (1, -1, -1)),
    "both": ((1, -1, -1), (1, -1, -1)),
}

# 設定ファイルの符号がプリセットのどれとも一致しないときの表示。
# この表示のまま送出を開始しても、符号を書き換えない。
CURRENT_INVERT_LABEL = "設定値のまま"

# ジャイロ出力倍率のプリセット（実機比較用）。
SCALE_PRESETS: tuple[str, ...] = ("1.0", "0.5", "0.25", "0.125", "0.0625")

# rad/s → Switch ジャイロ raw。1rad/s = 57.2958dps / 0.06103dps/LSB。
_GYRO_RAD_TO_RAW = 57.29577951308232 / 0.06103

# 送出中に毎サンプル読む加工値。
# (倍率, 不感帯raw, 加速度符号, ジャイロ符号, HIDバイアスraw, HID軸別倍率)
# HID バイアス・倍率は HID 直結のときだけ入り、SDL 路では None（二重適用防止）。
LiveTrim = tuple[
    float,
    float,
    tuple[int, int, int],
    tuple[int, int, int],
    tuple[int, int, int] | None,
    tuple[float, float, float] | None,
]


@dataclass(slots=True)
class SourceBuildResult:
    """入力源の生成結果。呼び出し側（ImuPanel）が保持すべき参照をまとめて返す。"""

    gen: Any = None
    error: str = ""
    sensor_reader: Any = None
    step_gen: Any = None
    player: Any = None
    playing: bool = False
    mouse_source: Any = None
    mouse_capture: Any = None
    mouse_interval_s: float = 0.0


def gyro_bias_to_raw(bias: tuple[float, float, float]) -> tuple[int, int, int]:
    """rad/s のバイアスを Switch ジャイロ raw へ換算する。"""
    return (
        int(round(float(bias[0]) * _GYRO_RAD_TO_RAW)),
        int(round(float(bias[1]) * _GYRO_RAD_TO_RAW)),
        int(round(float(bias[2]) * _GYRO_RAD_TO_RAW)),
    )


def signs_from_settings(
    settings: dict[str, str], source: str
) -> tuple[tuple[int, int, int], tuple[int, int, int]]:
    """その入力源が使う符号を設定から読む。読めなければ源の既定へ倒す。

    HID 直結は生値が Switch フレームなので恒等、SDL 路は軸写像の
    逆変換（-1,-1,1）が前提。watched() が毎サンプル適用する。
    """
    default = (1, 1, 1) if source == "hid" else (-1, -1, 1)
    try:
        from pokecon.domain.config import parse_imu_axis_map
    except Exception:
        return default, default
    prefix = "hid_" if source == "hid" else ""
    acc = parse_imu_axis_map(str(settings.get(prefix + "accel_signs", "")), (-1, 1))
    gyr = parse_imu_axis_map(str(settings.get(prefix + "gyro_signs", "")), (-1, 1))
    if acc is None:
        acc = default
    if gyr is None:
        gyr = default
    return acc, gyr


def live_trim_from_settings(settings: dict[str, str], source: str) -> LiveTrim:
    """送出中に毎サンプル読む加工値を設定から組み立てる。"""
    try:
        gyro_scale = float(settings.get("gyro_scale", "1.0") or 1.0)
    except (TypeError, ValueError):
        gyro_scale = 1.0
    if not 0.01 <= gyro_scale <= 4.0:
        gyro_scale = 1.0
    try:
        dead_dps = float(settings.get("gyro_dead_dps", "0.0") or 0.0)
    except (TypeError, ValueError):
        dead_dps = 0.0
    dead_raw = max(0.0, dead_dps) / 0.06103
    if source == "mouse":
        # マウス源は既に Switch フレームの符号・軸で作っている。
        # ここで反転を重ねると二重に掛かる。不感帯も掛けない
        # （遅いエイムが止まる）。倍率だけは実測トリムとして残す。
        return (gyro_scale, 0.0, (1, 1, 1), (1, 1, 1), None, None)
    acc_signs, gyr_signs = signs_from_settings(settings, source)
    # HID 直結だけ、rad/s のバイアスと軸別倍率を raw へ換算して持つ。
    # SDL 路は SdlSensorReader がバイアスを持っているため、ここで
    # 入れると二重に引く。
    hid_bias: tuple[int, int, int] | None = None
    hid_scale: tuple[float, float, float] | None = None
    if source == "hid":
        try:
            from pokecon.domain.config import parse_imu_bias, parse_imu_scale

            bias = parse_imu_bias(str(settings.get("gyro_bias", "")))
            if bias is not None:
                hid_bias = gyro_bias_to_raw(bias)
            hid_scale = parse_imu_scale(str(settings.get("hid_gyro_scale", "")))
        except Exception:
            hid_bias = None
            hid_scale = None
    return (gyro_scale, dead_raw, acc_signs, gyr_signs, hid_bias, hid_scale)


def sample_transform(
    settings: dict[str, str],
    hid: bool = False,
    apply_signs: bool = True,
) -> Any | None:
    """IMU 軸・符号の変換関数を作る。恒等なら None。

    HID 直結は生値が Switch フレームのため `hid_*` キー（既定は恒等）を使う。
    接頭辞なしのキーは SDL センサー用（Switch→SDL 変換の逆写像）であり、
    コンソール側の反転解釈を打ち消す実測（例: 符号=1,-1,-1）にも使う。
    `apply_signs=False` は軸だけを返し、符号は呼び出し側（watched）が
    毎サンプル適用する（二重適用防止）。
    読めないキーは None（＝無変換）に倒す。
    """
    try:
        from pokecon.domain.config import parse_imu_axis_map
    except Exception:
        return None
    pre = "hid_" if hid else ""
    aa = parse_imu_axis_map(str(settings.get(pre + "accel_axes", "")), (0, 1, 2))
    as_ = parse_imu_axis_map(str(settings.get(pre + "accel_signs", "")), (-1, 1))
    ga = parse_imu_axis_map(str(settings.get(pre + "gyro_axes", "")), (0, 1, 2))
    gs = parse_imu_axis_map(str(settings.get(pre + "gyro_signs", "")), (-1, 1))
    if aa is None or as_ is None or ga is None or gs is None:
        return None
    acc_signs = as_ if apply_signs else (1, 1, 1)
    gyr_signs = gs if apply_signs else (1, 1, 1)
    if (
        aa == (0, 1, 2)
        and ga == (0, 1, 2)
        and acc_signs == (1, 1, 1)
        and gyr_signs == (1, 1, 1)
    ):
        return None

    def apply(sample: Any) -> tuple[int, int, int, int, int, int]:
        ax, ay, az, gx, gy, gz = (int(v) for v in sample)
        acc = (ax, ay, az)
        gyr = (gx, gy, gz)
        ca = tuple(acc[aa[i]] * acc_signs[i] for i in range(3))
        cg = tuple(gyr[ga[i]] * gyr_signs[i] for i in range(3))
        return (ca[0], ca[1], ca[2], cg[0], cg[1], cg[2])

    return apply


def apply_axis_settings(
    reader: Any,
    settings: dict[str, str],
    apply_signs: bool = True,
) -> None:
    """軸変換・バイアス設定を reader へ適用する。読めない項目は飛ばす。

    `apply_signs=False` は軸とバイアスだけを渡し、符号は呼び出し側
    （watched）が毎サンプル適用する（二重適用防止）。
    """
    try:
        from pokecon.domain.config import parse_imu_axis_map, parse_imu_bias
    except Exception:
        return
    try:
        aa = parse_imu_axis_map(str(settings.get("accel_axes", "")), (0, 1, 2))
        as_ = parse_imu_axis_map(str(settings.get("accel_signs", "")), (-1, 1))
        ga = parse_imu_axis_map(str(settings.get("gyro_axes", "")), (0, 1, 2))
        gs = parse_imu_axis_map(str(settings.get("gyro_signs", "")), (-1, 1))
        if aa is not None and as_ is not None and ga is not None and gs is not None:
            if apply_signs:
                reader.set_axis_map(aa, as_, ga, gs)
            else:
                reader.set_axis_map(aa, (1, 1, 1), ga, (1, 1, 1))
        bias = parse_imu_bias(str(settings.get("gyro_bias", "")))
        if bias is not None:
            reader.set_gyro_bias(bias)
    except Exception:
        pass


def hid_failure_message() -> str:
    """HID 直結を開始できなかった理由。IMU 未有効と未接続を区別する。"""
    try:
        from pokecon.domain.hidhide import hidhide_ready
        from pokecon.domain.nintendo_hid import get_shared_reader

        reader = get_shared_reader()
        is_open = getattr(reader, "is_open", None)
        is_armed = getattr(reader, "is_armed", None)

        if not hidhide_ready(force=True):
            return (
                "HidHideが未導入・未設定のためHID直結を使えません。"
                "READMEのHidHide手順を確認してください。"
            )
        if callable(is_open) and callable(is_armed) and is_open():
            if not is_armed():
                return (
                    "ProConのIMUを有効化できませんでした"
                    "（0x40/0x03の応答なし）。USBを挿し直してください。"
                )
    except Exception:
        pass
    return "ProConが開けません。USB接続を確認してください。"


def trim_and_scale_sample(
    sample: tuple[int, int, int, int, int, int],
    trim: LiveTrim | None,
) -> tuple[int, int, int, int, int, int]:
    """1 サンプルへ HID バイアス・軸倍率・反転符号・出力倍率・不感帯を適用する。"""
    ax, ay, az, gx, gy, gz = sample
    hid_bias_raw: tuple[int, int, int] | None = None
    hid_scale: tuple[float, float, float] | None = None
    if trim is None:
        gyro_scale, dead_raw = 1.0, 0.0
        acc_signs = (1, 1, 1)
        gyr_signs = (1, 1, 1)
    else:
        (
            gyro_scale,
            dead_raw,
            acc_signs,
            gyr_signs,
            hid_bias_raw,
            hid_scale,
        ) = trim
    # HID 直結のバイアスは、軸変換後（source 側で適用済み）・符号
    # 適用前の生値から引く。SDL 路は reader 側が引いているため
    # hid_bias_raw は None（二重適用しない）。
    if hid_bias_raw is not None:
        gx = max(-32768, min(32767, gx - hid_bias_raw[0]))
        gy = max(-32768, min(32767, gy - hid_bias_raw[1]))
        gz = max(-32768, min(32767, gz - hid_bias_raw[2]))
    if hid_scale is not None and hid_scale != (1.0, 1.0, 1.0):
        gx = max(-32768, min(32767, int(round(gx * hid_scale[0]))))
        gy = max(-32768, min(32767, int(round(gy * hid_scale[1]))))
        gz = max(-32768, min(32767, int(round(gz * hid_scale[2]))))
    # 符号は入力源ではなくここで一度だけ掛ける（二重適用防止）。
    ax = max(-32768, min(32767, ax * acc_signs[0]))
    ay = max(-32768, min(32767, ay * acc_signs[1]))
    az = max(-32768, min(32767, az * acc_signs[2]))
    gx = max(-32768, min(32767, gx * gyr_signs[0]))
    gy = max(-32768, min(32767, gy * gyr_signs[1]))
    gz = max(-32768, min(32767, gz * gyr_signs[2]))
    if gyro_scale != 1.0:
        # コンソール側のスケール解釈が想定と違う場合の実測トリム。
        gx = max(-32768, min(32767, int(round(gx * gyro_scale))))
        gy = max(-32768, min(32767, int(round(gy * gyro_scale))))
        gz = max(-32768, min(32767, int(round(gz * gyro_scale))))
    if dead_raw > 0.0:
        # 不感帯。各軸独立に潰す。
        try:
            from pokecon.domain.imu_wave import apply_gyro_deadband

            ax, ay, az, gx, gy, gz = apply_gyro_deadband(
                (ax, ay, az, gx, gy, gz), dead_raw
            )
        except Exception:
            pass
    return (ax, ay, az, gx, gy, gz)


def open_playback_source(
    path: str,
    on_end: Callable[[int], None],
) -> tuple[Any, Any, bool, str]:
    """記録ファイルを読む生成器を作る。``(gen, player, playing, error)`` を返す。"""
    if not path:
        return None, None, False, "再生ファイルを選んでください。"
    try:
        from pokecon.domain.motion_track import MotionPlayer

        player = MotionPlayer(path)
        if not player.load():
            return None, None, False, f"記録を読めません: {path}（{player.error}）"
        if len(player) == 0:
            return None, None, False, f"記録が空です: {path}"
    except Exception as e:
        return None, None, False, f"記録を読めません: {e!r}"
    inner = player.as_source()
    done = {"end": False}
    total = len(player)

    def played() -> Any:
        # 終端は 1 回だけ届け、送出ループを止める（最後の値を送り続けない）。
        sample = inner()
        if sample is None and not done["end"]:
            done["end"] = True
            on_end(total)
        return sample

    return played, player, True, ""


def open_recorder_sink(
    source: str,
    record_on: bool,
    record_path: str,
    play_path: str,
    log_fn: Callable[[str], None],
) -> tuple[bool, Any]:
    """記録先を開く。記録しない設定なら ``(True, None)``。失敗は ``(False, None)``。"""
    if not record_on:
        return True, None
    if not record_path:
        log_fn("記録先が未設定です。参照…で選んでください。")
        return False, None
    # 再生元と同じファイルを開くと、書き込み（上書き）で元の記録が消える。
    if source == "file" and os.path.normcase(
        os.path.abspath(record_path)
    ) == os.path.normcase(os.path.abspath(play_path)):
        log_fn("記録先が再生ファイルと同じため、記録せず再生します。")
        return True, None
    try:
        from pokecon.domain.motion_track import MotionRecorder

        recorder = MotionRecorder(record_path)
        recorder.open()
    except Exception as e:
        log_fn(f"記録を開始できません: {e!r}")
        return False, None
    return True, recorder


def _build_hid_source(settings: dict[str, str]) -> SourceBuildResult:
    try:
        from pokecon.domain.hidhide import hidhide_ready

        if not hidhide_ready(force=True):
            return SourceBuildResult()
    except Exception:
        return SourceBuildResult()
    try:
        from pokecon.domain.nintendo_hid import get_shared_reader

        reader = get_shared_reader()
        # 開始時は必ず 0x40 01 → 0x03 30 を送り直す。0x30 が新鮮でも
        # IMU 未有効のハンドル（他プロセスが切り戻した等）を取り違えない。
        # 応答が取れない個体は 0x30 の実値で判定する（arm_imu 内）。
        arm_imu = getattr(reader, "arm_imu", None)
        ensure_live = getattr(reader, "ensure_live", None)
        if callable(arm_imu):
            if not arm_imu():
                return SourceBuildResult()
        elif callable(ensure_live):
            if not ensure_live():
                return SourceBuildResult()
        elif not reader.is_open() and not reader.open():
            return SourceBuildResult()
        # 符号は watched() が毎サンプル適用する（二重適用防止）。
        transform = sample_transform(settings, hid=True, apply_signs=False)
        # パッド API と同じ最新値方式。0x30 の 3 スロットを古い順に
        # 消費せず、常に最新スロットだけを返す（時間の巻き戻し防止）。
        inner_read = reader.read_next
        last_try = {"t": 0.0}

        def read_with_reopen() -> Any:
            # USB 再列挙（UF2 書き込み等）でハンドルが死んだ場合や、
            # 開いたまま 0x30 が途絶えた場合に、IMU 側から再武装する。
            sample = inner_read()
            if sample is not None:
                return sample
            now = time.monotonic()
            if now - last_try["t"] >= 1.0:
                last_try["t"] = now
                try:
                    if callable(ensure_live):
                        ensure_live()
                    elif not reader.is_open():
                        reader.open(retries=1)
                except Exception:
                    return None
            return None

        if transform is None:
            return SourceBuildResult(gen=read_with_reopen, sensor_reader=reader)

        def mapped() -> Any:
            sample = read_with_reopen()
            if sample is None:
                return None
            try:
                return transform(sample)
            except Exception:
                return None

        return SourceBuildResult(gen=mapped, sensor_reader=reader)
    except Exception:
        return SourceBuildResult()


def _build_mouse_source(
    settings: dict[str, str],
    owner_hwnd: int,
    clip_rect: Any,
    on_state: Callable[[Any, bool, str], None],
    on_log: Callable[[str], None],
    on_mouse_created: Callable[[Any, Any, float], None] | None = None,
) -> SourceBuildResult:
    try:
        from pokecon.domain.mouse_capture import (
            RawMouseCapture,
            mouse_capture_available,
        )
        from pokecon.domain.mouse_gyro import (
            MouseGyroSource,
            config_from_settings,
        )
    except Exception as exc:
        return SourceBuildResult(error=f"マウス入力の部品を読めません: {exc!r}")
    if not mouse_capture_available():
        return SourceBuildResult(error="このOSではマウス入力を扱えません。")
    if owner_hwnd <= 0:
        # 前面判定ができないと、窓外でもキャプチャが残る。安全側で止める。
        return SourceBuildResult(
            error="PokeConのウィンドウを特定できないため、マウス入力を開始できません。"
        )
    try:
        config = config_from_settings(settings)
        mouse_source = MouseGyroSource(config)

        def reset_from_hotkey() -> None:
            try:
                mouse_source.reset_orientation()
            except Exception:
                pass
            on_log("姿勢をリセットしました（ホットキー）。")

        capture = RawMouseCapture(
            on_motion=mouse_source.push_motion,
            on_state=on_state,
            on_log=on_log,
            hotkey=config.hotkey,
            reset_hotkey=config.reset_hotkey,
            on_reset=reset_from_hotkey,
            roll_modifier=config.roll_modifier,
        )
    except Exception as exc:
        return SourceBuildResult(error=f"マウス入力を作れません: {exc!r}")

    # capture.start() の同期通知より先に呼び出し側へ参照を渡す。
    if on_mouse_created is not None:
        on_mouse_created(mouse_source, capture, config.interval_s)

    if not capture.start(owner_hwnd=owner_hwnd, clip_rect=clip_rect):
        return SourceBuildResult(
            error="マウス入力を開始できませんでした。",
            mouse_source=mouse_source,
            mouse_capture=capture,
            mouse_interval_s=config.interval_s,
        )
    return SourceBuildResult(
        gen=mouse_source.read,
        mouse_source=mouse_source,
        mouse_capture=capture,
        mouse_interval_s=config.interval_s,
    )


def build_imu_source(
    source: str,
    pad: int,
    settings: dict[str, str],
    *,
    play_path: str,
    owner_hwnd: int,
    clip_rect: Any,
    manual_reader: Callable[[], Any],
    on_playback_end: Callable[[int], None],
    on_mouse_state: Callable[[Any, bool, str], None],
    on_log: Callable[[str], None],
    on_mouse_created: Callable[[Any, Any, float], None] | None = None,
) -> SourceBuildResult:
    """入力源名と設定から生成器を組み立てる。失敗時は ``gen=None`` を返す。"""
    if source == "file":
        gen, player, playing, error = open_playback_source(play_path, on_playback_end)
        return SourceBuildResult(gen=gen, error=error, player=player, playing=playing)
    if source == "manual":
        return SourceBuildResult(gen=manual_reader())

    try:
        acc_z = int(settings.get("acc_z", "4096"))
    except (TypeError, ValueError):
        acc_z = 4096
    try:
        acc_x = int(settings.get("acc_x", "0"))
    except (TypeError, ValueError):
        acc_x = 0
    try:
        acc_y = int(settings.get("acc_y", "0"))
    except (TypeError, ValueError):
        acc_y = 0

    if source == "sensor":
        try:
            from pokecon.domain.sdl_sensor import SdlSensorReader

            # 符号は watched() が毎サンプル適用する（二重適用防止）。
            # 軸設定が欠けた場合でも reader 側の既定符号が残らないよう、
            # 生成時点で恒等にしておく。
            reader = SdlSensorReader(accel_signs=(1, 1, 1), gyro_signs=(1, 1, 1))
            apply_axis_settings(reader, settings, apply_signs=False)
            if not reader.open(pad):
                return SourceBuildResult()
            return SourceBuildResult(gen=reader.read, sensor_reader=reader)
        except Exception:
            return SourceBuildResult()
    if source == "hid":
        return _build_hid_source(settings)
    if source == "mouse":
        return _build_mouse_source(
            settings,
            owner_hwnd,
            clip_rect,
            on_mouse_state,
            on_log,
            on_mouse_created=on_mouse_created,
        )
    if source == "sine":
        try:
            from pokecon.domain.imu_wave import SineWave

            axis = int(settings.get("sine_axis", "2"))
            amp = int(settings.get("sine_amp", "4096"))
            period = float(settings.get("sine_period_s", "2.0"))
            return SourceBuildResult(
                gen=SineWave(axis=axis, amp=amp, period_s=period, acc_z=acc_z)
            )
        except (TypeError, ValueError):
            return SourceBuildResult()
    if source == "step":
        try:
            from pokecon.domain.imu_wave import StepWave

            axis = int(settings.get("step_axis", "2"))
            raw = int(settings.get("step_raw", "5899"))
            duration = float(settings.get("step_duration_s", "4.0"))
            gen = StepWave(axis=axis, raw=raw, duration_s=duration, acc_z=acc_z)
            gen.trigger()
            return SourceBuildResult(gen=gen, step_gen=gen)
        except (TypeError, ValueError):
            return SourceBuildResult()
    try:
        from pokecon.domain.imu_wave import StaticRest

        return SourceBuildResult(gen=StaticRest(acc_z=acc_z, acc_x=acc_x, acc_y=acc_y))
    except Exception:
        return SourceBuildResult()


def close_sensor_reader(reader: Any) -> None:
    """共有 HID でないセンサーリーダーを閉じる。例外は投げない。"""
    if reader is None:
        return
    try:
        from pokecon.domain.nintendo_hid import is_shared_reader

        if is_shared_reader(reader):
            return
    except Exception:
        pass
    try:
        reader.close()
    except Exception:
        pass
