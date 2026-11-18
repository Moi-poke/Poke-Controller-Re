#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""bcon_shared.py - BCON 送出系（in-proc / 別プロセス）の共通純粋補助."""

from __future__ import annotations

import math
import sys
import threading
from collections import deque
from collections.abc import Callable, Sequence
from typing import Any

#: live loop の既定周期（120Hz）。Pico の timeout-neutral 200ms に対し十分短い。
LIVE_INTERVAL_S = 1.0 / 120.0

#: IMU loop の既定周期（200Hz・spec §5.13）。
IMU_INTERVAL_S = 1.0 / 200.0

#: 未受信時や失敗時に返す STATUS 辞書の正準テンプレート。
ZERO_STATUS: dict[str, int] = {
    "flags": 0,
    "last_seq": 0,
    "err_crc": 0,
    "err_drop": 0,
    "errcode": 0,
}

#: 未取得時や失敗時に返す live_stats 辞書の正準テンプレート。
ZERO_LIVE_STATS: dict[str, Any] = {
    "sent": 0,
    "merged": 0,
    "dropped": 0,
    "seq_gap": 0,
    "err_crc": 0,
    "err_drop": 0,
    "rtt_ms": None,
}


def precise_timer(on: bool) -> None:
    """Windows の待ち粒度を 1ms へ上げる（失敗時は coarse のまま続行）。"""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        if on:
            ctypes.windll.winmm.timeBeginPeriod(1)
        else:
            ctypes.windll.winmm.timeEndPeriod(1)
    except Exception:
        pass


def zero_status() -> dict[str, int]:
    """全要素 0 の STATUS 辞書を新しく返す。"""
    return dict(ZERO_STATUS)


def normalize_status(raw: Any) -> dict[str, int]:
    """RPC 等から得た STATUS 辞書を整数5鍵へ正規化する。"""
    if not isinstance(raw, dict):
        return zero_status()
    merged = zero_status()
    for key in merged:
        if key in raw:
            try:
                merged[key] = int(raw[key])
            except (TypeError, ValueError):
                pass
    return merged


def zero_live_stats() -> dict[str, Any]:
    """初期値のみの live_stats 辞書を新しく返す。"""
    return dict(ZERO_LIVE_STATS)


def normalize_live_stats(raw: Any) -> dict[str, Any]:
    """RPC 等から得た live_stats 辞書を既定鍵へ揃える。"""
    if not isinstance(raw, dict):
        return zero_live_stats()
    merged = zero_live_stats()
    for key in merged:
        if key in raw:
            merged[key] = raw[key]
    return merged


def clamp_i16(value: int) -> int:
    """整数値を符号付き 16bit 範囲 (-32768..32767) へ飽和させる。"""
    return max(-32768, min(32767, int(value)))


def clamp_imu_tuple(
    vals: Sequence[int],
) -> tuple[int, int, int, int, int, int]:
    """IMU 6 値列を int16 飽和済みの 6 タプルへ整える。"""
    return (
        clamp_i16(vals[0]),
        clamp_i16(vals[1]),
        clamp_i16(vals[2]),
        clamp_i16(vals[3]),
        clamp_i16(vals[4]),
        clamp_i16(vals[5]),
    )


def parse_clamped_timeout(
    raw: Any,
    *,
    nan_value: float,
    min_s: float,
    max_s: float,
    fallback: float | None = None,
) -> float | None:
    """待ち時間引数を浮動小数へ読み、NaN/Inf/上下限を正規化する。

    数値化できない入力は `fallback` を返す（`None` なら呼び側で失敗扱い）。
    """
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return fallback
    if math.isnan(value):
        return nan_value
    if math.isinf(value):
        return max_s
    return max(min_s, min(value, max_s))


def imu_sample_of(msg: Any) -> tuple[int, int, int, int, int, int] | None:
    """`("imu", ax, ay, az, gx, gy, gz[, seq])` から 6 値を取り出す。

    親の合算メールボックスは int16 を超える和を載せ得るため、ここでは
    飽和させない（線へ出す直前に割る）。形が違えば None。
    """
    if not (isinstance(msg, tuple) and len(msg) >= 7 and msg[0] == "imu"):
        return None
    try:
        vals = tuple(int(v) for v in msg[1:7])
    except (TypeError, ValueError):
        return None
    if len(vals) != 6:
        return None
    return (vals[0], vals[1], vals[2], vals[3], vals[4], vals[5])


def imu_combine(
    samples: (
        list[tuple[int, int, int, int, int, int]]
        | deque[tuple[int, int, int, int, int, int]]
    ),
) -> tuple[int, int, int, int, int, int]:
    """複数サンプルを 1 つへ畳む。加速度は最新、ジャイロは合計。

    ジャイロは 5ms あたりの角速度なので、和＝その区間に回った合計角度。
    最新値だけを残すと動きが消えるため、ここは必ず和にする。
    """
    last = samples[-1]
    return (
        int(last[0]),
        int(last[1]),
        int(last[2]),
        sum(int(s[3]) for s in samples),
        sum(int(s[4]) for s in samples),
        sum(int(s[5]) for s in samples),
    )


def imu_split_even(total: int, parts: int) -> list[int]:
    """整数を parts 個へ、合計を保ったままできるだけ均等に割る。"""
    base, rem = divmod(int(total), parts)
    return [base + (1 if i < rem else 0) for i in range(parts)]


def imu_plan_frames(
    samples: list[tuple[int, int, int, int, int, int]],
    carry: list[int],
    *,
    chunks_max: int = 3,
) -> tuple[list[tuple[int, int, int, int, int, int]], list[int]]:
    """合算済みサンプルを int16 の IMU フレーム列へ割る。

    戻り値は (フレーム列, 次のバッチへ繰り越す残差)。最大 chunks_max
    フレームに抑え、収まらない角度は残差として次のサンプルへ足す。
    """
    merged = imu_combine(samples)
    ax, ay, az = merged[0], merged[1], merged[2]
    totals = [
        int(carry[0]) + int(merged[3]),
        int(carry[1]) + int(merged[4]),
        int(carry[2]) + int(merged[5]),
    ]
    parts = 1
    for total in totals:
        if total >= 0:
            need = (total + 32766) // 32767
        else:
            need = (-total + 32767) // 32768
        if need > parts:
            parts = need
    parts = max(1, min(parts, int(chunks_max)))
    columns: list[list[int]] = []
    residual: list[int] = []
    for total in totals:
        split = [clamp_i16(v) for v in imu_split_even(total, parts)]
        residual.append(total - sum(split))
        columns.append(split)
    frames = [
        (ax, ay, az, columns[0][i], columns[1][i], columns[2][i]) for i in range(parts)
    ]
    return frames, residual


def add_unique_listener(
    lock: threading.Lock | threading.RLock,
    listeners: list[Any],
    func: Any,
    ng_ids: set[int] | None = None,
) -> bool:
    """聞き手を重複なく登録する（非 callable は False）。"""
    if not callable(func):
        return False
    try:
        with lock:
            if func not in listeners:
                listeners.append(func)
            if ng_ids is not None:
                try:
                    ng_ids.discard(id(func))
                except Exception:
                    pass
        return True
    except Exception:
        return False


def remove_registered_listener(
    lock: threading.Lock | threading.RLock,
    listeners: list[Any],
    func: Any,
    ng_ids: set[int] | None = None,
) -> None:
    """聞き手を外し、必要なら失敗記録も消す。"""
    try:
        with lock:
            if func in listeners:
                listeners.remove(func)
            if ng_ids is not None:
                try:
                    ng_ids.discard(id(func))
                except Exception:
                    pass
    except Exception:
        pass


def notify_imu_listeners(
    lock: threading.Lock | threading.RLock,
    listeners: list[Callable[[tuple[int, int, int, int, int, int]], None]],
    sample: tuple[int, int, int, int, int, int],
) -> None:
    """送った IMU サンプルを聞き手へ錠の外で配る。"""
    try:
        with lock:
            funcs = list(listeners)
        for func in funcs:
            try:
                func(sample)
            except Exception:
                pass
    except Exception:
        pass


def notify_tx_listeners(
    lock: threading.Lock | threading.RLock,
    listeners: list[Any],
    ng_ids: set[int],
    row: Any,
    get_last_note: Callable[[], str | None],
    set_last_note: Callable[[str], None],
    warn: Callable[[str], None],
) -> None:
    """送信行を聞き手へ配る（直前と同一行の連続再送は抑止する）。"""
    text = row if isinstance(row, str) else ""
    if not text:
        return
    with lock:
        if text == get_last_note():
            return
        funcs = list(listeners)
        if not funcs:
            return
        set_last_note(text)
    for func in funcs:
        try:
            func(row)
        except Exception as exc:
            try:
                key = id(func)
                with lock:
                    if key in ng_ids:
                        continue
                    ng_ids.add(key)
            except Exception:
                pass
            try:
                warn(f"送信行の受け取りで例外が出ました（以後は黙ります）: {exc!r}")
            except Exception:
                pass
