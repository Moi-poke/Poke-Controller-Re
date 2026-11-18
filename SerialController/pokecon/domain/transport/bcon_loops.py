#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""bcon_loops.py - BconTransport の 120Hz live / 200Hz IMU ループと聞き手管理."""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from pokecon.domain.transport.bcon_protocol import T_STATE, frame_build
from pokecon.domain.transport.bcon_shared import (
    IMU_INTERVAL_S,
    LIVE_INTERVAL_S,
    add_unique_listener,
    notify_imu_listeners,
    notify_tx_listeners,
    precise_timer,
    remove_registered_listener,
    zero_live_stats,
)


class BconLoopsMixin:
    """120Hz live loop・200Hz IMU loop・送出聞き手の Mixin."""

    if TYPE_CHECKING:
        ser: Any
        _logger: Any
        _lock: threading.RLock
        _write_lock: threading.Lock
        _tx_seq: int
        _tx_hold: bool
        _input_hold: bool
        _hello_established: bool
        _on_write_begin: Callable[..., None] | None
        _on_write_end: Callable[..., None] | None
        _last_status: dict[str, int]
        _rx_seq_gap: int
        _live_loop_thread: threading.Thread | None
        _live_loop_stop: threading.Event
        _live_loop_event: threading.Event
        _live_interval_s: float
        _live_pending: tuple[bytes, str, bool] | None
        _live_latest: tuple[bytes, str] | None
        _live_last_tx: float
        _live_sent: int
        _live_merged: int
        _live_dropped: int
        _live_rtt_ms: float | None
        _imu_loop_thread: threading.Thread | None
        _imu_loop_stop: threading.Event
        _imu_gen: int
        _imu_interval_s: float
        _imu_source: Callable[[], tuple[int, int, int, int, int, int] | None] | None
        _imu_owner: str
        _imu_sent: int
        _imu_dropped: int
        _imu_listeners: list[Callable[[tuple[int, int, int, int, int, int]], None]]
        _tx_listeners: list[Callable[[str], None]]
        _tx_last_note: str | None
        _tx_listener_ng: set[int]

        def send_imu(
            self, ax: int, ay: int, az: int, gx: int, gy: int, gz: int
        ) -> bool: ...

        def _write_frame(
            self,
            frame: bytes,
            row: str,
            measure_perf: bool,
            begin: Callable[..., None] | None,
            end: Callable[..., None] | None,
            ser: Any,
        ) -> bool: ...

    # -- IMU 聞き手・200Hz IMU ループ ----------------------------------

    def _notify_imu(self, sample: tuple[int, int, int, int, int, int]) -> None:
        """送ったIMUサンプルを聞き手へ配る。聞き手が落ちても送信は止めない。"""
        notify_imu_listeners(self._lock, self._imu_listeners, sample)

    def add_imu_listener(self, func: Any) -> bool:
        """送ったIMUサンプルを受け取る相手を足す（記録用）。

        send_imu が実際に送れたときだけ、6値タプルを渡して呼ぶ。重ねて
        足さない。記録は「Pico に届いた値」をそのまま残すために使う。
        """
        return add_unique_listener(self._lock, self._imu_listeners, func)

    def remove_imu_listener(self, func: Any) -> None:
        """IMU の聞き手を外す。例外は投げない。"""
        remove_registered_listener(self._lock, self._imu_listeners, func)

    def imu_loop_owner(self) -> str:
        """いまIMUループを回している持ち主。空なら誰も回していない。"""
        try:
            with self._lock:
                return str(self._imu_owner)
        except Exception:
            return ""

    def imu_loop_running(self) -> bool:
        """IMU loopが回っているか（読むだけ）。例外は投げない。"""
        try:
            thread = self._imu_loop_thread
            return thread is not None and thread.is_alive()
        except Exception:
            return False

    def start_imu_loop(
        self,
        source: Callable[[], tuple[int, int, int, int, int, int] | None],
        interval_s: float = IMU_INTERVAL_S,
        *,
        replace: bool = False,
        owner: str = "",
    ) -> bool:
        """IMU送出loopを1本だけ起こす。動いていれば True。

        sourceは毎刻み呼ぶ生成器で、6値タプルかNoneを返す。Noneの刻みは送らない。
        確認と起動を同じ錠の中で行う。例外は投げない。

        replace=True は、既に動いていても source を差し替える（マクロの
        ジャイロ再生が、パネルの送出を引き継ぐ用途）。旧スレッドは世代
        番号の不一致で抜ける（join は待たない）。

        停止要求中（stop_imu_loopのjoinが0.5秒で諦めた直後など）に
        旧スレッドが生きていても、ここでTrueを返してsourceを差し替え
        ないと「開始したのに送出0」になる。世代番号を進めて新しい
        スレッドを起こし、旧スレッドは自分の世代でないと気づき次第
        抜ける（停止イベントは旧世代のものが既に立っている）。
        """
        try:
            with self._lock:
                thread = self._imu_loop_thread
                if (
                    thread is not None
                    and thread.is_alive()
                    and not self._imu_loop_stop.is_set()
                    and not replace
                ):
                    return True
                self._imu_gen += 1
                generation = self._imu_gen
                self._imu_source = source
                self._imu_interval_s = float(interval_s)
                self._imu_owner = str(owner)
                stop = threading.Event()
                self._imu_loop_stop = stop
                worker = threading.Thread(
                    target=self._imu_loop_run,
                    args=(generation, stop),
                    name="bcon-imu-loop",
                    daemon=True,
                )
                self._imu_loop_thread = worker
            worker.start()
            return True
        except Exception:
            self._logger.debug("IMU loopの起動に失敗", exc_info=True)
            return False

    def stop_imu_loop(self, owner: str | None = None) -> bool:
        """IMU loopを止める。止まるまで待つ（上限0.5秒）。例外は投げない。

        owner を渡したときは、いまの持ち主が一致する場合だけ止める
        （パネルの停止でマクロの再生を巻き込まない・その逆も避ける）。
        owner を省略した停止（close・後始末）は持ち主を問わず止める。
        """
        try:
            # 停止イベントとスレッドを同じ錠で読む。startが並行して
            # 次世代を起こしても、別世代のイベントだけを立てて別の
            # スレッドをjoinする取り違えを避ける。
            with self._lock:
                if owner is not None and self._imu_owner != str(owner):
                    return False
                stop = self._imu_loop_stop
                thread = self._imu_loop_thread
                self._imu_owner = ""
            stop.set()
            if thread is None:
                return True
            thread.join(0.5)
            alive = thread.is_alive()
            if not alive:
                with self._lock:
                    if self._imu_loop_thread is thread:
                        self._imu_loop_thread = None
            return not alive
        except Exception:
            self._logger.debug("IMU loopの停止に失敗", exc_info=True)
            return False

    def _imu_loop_run(self, generation: int, stop: threading.Event) -> None:
        """IMU loop本体。絶対時刻で刻み、遅れは追従しbacklogを作らない。

        live loopと同規律：抑え中は捨ててdroppedに数える。生成器の例外は
        その刻みだけ捨てる。1フレーム1write・SEQ方向別。例外では落ちない。
        stopはこの世代専用のイベント。startが次の世代を起こした場合は
        世代番号の不一致で抜ける（旧sourceで送り続けない）。
        """
        precise_timer(True)
        try:
            try:
                with self._lock:
                    interval = float(self._imu_interval_s)
            except Exception:
                interval = IMU_INTERVAL_S
            next_tx = time.perf_counter() + interval
            while not stop.is_set():
                now = time.perf_counter()
                timeout = next_tx - now
                if timeout > 0:
                    if stop.wait(timeout):
                        break
                if stop.is_set():
                    break
                now = time.perf_counter()
                with self._lock:
                    if self._imu_gen != generation:
                        break
                    held = (
                        self._tx_hold or self._input_hold or not self._hello_established
                    )
                    source = self._imu_source
                if held:
                    with self._lock:
                        self._imu_dropped += 1
                    next_tx = now + interval
                    continue
                if source is None:
                    next_tx = now + interval
                    continue
                try:
                    sample = source()
                except Exception:
                    self._logger.debug("IMU生成器で例外", exc_info=True)
                    next_tx = now + interval
                    continue
                if sample is None:
                    next_tx = now + interval
                    continue
                try:
                    ax, ay, az, gx, gy, gz = (int(v) for v in sample)
                except (TypeError, ValueError):
                    self._logger.debug("IMU生成器の戻りが6値でないため捨てます")
                    next_tx = now + interval
                    continue
                with self._lock:
                    if self._imu_gen != generation:
                        break
                self.send_imu(ax, ay, az, gx, gy, gz)
                now = time.perf_counter()
                if next_tx <= now:
                    next_tx = now + interval
                else:
                    next_tx += interval
        finally:
            precise_timer(False)

    def imu_stats(self) -> dict[str, int]:
        """IMU送出統計の写しを返す（読むだけ・副作用なし）。例外は投げない。"""
        try:
            with self._lock:
                return {"sent": int(self._imu_sent), "dropped": int(self._imu_dropped)}
        except Exception:
            return {"sent": 0, "dropped": 0}

    # -- 120Hz live loop ------------------------------------------------

    def live_loop_running(self) -> bool:
        """120Hz loopが回っているか（読むだけ）。例外は投げない。"""
        try:
            thread = self._live_loop_thread
            return thread is not None and thread.is_alive()
        except Exception:
            return False

    def start_live_loop(self, interval_s: float = LIVE_INTERVAL_S) -> bool:
        """120Hz送出loopを1本だけ起こす。動いていれば True。

        確認と起動を同じ錠の中で行う。二重起動で線が2本になると、
        古い方が止められず漏れる。範囲外の周期は120Hzへ戻す。
        例外は投げない。
        """
        try:
            try:
                interval = float(interval_s)
            except (TypeError, ValueError):
                interval = LIVE_INTERVAL_S
            if math.isnan(interval) or math.isinf(interval):
                interval = LIVE_INTERVAL_S
            interval = max(0.001, min(interval, 0.2))
            with self._lock:
                thread = self._live_loop_thread
                if thread is not None and thread.is_alive():
                    return True
                self._live_interval_s = interval
                self._live_loop_stop.clear()
                self._live_loop_event.clear()
                worker = threading.Thread(
                    target=self._live_loop_run, name="BconLive120", daemon=True
                )
                self._live_loop_thread = worker
                worker.start()
                return True
        except Exception:
            self._logger.debug("live loopの起動に失敗", exc_info=True)
            return False

    def stop_live_loop(self) -> bool:
        """120Hz loopを止める。止まるまで待つ（上限0.5秒）。

        待ちのあいだは錠を持たない。持ったまま join すると、起動側が
        止まるまで待たされる。例外は投げない。
        """
        try:
            self._live_loop_stop.set()
            try:
                self._live_loop_event.set()
            except Exception:
                pass
            with self._lock:
                thread = self._live_loop_thread
            if thread is None:
                return True
            thread.join(0.5)
            alive = thread.is_alive()
            if not alive:
                with self._lock:
                    if self._live_loop_thread is thread:
                        self._live_loop_thread = None
            return not alive
        except Exception:
            self._logger.debug("live loopの停止に失敗", exc_info=True)
            return False

    def live_stats(self) -> dict[str, Any]:
        """送出統計の写しを返す（読むだけ・副作用なし）。

        sentはSTATE送出数（即送＋loop更新）。mergedはloop保留の畳み数
        （最新1件上書き・backlogなし）。droppedは抑え（hunt・baud切替窓）
        で捨てたSTATE数。不正行の破棄は数えない（写像の警告を見る）。
        seq_gapはPico→PC方向のSEQ欠番イベント数（初回不計数）。
        err_crc/err_dropは直近STATUSのPico側累積（link品質の正準）。
        rtt_msは直近pingの往復ms、未計測はNone。openで0へ戻る。
        呼ぶだけで線に触れない。例外は投げない。
        """
        try:
            with self._lock:
                status = dict(self._last_status)
                rtt = self._live_rtt_ms
                return {
                    "sent": int(self._live_sent),
                    "merged": int(self._live_merged),
                    "dropped": int(self._live_dropped),
                    "seq_gap": int(self._rx_seq_gap),
                    "err_crc": int(status.get("err_crc", 0)),
                    "err_drop": int(status.get("err_drop", 0)),
                    "rtt_ms": None if rtt is None else float(rtt),
                }
        except Exception:
            return zero_live_stats()

    def _live_loop_run(self) -> None:
        """120Hz loop本体。絶対時刻で刻み、遅れは追従しbacklogを作らない。

        tkinterのafterには載せない（GUIの詰まりから切り離す）。
        変化はsend_rowがeventで起こすため即送に近い。起床競合で1刻み
        遅れても保留は消さず次刻みで送る（欠落なし）。抑え（_tx_hold）
        が立つあいだは送らず保留を捨てる（hunt・baud切替窓のSTATE化け
        防止。捨て分はdropped）。保留取出しだけ錠内、採番→組立→書込は
        送出の錠で会話と排他（線上のSEQ順＝採番順）、購読者呼出しは錠の外
        （TextSerialTransportと同一規律）。
        1フレーム1write・SEQ方向別。例外では落ちない。
        """
        try:
            with self._lock:
                interval = float(self._live_interval_s)
        except Exception:
            interval = LIVE_INTERVAL_S
        next_tx = time.perf_counter() + interval
        while not self._live_loop_stop.is_set():
            now = time.perf_counter()
            timeout = next_tx - now
            if timeout > 0:
                try:
                    self._live_loop_event.wait(timeout)
                except Exception:
                    self._logger.debug("live loopの待ちで例外", exc_info=True)
            try:
                self._live_loop_event.clear()
            except Exception:
                pass
            if self._live_loop_stop.is_set():
                break
            now = time.perf_counter()
            with self._lock:
                held = self._tx_hold or self._input_hold or not self._hello_established
                pending = self._live_pending
                self._live_pending = None
                latest = self._live_latest
                last_tx = self._live_last_tx
            if held:
                if pending is not None:
                    with self._lock:
                        self._live_dropped += 1
                next_tx = now + interval
                continue
            job: tuple[bytes, str, bool] | None = None
            if pending is not None:
                job = pending
            elif latest is not None and (now - last_tx) >= interval * 0.5:
                # 定期再送の門は半周期で見る。Windowsの待ちは±1ms揺らぐ
                # ため等号ちょうどで比べると1刻みおきに落とし120Hzが
                # 半減する。早めの再送はPico側200ms維持に無害である。
                job = (latest[0], latest[1], False)
            if job is None:
                if next_tx <= now:
                    next_tx = now + interval
                continue
            payload, note, show = job
            # 採番→組立→書込を1単位で包む（会話フレームとの逆転防止）。
            with self._write_lock:
                with self._lock:
                    if self._tx_hold or self._input_hold or not self._hello_established:
                        self._live_dropped += 1
                        continue
                    seq = self._tx_seq
                    self._tx_seq = (self._tx_seq + 1) & 0xFF
                    ser = self.ser
                    begin = self._on_write_begin
                    end = self._on_write_end
                if ser is None:
                    self._logger.debug("bconのlive loopは開いていないため送りません")
                    next_tx = now + interval
                    continue
                try:
                    frame = frame_build(T_STATE, payload, seq)
                except Exception as e:
                    self._logger.debug(f"bconのlive組立に失敗: {e!r}")
                    next_tx = now + interval
                    continue
                sent_ok = bool(self._write_frame(frame, note, show, begin, end, ser))
            now = time.perf_counter()
            with self._lock:
                if sent_ok:
                    self._live_sent += 1
                if not self._input_hold and self._hello_established:
                    self._live_latest = (payload, note)
                    self._live_last_tx = now
            if sent_ok and not held:
                # 実際に送った分だけを配る。開いていない線の分は配らない。
                # 同一行の再送は _notify_tx 側で削る。呼び出しは錠の外。
                self._notify_tx(note if isinstance(note, str) else "")
            next_tx += interval
            if next_tx <= now:
                next_tx = now + interval

    # -- 聞き手（入力ログ・記録・モニタ用。送った行の写しを配る）--

    def add_listener(self, func: Any) -> bool:
        """送信行を受け取る相手を足す。入力ログはここへ繋ぐ。

        配るのは send_row が受けた行の写し（Modifiedのwire行・Pico式S行・
        `end` のいずれか。InputLogger は両書式を読む）。配る時点は
        「実際に送った後」（同期送出の成功時・loop送出の書込成功時）。
        間引きで畳まれた分・抑えで捨てた分・会話フレーム（HELLO等）は
        配らない。Switch に届いていない操作を記録に残すと、ログと実機が
        ずれるため。重ねて足さない（同じ行が2回届く）。
        """
        return add_unique_listener(
            self._lock, self._tx_listeners, func, self._tx_listener_ng
        )

    def remove_listener(self, func: Any) -> None:
        """聞き手を外す。外したら苦情の記録も消す（付け直せる）。"""
        remove_registered_listener(
            self._lock, self._tx_listeners, func, self._tx_listener_ng
        )

    def _notify_tx(self, row: Any) -> None:
        """送った行の写しを聞き手へ配る。聞き手が落ちても送信は止めない。"""

        def _set_note(val: str) -> None:
            self._tx_last_note = val

        notify_tx_listeners(
            self._lock,
            self._tx_listeners,
            self._tx_listener_ng,
            row,
            lambda: self._tx_last_note,
            _set_note,
            self._logger.warning,
        )
