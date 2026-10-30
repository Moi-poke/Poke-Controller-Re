"""描画スレッド（core/preview_pump.py）の規則。

設計: docs/superpowers/specs/2026-09-26-gdi-preview-renderer-design.md
改訂: docs/superpowers/specs/2026-10-30-frame-driven-preview-pump-design.md

時刻を待たずに決まる規則（どのフレームを出すか）は、偽の時計と台本付きの
フレーム源で _step を直接回して確かめる。スレッドの起床・停止・統計だけを
実物（LatestFrame + 条件変数）で確かめる。
"""

from __future__ import annotations

import time
from typing import Any

import numpy as np
import pytest
from core.Camera import LatestFrame
from core.preview_pump import PreviewPump


class _FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


class _ScriptedSource:
    """wait_for_frame が「次の1枚を公開する」台本付きフレーム源。

    実時間は進めない。1 回の待ちで時計を step だけ進め、世代を 1 増やす。
    """

    def __init__(self, clock: _FakeClock, step: float) -> None:
        self.clock = clock
        self.step = step
        self.seq = 0
        self.frame: np.ndarray | None = np.zeros((2, 2, 3), np.uint8)
        self.waits = 0
        self.subscribed: Any = None

    def readFrameWithSeq(self, copy: bool = False) -> tuple[Any, int]:
        return self.frame, self.seq

    def wait_for_frame(self, after_seq: int, timeout: float) -> int:
        self.clock.t += self.step
        self.seq += 1
        self.waits += 1
        return self.seq

    def subscribe_frames(self, callback: Any) -> None:
        self.subscribed = callback

    def unsubscribe_frames(self, callback: Any) -> None:
        if self.subscribed is callback:
            self.subscribed = None


def _make(
    step: float, fps: int
) -> tuple[PreviewPump, _ScriptedSource, _FakeClock, list[tuple[Any, Any]]]:
    clock = _FakeClock()
    source = _ScriptedSource(clock, step)
    drawn: list[tuple[Any, Any]] = []

    def draw(frame: Any, seq: Any) -> bool:
        drawn.append((frame, seq))
        return True

    pump = PreviewPump(source, source.readFrameWithSeq, draw, fps, clock=clock)
    return pump, source, clock, drawn


def _run(pump: PreviewPump, steps: int) -> None:
    last: int | None = None
    deadline = pump._clock()
    for _ in range(steps):
        last, deadline = pump._step(last, deadline)


def test_same_rate_draws_every_published_frame() -> None:
    # Given: 60fps の取込と 60fps の表示
    pump, _source, clock, drawn = _make(1 / 60, 60)
    pump.set_active(True)

    # When: 30 回ぶん回す
    _run(pump, 30)

    # Then: 1 枚も落とさず、全部描く（位相合わせの空振りが無い）
    assert [seq for _frame, seq in drawn] == list(range(30))
    health = pump.pump_health()
    assert health.frames_dropped_rate == 0
    assert health.frames_drawn == 30
    assert clock.t == pytest.approx(30 / 60)


def test_display_cap_drops_early_frames() -> None:
    # Given: 60fps の取込を 30fps で表示する
    pump, _source, _clock, drawn = _make(1 / 60, 30)
    pump.set_active(True)

    # When: 60 枚ぶん回す
    _run(pump, 60)

    # Then: およそ半分だけ描き、残りは上限で捨てる
    assert 29 <= len(drawn) <= 31
    health = pump.pump_health()
    assert health.frames_drawn == len(drawn)
    assert health.frames_dropped_rate == 60 - len(drawn)


def test_slower_source_draws_every_frame() -> None:
    # Given: 30fps の取込を 60fps で表示する
    pump, _source, _clock, drawn = _make(1 / 30, 60)
    pump.set_active(True)

    # When: 20 枚ぶん回す
    _run(pump, 20)

    # Then: 取込より速くは描けないが、取りこぼしもしない
    assert len(drawn) == 20
    assert pump.pump_health().frames_dropped_rate == 0


def test_inactive_pump_never_draws() -> None:
    # Given: 表示が切られている
    pump, _source, _clock, drawn = _make(1 / 60, 60)

    # When: 20 回ぶん回す
    _run(pump, 20)

    # Then: 1 枚も描かない
    assert drawn == []
    assert pump.pump_health().frames_drawn == 0


def test_missing_frame_draws_the_disabled_image() -> None:
    # Given: カメラが 1 枚も出していない（None）
    pump, source, _clock, drawn = _make(1 / 60, 60)
    source.frame = None
    pump.set_active(True)

    # When: 3 回ぶん回す
    _run(pump, 3)

    # Then: 停止画像の描画を頼み続ける（重複を省くのは draw 側の契約）
    assert [seq for _frame, seq in drawn] == [0, 1, 2]
    assert pump.pump_health().frames_none == 3


def test_missing_frame_waits_on_the_observed_sequence() -> None:
    # Given: まだ1枚も公開されていないフレーム箱（seq は 0 のまま）
    clock = _FakeClock()
    source = _ScriptedSource(clock, 0.25)
    source.frame = None
    waited: list[int] = []

    def wait_for_frame(after_seq: int, timeout: float) -> int:
        waited.append(after_seq)
        clock.t += timeout
        return after_seq

    source.wait_for_frame = wait_for_frame  # type: ignore[method-assign]
    pump = PreviewPump(
        source, source.readFrameWithSeq, lambda _f, _s: True, 60, clock=clock
    )
    pump.set_active(True)

    # When: 3 回ぶん回す
    _run(pump, 3)

    # Then: 観測した世代（0）で待つ。-1 で待つと述語が常に真になり、
    # 停止画像を描き続ける空転になる。
    assert waited == [0, 0, 0]


def test_draw_exception_is_counted_and_logged_once() -> None:
    # Given: 描画が毎回例外を投げる
    clock = _FakeClock()
    source = _ScriptedSource(clock, 1 / 60)
    calls = {"n": 0}

    def draw(_frame: Any, _seq: Any) -> bool:
        calls["n"] += 1
        raise RuntimeError("boom")

    pump = PreviewPump(source, source.readFrameWithSeq, draw, 60, clock=clock)
    pump.set_active(True)

    # When: 4 回ぶん回す
    _run(pump, 4)

    # Then: 止まらずに走り続け、例外は数えるだけ（ログは 1 回）
    assert calls["n"] == 4
    assert pump.pump_health().draw_errors == 4
    assert pump._error_logged is True


def test_take_stats_reports_presented_fps_and_resets() -> None:
    # Given: 60fps で 60 枚描いた（1 秒ぶん）
    pump, _source, clock, _drawn = _make(1 / 60, 60)
    pump.set_active(True)
    _run(pump, 60)

    # When: 統計を読む
    stats = pump.take_stats()

    # Then: 描いた枚数/経過秒 を返し、次の区切りは 0 から
    assert stats["fps"] == pytest.approx(60.0, abs=0.5)
    assert stats["draw_ms"] >= 0.0
    assert pump.take_stats()["fps"] == 0.0
    assert clock.t == pytest.approx(1.0)


def test_start_subscribes_and_stop_unsubscribes() -> None:
    # Given: 実物のフレーム箱と購読を受けるフレーム源
    box = LatestFrame()
    pump = PreviewPump(
        _SubscribingSource(box),
        lambda: box.get_with_seq(),
        lambda _frame, _seq: True,
        60,
    )

    # When: 起動して止める
    assert pump.start() is True
    assert pump.health_snapshot()["pump_running"] is True
    assert pump.stop() is True

    # Then: 購読が外れ、スレッドも残らない
    assert pump.health_snapshot()["pump_running"] is False
    assert pump.stop() is True


class _SubscribingSource:
    def __init__(self, box: LatestFrame) -> None:
        self.box = box

    def readFrameWithSeq(self, copy: bool = False) -> tuple[Any, int]:
        return self.box.get_with_seq()

    def wait_for_frame(self, after_seq: int, timeout: float) -> int:
        return self.box.wait_for_change(after_seq, timeout)

    def subscribe_frames(self, callback: Any) -> None:
        self.box.subscribe(callback)

    def unsubscribe_frames(self, callback: Any) -> None:
        self.box.unsubscribe(callback)


def test_real_thread_wakes_on_publish_and_stops() -> None:
    # Given: 実物のフレーム箱を購読する描画スレッド
    box = LatestFrame()
    seen: list[int] = []

    def draw(frame: Any, seq: Any) -> bool:
        # 起動直後は「まだ1枚も無い」ので停止画像の描画が来る。数えるのは
        # 実際のフレームだけ。
        if frame is not None and seq is not None:
            seen.append(int(seq))
        return True

    pump = PreviewPump(
        _SubscribingSource(box),
        lambda: box.get_with_seq(),
        draw,
        60,
    )
    pump.set_active(True)
    assert pump.start() is True

    # When: 1 枚ずつ公開する（公開のたびに起きなければここで待ち切る）
    frame = np.zeros((2, 2, 3), np.uint8)
    for expected in range(1, 6):
        box.put(frame, 0)
        deadline = time.monotonic() + 5.0
        while expected not in seen and time.monotonic() < deadline:
            time.sleep(0.005)
        # Then: 起きて、その世代を描いている
        assert expected in seen

    # Then: 止められる
    assert pump.stop() is True
    assert seen[:5] == [1, 2, 3, 4, 5]
