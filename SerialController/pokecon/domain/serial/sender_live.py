#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""sender_live.py - Sender の 8ms live ワーカー・タイマー粒度・統計補助 Mixin."""

from __future__ import annotations

import threading
import time
import traceback
from typing import TYPE_CHECKING, Any

from pokecon.domain.serial.live_scheduler import LiveScheduler


class SenderLiveMixin:
    """Sender から切り出した Pico live ワーカーと送出統計管理。"""

    if TYPE_CHECKING:
        POSTURE_CENTER: int
        POSTURE_HAT_CENTER: int
        _lock: threading.RLock
        _logger: Any
        transport: Any

        def _should_show_serial(self) -> bool: ...
        def snapshot(self) -> dict[str, Any]: ...

        @staticmethod
        def encodePicoState(snap: dict[str, Any]) -> str: ...

    #: 8 ms (125 Hz) ごとに変化の取出しを行うスロット周期。
    LIVE_SLOT_S = 0.008
    #: 無変化の再送間隔（UART 10ms ポーリングでのオーバーラン防止）。
    LIVE_REPEAT_S = 0.024
    #: 切断時に中立の送出を待つ上限。
    CLOSE_DRAIN_S = 0.10
    #: 切断時に worker の停止を待つ上限。
    CLOSE_JOIN_S = 0.30
    #: 線が開いているかの問い合わせ間隔（秒）。
    OPEN_PROBE_S = 0.25

    def _ensureLiveState(self) -> None:
        """live 経路の scheduler、worker 状態、統計、表示状態を初期化する。"""
        if not hasattr(self, "_live_lock"):
            self._live_lock = threading.Lock()
        if not hasattr(self, "_live_sched"):
            min_dwell = float(getattr(self, "_live_min_dwell_s", 0.016))
            self._live_sched: LiveScheduler = LiveScheduler(min_dwell_s=min_dwell)
        if not hasattr(self, "_live_stop"):
            self._live_stop = threading.Event()
        if not hasattr(self, "_live_thread"):
            self._live_thread: threading.Thread | None = None
        if not hasattr(self, "_live_stats"):
            self._live_stats: dict[str, int] = {
                "put": 0,
                "replaced": 0,
                "sent": 0,
                "keepalive": 0,
                "dropped": 0,
                "last_revision": -1,
                "inversions": 0,
            }
        if not hasattr(self, "_live_last_snap"):
            self._live_last_snap: dict[str, Any] | None = None
        if not hasattr(self, "_live_last_sent_at"):
            self._live_last_sent_at: float | None = None
        if not hasattr(self, "_live_timer_raised"):
            self._live_timer_raised = False
        if not hasattr(self, "_live_closing"):
            self._live_closing = False
        if not hasattr(self, "_live_last_shown_at"):
            self._live_last_shown_at: float | None = None
        if not hasattr(self, "_live_last_shown_snap"):
            self._live_last_shown_snap: dict[str, Any] | None = None
        if not hasattr(self, "_last_put_rev"):
            self._last_put_rev = -1
        if not hasattr(self, "_open_probe_at"):
            self._open_probe_at = -1e9
        if not hasattr(self, "_open_probe_tr"):
            self._open_probe_tr: Any = None
        if not hasattr(self, "_open_probe_ok"):
            self._open_probe_ok = True
        if not hasattr(self, "_open_probe_warned"):
            self._open_probe_warned = False

    def putLive(
        self, snap: dict[str, Any] | None = None, *, _closing: bool = False
    ) -> None:
        """申告された状態を scheduler の列へ積む（潰さず順に送る）。"""
        self._ensureLiveState()
        with self._lock:
            if self._live_closing and not _closing:
                return
            if snap is None:
                snap = self.snapshot()
            revision = int(snap.get("revision", 0))
            if __debug__:
                assert revision >= self._last_put_rev, (
                    "stale snapshot: 錠の外で採った snap を渡していないか"
                )
            self._last_put_rev = revision
            self._live_sched.push(snap)
        with self._live_lock:
            self._live_stats["put"] += 1

    def takeLive(self) -> dict[str, Any] | None:
        """互換のために残す取出し口。送出中へ進めて非破壊で覗く。"""
        self._ensureLiveState()
        now = time.perf_counter()
        self._live_sched.advance(now)
        return self._live_sched.current()

    def _showLiveRow(self, snap: dict[str, Any], now: float, is_repeat: bool) -> bool:
        """送信行の表示可否を返す。表示は 20 Hz に制限し、解放・中立は即表示。"""
        if is_repeat or not self._should_show_serial():
            return False
        prev = self._live_last_shown_snap
        releasing = False
        if prev is not None:
            stick_released = any(
                int(prev[k]) != self.POSTURE_CENTER
                and int(snap[k]) == self.POSTURE_CENTER
                for k in ("lx", "ly", "rx", "ry")
            )
            releasing = (
                (int(snap["btn"]) & int(prev["btn"])) != int(prev["btn"])
                or (
                    int(prev["hat"]) != self.POSTURE_HAT_CENTER
                    and int(snap["hat"]) == self.POSTURE_HAT_CENTER
                )
                or stick_released
            )
        due = (
            self._live_last_shown_at is None or now - self._live_last_shown_at >= 0.050
        )
        if releasing or due:
            self._live_last_shown_at = now
            self._live_last_shown_snap = dict(snap)
            return True
        return False

    def _beginPreciseTimer(self) -> None:
        """Windows のタイマ分解能を 1ms へ上げる。"""
        if getattr(self, "_live_timer_raised", False):
            return
        try:
            import ctypes

            ctypes.WinDLL("winmm").timeBeginPeriod(1)
            self._live_timer_raised = True
        except Exception:
            self._live_timer_raised = False

    def _endPreciseTimer(self) -> None:
        """上げた分解能を必ず戻す（上げっぱなしは消費電力に響く）。"""
        if not getattr(self, "_live_timer_raised", False):
            return
        try:
            import ctypes

            ctypes.WinDLL("winmm").timeEndPeriod(1)
        except Exception:
            pass
        self._live_timer_raised = False

    def _transportOpenCached(self, transport: Any, now: float) -> bool:
        """線が開いているか。問い合わせは OPEN_PROBE_S に 1 回だけにする。"""
        if (
            self._open_probe_tr is transport
            and now - self._open_probe_at < self.OPEN_PROBE_S
        ):
            return self._open_probe_ok
        self._open_probe_tr = transport
        self._open_probe_at = now
        probe = getattr(transport, "is_open", None)
        if probe is None or not getattr(transport, "is_open_is_local", False):
            self._open_probe_ok = True
            return True
        try:
            self._open_probe_ok = bool(probe() if callable(probe) else probe)
        except Exception:
            if not self._open_probe_warned:
                self._open_probe_warned = True
                self._logger.warning(
                    "is_open の問い合わせで例外。開いている扱いにします",
                    exc_info=True,
                )
            self._open_probe_ok = True
        return self._open_probe_ok

    def _liveLoop(self, transport: Any) -> None:
        """8 ms ごとに最新の姿勢を1行送り続ける（状態同期）。"""
        self._ensureLiveState()
        self._beginPreciseTimer()
        try:
            deadline = time.perf_counter()
            while not self._live_stop.is_set():
                deadline += self.LIVE_SLOT_S
                remain = deadline - time.perf_counter()
                if remain > 0:
                    time.sleep(remain)
                else:
                    deadline = time.perf_counter()
                if self._live_stop.is_set():
                    break
                now = time.perf_counter()
                self._live_sched.advance(now)
                out = self._live_sched.current()
                if out is None:
                    continue
                with self._live_lock:
                    prev = self._live_last_snap
                    self._live_last_snap = out
                try:
                    is_repeat = prev is not None and int(
                        prev.get("revision", -1)
                    ) == int(out.get("revision", -2))
                except (TypeError, ValueError):
                    is_repeat = False
                if is_repeat:
                    with self._live_lock:
                        last_at = self._live_last_sent_at
                    if last_at is not None and now - last_at < self.LIVE_REPEAT_S:
                        continue
                if not self._transportOpenCached(transport, now):
                    with self._live_lock:
                        self._live_last_sent_at = now
                    continue
                self._recordLiveOrder(out)
                try:
                    show = self._showLiveRow(out, now, is_repeat)
                    transport.send_row(self.encodePicoState(out), measure_perf=show)
                    with self._live_lock:
                        self._live_stats["sent"] += 1
                        if is_repeat:
                            self._live_stats["keepalive"] += 1
                        self._live_last_sent_at = time.perf_counter()
                except Exception:
                    with self._live_lock:
                        self._live_last_sent_at = time.perf_counter()
                    self._logLiveError()
        finally:
            self._endPreciseTimer()

    def _recordLiveOrder(self, snap: dict[str, Any]) -> None:
        """revision が逆転していないかを数える。"""
        rev = int(snap.get("revision", 0))
        with self._live_lock:
            if rev < self._live_stats["last_revision"]:
                self._live_stats["inversions"] += 1
            self._live_stats["last_revision"] = rev

    def _logLiveError(self) -> None:
        """送信の失敗を記録する。黙って捨てない。"""
        logger = getattr(self, "_logger", None)
        if logger is not None:
            logger.error("live worker send failed:\n" + traceback.format_exc())

    def startLiveWorker(self, transport: Any = None) -> bool:
        """workerを1本だけ起動し、現在姿勢を最初に送る。"""
        return self._startLiveWorker(transport) is not None

    def _startLiveWorker(self, transport: Any = None) -> threading.Thread | None:
        """workerを1本だけ起動し、起こしたスレッドを返す。起こさなければ None。"""
        self._ensureLiveState()
        if transport is None:
            transport = getattr(self, "transport", None)
        if transport is None:
            return None
        with self._live_lock:
            thread = self._live_thread
            if thread is not None and thread.is_alive():
                return None
            self._live_stop.clear()
            self._live_last_snap = None
            self._live_last_sent_at = None
            self._live_last_shown_at = None
            self._live_last_shown_snap = None
            started = threading.Thread(
                target=self._liveLoop,
                args=(transport,),
                name="PicoLiveWorker",
                daemon=True,
            )
            self._live_thread = started
            started.start()
            started_alive = started.is_alive()
        if not started_alive:
            with self._live_lock:
                if self._live_thread is started:
                    self._live_thread = None
            return None
        with self._lock:
            self.putLive(self.snapshot())
        return started

    def discardLive(self) -> bool:
        """scheduler の未送出を破棄する。"""
        self._ensureLiveState()
        return bool(self._live_sched.clear())

    def waitLiveDrained(self, timeout: float = 0.05) -> bool:
        """未送出が空になるまで待つ。"""
        self._ensureLiveState()
        limit = time.perf_counter() + max(float(timeout), 0.0)
        while time.perf_counter() < limit:
            if self._live_sched.pending_empty():
                return True
            if not self.isLiveWorkerRunning():
                return False
            time.sleep(0.001)
        return bool(self._live_sched.pending_empty())

    def stopLiveWorker(self, timeout: float = 1.0) -> bool:
        """常駐ワーカーを止める。止まるまで待つ。"""
        self._ensureLiveState()
        with self._live_lock:
            self._live_stop.set()
            thread = self._live_thread
        if thread is None:
            return True
        thread.join(timeout)
        alive = thread.is_alive()
        if not alive:
            with self._live_lock:
                if self._live_thread is thread:
                    self._live_thread = None
        return not alive

    def _stopLiveWorkerIf(
        self, thread: threading.Thread | None, timeout: float = 1.0
    ) -> bool:
        """指定の worker がまだ残っているときだけ止める。"""
        self._ensureLiveState()
        if thread is not None:
            with self._live_lock:
                if self._live_thread is not thread:
                    return True
        return self.stopLiveWorker(timeout)

    def isLiveWorkerRunning(self) -> bool:
        """ワーカーが動いているか（読むだけ）。"""
        self._ensureLiveState()
        with self._live_lock:
            thread = self._live_thread
        return thread is not None and thread.is_alive()

    def getLiveStats(self) -> dict[str, Any]:
        """帳簿の写しを返す（読むだけ・副作用なし）。"""
        self._ensureLiveState()
        sched_stats = self._live_sched.stats()
        with self._live_lock:
            out = dict(self._live_stats)
        out["replaced"] = int(sched_stats.get("merged", 0))
        out["dropped"] = int(sched_stats.get("dropped", 0))
        return out

    def clearLiveStats(self) -> None:
        """live統計を初期化する。"""
        self._ensureLiveState()
        self._live_sched.reset_stats()
        with self._live_lock:
            for key in (
                "put",
                "replaced",
                "sent",
                "keepalive",
                "dropped",
                "inversions",
            ):
                self._live_stats[key] = 0
            self._live_stats["last_revision"] = -1
