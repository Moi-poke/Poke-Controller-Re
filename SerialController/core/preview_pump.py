"""プレビュー描画を Tk のメインループから切り離す描画スレッド（tkinter 非依存）。

設計: docs/superpowers/specs/2026-09-26-gdi-preview-renderer-design.md
（改訂: docs/superpowers/specs/2026-10-30-frame-driven-preview-pump-design.md）

これまでの描画は native clock が Tk のメインスレッドへ 1 tick を渡し、
その場で compose + present していた。Tk がログ描画・コマンド・入力処理で
数十 ms 止まると tick が遅れ、latest-only のフレーム箱から読むので
その間のフレームが落ちる（実測: メインスレッドに 200ms ごと 40ms の
仕事を入れると 60fps → 48fps）。

ここでは描画を専用スレッドへ移し、**フレームの公開で起きる**。取込と
表示が同じ 60fps でも、時刻で刻むと位相のずれで「空振り」と「1 枚飛ばし」が
交互に出る。公開で起きればその 2 つは原理的に起きない。表示 fps の設定は
「公開から次の期限までの待ち」で上限として掛ける。

Tk・ウィジェット・Tk 変数には触れない。描画対象（GdiSurface）は子窓へ
直接描くのでスレッドを選ばない。Tk のウィジェットへ描く面（photo）は
``cross_thread_render`` が False で、このスレッドは使わない。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final

from loguru import logger

#: 表示上限の判定に使う許容。取込と表示が同じ fps のとき、公開の揺らぎ
#: （実測 p99 で数 ms）で「まだ期限前」と判定して 1 枚捨てるのを防ぐ。
#: 0 にすると 60fps 同士でも半分ほど落ちる（実測 58.7fps）。
_GATE_TOLERANCE_S: Final[float] = 0.0015
#: 購読 API を持たないフレーム源（試験用の替身など）を回すときの待ち。
_FALLBACK_POLL_S: Final[float] = 0.002
#: 待ちの上限。停止要求・購読の取りこぼしに備えた安全弁で、通常は
#: フレーム公開か notify() で先に起きる。
_MAX_WAIT_S: Final[float] = 0.25
#: stop() が join を待つ上限。終了階段（Window.EXIT_PREVIEW_RETRY_MAX）は
#: これより短い 50ms 間隔で再試行するので、1 回で長く待たない。
JOIN_TIMEOUT_S: Final[float] = 0.5


@dataclass(frozen=True, slots=True)
class PumpHealth:
    """描画スレッドの観測値。証跡（_preview_evidence_snapshot）用。"""

    running: bool
    active: bool
    frames_drawn: int
    frames_dropped_rate: int
    frames_none: int
    draw_errors: int
    draw_ms: float
    draw_max_ms: float
    last_sequence: int


class PreviewPump:
    """フレーム公開で起きる描画スレッド。

    ``read_frame`` は ``(frame, seq)`` を返す（CaptureArea._readLatest）。
    ``draw`` は ``(frame, seq)`` を受けて 1 枚出せたら True を返す
    （CaptureArea._drawFrame）。重複判定は draw 側が持つので、ここは
    「起きる条件」と「表示 fps の上限」だけを扱う。
    """

    def __init__(
        self,
        source: Any,
        read_frame: Callable[[], tuple[Any, int | None]],
        draw: Callable[[Any, int | None], bool],
        target_fps: int,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        self._source = source
        self._read_frame = read_frame
        self._draw_frame = draw
        self._clock = clock
        self._cond = threading.Condition()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._period_s = 1.0 / max(1, int(target_fps))
        self._active = False
        self._subscribed = False
        # 統計は getStats() が Tk スレッドから読むので専用の錠で守る。
        self._stats_lock = threading.Lock()
        self._shown = 0
        self._began_at: float | None = None
        self._draw_ms = 0.0
        self._draw_max_ms = 0.0
        # 証跡用の単調カウンタ（取り合いになっても 1 増えるだけ）。
        self._frames_drawn = 0
        self._frames_dropped_rate = 0
        self._frames_none = 0
        self._draw_errors = 0
        self._last_sequence = -1
        self._error_logged = False

    # -- 公開 API -----------------------------------------------------------

    def start(self) -> bool:
        """描画スレッドを始める。既に動いていれば何もしない。"""
        with self._cond:
            if self._thread is not None and self._thread.is_alive():
                return True
            self._stop.clear()
            thread = threading.Thread(
                target=self._run,
                name="PreviewPump",
                daemon=True,
            )
            self._thread = thread
        subscribe = getattr(self._source, "subscribe_frames", None)
        if callable(subscribe):
            subscribe(self.notify)
            self._subscribed = True
        try:
            thread.start()
        except (OSError, RuntimeError):
            logger.exception("プレビュー描画スレッドを起動できませんでした")
            self._unsubscribe()
            with self._cond:
                if self._thread is thread:
                    self._thread = None
            return False
        return True

    def stop(self) -> bool:
        """止めて join する。スレッドが残ったら False（面を解放してはならない）。"""
        self._stop.set()
        self.notify()
        thread = self._thread
        if thread is None:
            self._unsubscribe()
            return True
        thread.join(JOIN_TIMEOUT_S)
        if thread.is_alive():
            return False
        self._unsubscribe()
        with self._cond:
            if self._thread is thread:
                self._thread = None
        return True

    def _unsubscribe(self) -> None:
        if not self._subscribed:
            return
        self._subscribed = False
        unsubscribe = getattr(self._source, "unsubscribe_frames", None)
        if callable(unsubscribe):
            unsubscribe(self.notify)

    def notify(self) -> None:
        """状態が変わった（オーバーレイ・表示の有無・面の大きさ）ことを伝える。"""
        with self._cond:
            self._cond.notify_all()

    def set_active(self, active: bool) -> None:
        with self._cond:
            if active != self._active:
                self._active = active
                self._cond.notify_all()

    def set_fps(self, fps: int) -> None:
        period = 1.0 / max(1, int(fps))
        with self._cond:
            self._period_s = period
            self._cond.notify_all()

    def take_stats(self) -> dict[str, float]:
        """表示実測を返し、区切り直す（CaptureArea.getStats と同じ形）。"""
        now = self._clock()
        with self._stats_lock:
            shown, began_at = self._shown, self._began_at
            draw_ms, draw_max_ms = self._draw_ms, self._draw_max_ms
            self._shown = 0
            self._began_at = None
            self._draw_max_ms = 0.0
        if began_at is None or shown <= 0:
            return {"fps": 0.0, "draw_ms": round(draw_ms, 1), "draw_max_ms": 0.0}
        elapsed = now - began_at
        if elapsed < 1e-6:
            return {"fps": 0.0, "draw_ms": round(draw_ms, 1), "draw_max_ms": 0.0}
        return {
            "fps": round(shown / elapsed, 1),
            "draw_ms": round(draw_ms, 1),
            "draw_max_ms": round(draw_max_ms, 1),
        }

    def health_snapshot(self) -> dict[str, bool | int | float]:
        with self._cond:
            running = self._thread is not None and self._thread.is_alive()
            active = self._active
        with self._stats_lock:
            draw_ms, draw_max_ms = self._draw_ms, self._draw_max_ms
        return {
            "pump_running": running,
            "pump_active": active,
            "pump_frames_drawn": self._frames_drawn,
            "pump_frames_dropped_rate": self._frames_dropped_rate,
            "pump_frames_none": self._frames_none,
            "pump_draw_errors": self._draw_errors,
            "pump_draw_ms": round(draw_ms, 3),
            "pump_draw_max_ms": round(draw_max_ms, 3),
            "pump_last_sequence": self._last_sequence,
        }

    def pump_health(self) -> PumpHealth:
        snapshot = self.health_snapshot()
        return PumpHealth(
            running=bool(snapshot["pump_running"]),
            active=bool(snapshot["pump_active"]),
            frames_drawn=int(snapshot["pump_frames_drawn"]),
            frames_dropped_rate=int(snapshot["pump_frames_dropped_rate"]),
            frames_none=int(snapshot["pump_frames_none"]),
            draw_errors=int(snapshot["pump_draw_errors"]),
            draw_ms=float(snapshot["pump_draw_ms"]),
            draw_max_ms=float(snapshot["pump_draw_max_ms"]),
            last_sequence=int(snapshot["pump_last_sequence"]),
        )

    # -- スレッド本体 -------------------------------------------------------

    def _run(self) -> None:
        last_handled: int | None = None
        deadline = self._clock()
        while not self._stop.is_set():
            # 1 回分の失敗でスレッドを死なせない。死ぬと以後 1 枚も出ない
            # まま、画面は最後の絵で固まって見える。原因は 1 度だけ記録する。
            try:
                last_handled, deadline = self._step(last_handled, deadline)
            except Exception as exc:
                self._note_error("描画ループ", exc)
                time.sleep(0.05)

    def _step(
        self, last_handled: int | None, deadline: float
    ) -> tuple[int | None, float]:
        """1 回分の判断と描画。戻り値は (最後に扱った世代, 次の期限)。

        スレッドを起動しないで規則を確かめられるよう、待ちだけを
        ``_wait`` に切り出してある（tests/test_preview_pump.py）。
        """
        if self._stop.is_set():
            # 停止要求の後に 1 枚描かない。終了時に停止画像が一瞬出るのを
            # 避ける（旧来の _dispatch_tick も停止要求で即 return していた）。
            return last_handled, deadline
        with self._cond:
            active = self._active
            period = self._period_s
        if not active:
            self._wait(last_handled)
            return last_handled, self._clock() + period
        frame, seq = self._read()
        now = self._clock()
        new_frame = frame is not None and (seq is None or seq != last_handled)
        if frame is None:
            # カメラ停止中の絵。重複は draw 側が省く。
            self._draw(frame, seq)
            # 世代は観測したので、次の待ちはその世代基準にする。ここを
            # None のままにすると ``wait_for_frame(-1)`` が常に即時 true に
            # なり、停止画像を描くだけの空転で 1 コアを焼き切る
            # （実測 2026-10-30: 数秒で 100 万回）。
            if seq is not None:
                last_handled = seq
        elif new_frame:
            if now + _GATE_TOLERANCE_S >= deadline:
                self._draw(frame, seq)
                deadline += period
                # 大きく遅れたら基準を取り直す。溜まった期限を連射しない。
                if deadline <= now:
                    deadline = now + period
            else:
                # 表示 fps の上限に掛かった。この 1 枚は出さない。
                self._frames_dropped_rate += 1
            last_handled = seq
        else:
            # 新しいフレームは無いが、起きた（オーバーレイ・停止画像の
            # 更新）。draw 側の重複判定で、変化が無ければ何もしない。
            self._draw(frame, seq)
        self._wait(last_handled)
        return last_handled, deadline

    def _read(self) -> tuple[Any, int | None]:
        try:
            return self._read_frame()
        except Exception as exc:
            self._note_error("フレームの読み取り", exc)
            return None, None

    def _draw(self, frame: Any, seq: int | None) -> None:
        if frame is None:
            self._frames_none += 1
        started = self._clock()
        try:
            shown = bool(self._draw_frame(frame, seq))
        except Exception as exc:
            self._note_error("描画", exc)
            return
        elapsed_ms = (self._clock() - started) * 1000.0
        with self._stats_lock:
            if self._draw_ms <= 0.0:
                self._draw_ms = elapsed_ms
            else:
                self._draw_ms = self._draw_ms * 0.9 + elapsed_ms * 0.1
            if elapsed_ms > self._draw_max_ms:
                self._draw_max_ms = elapsed_ms
            if shown:
                self._shown += 1
                if self._began_at is None:
                    self._began_at = started
        if shown:
            self._frames_drawn += 1
        if seq is not None:
            self._last_sequence = seq

    def _note_error(self, what: str, exc: BaseException) -> None:
        self._draw_errors += 1
        # 60fps で毎回同じ例外が出るとファイルログが埋まり、原因の 1 行が
        # 埋もれる。最初の 1 回だけスタック付きで出す。
        if self._error_logged:
            return
        self._error_logged = True
        logger.opt(exception=exc).error("プレビュー描画スレッドで例外（{}）", what)

    def _wait(self, seen: int | None) -> None:
        """次のフレーム公開か notify() まで待つ。

        フレーム源が ``wait_for_frame`` を持つ（core.Camera）なら、その
        Condition で公開の瞬間に起きる。持たない替身では自分の Condition を
        短い周期で見る（本番の経路ではない）。
        """
        wait_for_frame = getattr(self._source, "wait_for_frame", None)
        if callable(wait_for_frame) and seen is not None:
            try:
                wait_for_frame(int(seen), _MAX_WAIT_S)
                return
            except Exception as exc:
                self._note_error("フレーム待ち", exc)
                return
        # 世代を持たない源（試験用の替身）では、待つべき変化の鍵が無い。
        # -1 で待つと述語が常に真になり空転するので、短い周期で見る。
        with self._cond:
            self._cond.wait(_FALLBACK_POLL_S)
