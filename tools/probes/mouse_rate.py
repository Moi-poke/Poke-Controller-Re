#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mouse_rate.py - マウスのRaw Inputイベントレートを測る（Windows）。

使い方:
    uv run --frozen python tools/probes/mouse_rate.py --seconds 5

表示された秒数のあいだマウスを動かす。WM_INPUTの到着間隔から、
実効的なポーリングレート（125/250/500/1000Hzなど）を見積もる。
マウス付属ソフトの設定値ではなく、このアプリが実際に受け取る値。
"""

from __future__ import annotations

import argparse
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "SerialController"))

from pokecon.domain.mouse_capture import (  # noqa: E402
    RawMouseCapture,
    mouse_capture_available,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Raw Inputのイベントレート測定")
    parser.add_argument("--seconds", type=float, default=5.0, help="測定秒数")
    args = parser.parse_args()
    if not mouse_capture_available():
        print("このOSでは測定できません（Windows専用）。")
        return 1

    times: list[float] = []

    def on_motion(dx: int, dy: int, roll: bool = False) -> None:
        times.append(time.perf_counter())

    capture = RawMouseCapture(
        on_motion=on_motion,
        on_state=lambda enabled, reason: None,
        on_log=print,
        hotkey="",
    )
    print(f"{args.seconds:g}秒間、マウスを動かしてください...")
    try:
        if not capture.start(owner_hwnd=0):
            print("Raw Inputを開始できませんでした。")
            return 1
        capture.set_enabled(True, "probe")
        time.sleep(max(0.5, float(args.seconds)))
    finally:
        capture.stop()

    if len(times) < 2:
        print("イベントが足りません。マウスを動かしてから再実行してください。")
        return 1
    gaps = [(b - a) * 1000.0 for a, b in zip(times, times[1:])]
    median_ms = statistics.median(gaps)
    median_hz = 1000.0 / median_ms if median_ms > 0 else 0.0
    span = times[-1] - times[0]
    average_hz = (len(times) - 1) / span if span > 0 else 0.0
    print(f"イベント {len(times)}件 / 平均 {average_hz:.0f}/s")
    print(
        f"間隔 中央 {median_ms:.2f}ms / 最小 {min(gaps):.2f}ms / 最大 {max(gaps):.2f}ms"
    )
    print(f"中央値から ≈ {median_hz:.0f}Hz")
    for hz in (125, 250, 500, 1000, 2000, 4000, 8000):
        if abs(median_hz - hz) / hz < 0.15:
            print(f"→ およそ {hz}Hz 相当")
            break
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
