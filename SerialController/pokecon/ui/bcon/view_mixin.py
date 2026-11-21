"""BCON 設定 UI の表示更新・スナップショット反映・取込期限・計測カウンタ。"""

from __future__ import annotations

import logging
import queue
import time
import tkinter as tk
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from pokecon.domain.bcon_status import (
    CAPTURE_DEADLINE_MARGIN_S,
    CAPTURE_MAX_S,
    CAPTURE_MIN_S,
    COLOR_DEFAULTS,
    COLOR_READ_FALLBACK_NOTE,
    SAVE_SUCCESS_TEXT,
    SAVE_TIMEOUT_TEXT,
    STATUS_FLAG_WIRED,
    decode_player_flags,
    decode_player_imu_range,
    decode_player_lamp,
    decode_player_save,
    request_color,
)
from pokecon.domain.connection_state import ConnectionSnapshot
from pokecon.domain.transport.bcon import decode_rumble
from pokecon.domain.ui_inbox import post_latest_to_gui
from pokecon.ui.bcon.helpers import (
    BCON_FLUSH_MAX,
    BCON_MAX_LINES,
    COUNTERS_LOG_INTERVAL_S,
    PLAYER_LAMP_OFF_COLOR,
    PLAYER_LAMP_ON_COLOR,
    update_color_preview,
)

_LOGGER = logging.getLogger("pokecon.ui.BconSetup")


class BconViewMixin:
    """BconSetup の画面更新・スナップショット購読・計測を担当する Mixin。"""

    if TYPE_CHECKING:
        window: Any
        _service: Any
        _command_busy: Callable[[], bool]
        _keyboard_enabled: Callable[[], bool]
        _show_serial: Callable[[], None] | None
        _snapshot: ConnectionSnapshot
        _unsupported: set[str]
        _color_loaded: bool
        inbox: Any
        _visible: bool
        _visibility_revision: int
        _poll_id: Any
        _embedded: bool
        _queue: Any
        _queue_dropped_total: int
        _busy: bool
        _closed: bool
        _poll_ticks: int
        _poll_drained_total: int
        _poll_last_drained: int
        _poll_last_ms: float
        _poll_max_ms: float
        _queue_max_qsize: int
        _rx_counts: dict[int, int]
        _rx_total: int
        _rx_start_t: float
        _counters_log_t: float
        _capture_saved_seen: bool
        _capture_deadline_id: Any
        _btn_cap: Any
        _btn_reconnect: Any
        _btn_status: Any
        _btn_ping: Any
        _btn_winfo: Any
        _btn_w0: Any
        _btn_w1: Any
        _btn_e0: Any
        _btn_e1: Any
        _btn_e2: Any
        _btn_bootsel: Any
        _btn_keys: Any
        _btn_color: Any
        _btn_kb_send: Any
        _btn_kb_release: Any
        _btn_beacon: Any
        _btn_connect: Any
        _btn_hunt: Any
        _btn_retry_quick: Any
        _btn_hunt_quick: Any
        _btn_open_serial: Any
        _seconds_input: Any
        _kb_text: Any
        _kb_interval_input: Any
        _kb_focus_note: Any
        _kb_mode_note: Any
        _color_entries: list[Any]
        _colors: list[Any]
        _color_canvas: Any
        _color_items: list[int]
        _reason_text: Any
        _log: Any
        _result: Any
        _mode_label: Any
        _imu_label: Any
        _controller_type_label: Any
        _vib_label: Any
        _summary: Any
        _actual_baud: Any
        _lamp_items: list[int]
        _lamp_canvas: Any
        _lamps_shown: Any
        _imu_range_shown: Any

        def _reflow_columns(self) -> None: ...
        def _post_result(self, kind: str, value: Any) -> None: ...

    @staticmethod
    def _apply_text(widget: Any, text: str) -> None:
        """文が変わるときだけ configure する（理由は _apply_state と同じ）。"""
        if widget is None:
            return
        try:
            if str(widget.cget("text")) == text:
                return
        except Exception:
            pass
        try:
            widget.configure(text=text)
        except Exception:
            pass

    @staticmethod
    def _apply_state(widget: Any, state: str) -> None:
        """状態が変わるときだけ configure する。

        ttk/tk の configure は同じ値でも再描画を積む。この画面は接続の写しが
        届くたび（PLAYER_INFO は 60Hz）に可否を見直すので、同じ値の入れ直し
        だけで描画が積み上がり、表示中のプレビューの tick が落ちる（実測
        60→39fps）。cget を持たない差し替え画面では従来どおり入れる。
        """
        try:
            if str(widget.cget("state")) == state:
                return
        except Exception:
            pass
        widget.configure(state=state)

    def _set_busy(self) -> None:
        snapshot = self._service.connection_snapshot()
        busy = bool(snapshot.operation) or self._command_busy()
        self._busy = busy
        ready = snapshot.opened and snapshot.phase == "ready" and not busy
        recovery = (
            snapshot.opened and snapshot.phase in {"ready", "failed"} and not busy
        )
        for name in (
            "_btn_cap",
            "_btn_reconnect",
            "_btn_status",
            "_btn_ping",
            "_btn_winfo",
            "_btn_w0",
            "_btn_w1",
            "_btn_e0",
            "_btn_e1",
            "_btn_e2",
            "_btn_bootsel",
            "_btn_keys",
            "_btn_color",
            "_btn_kb_send",
            "_btn_kb_release",
        ):
            self._apply_state(getattr(self, name), tk.NORMAL if ready else tk.DISABLED)
        serial_btn = getattr(self, "_btn_serial", None)
        if serial_btn is not None:
            self._apply_state(serial_btn, tk.NORMAL if ready else tk.DISABLED)
        wired = bool(snapshot.status and (int(snapshot.status[0]) & STATUS_FLAG_WIRED))
        self._apply_state(
            self._btn_keys, tk.NORMAL if ready and not wired else tk.DISABLED
        )
        note = getattr(self, "_keys_note", None)
        if note is not None:
            try:
                if wired:
                    note.grid()
                else:
                    note.grid_remove()
            except tk.TclError:
                pass
        saved = len(snapshot.player_info) >= 2 and decode_player_save(
            snapshot.player_info[1]
        )
        self._apply_state(
            self._btn_beacon, tk.NORMAL if ready and saved else tk.DISABLED
        )
        self._apply_text(
            self._kb_focus_note,
            "キーボード操作が ON です。入力欄の外では打鍵がコントローラ操作にも送られます。"
            if self._keyboard_enabled()
            else "",
        )
        self._apply_text(
            self._kb_mode_note,
            "無線モードでは Switch に届きません。"
            if snapshot.status and not wired
            else "",
        )
        for widget in [
            self._seconds_input,
            self._kb_text,
            self._kb_interval_input,
            *self._color_entries,
        ]:
            self._apply_state(widget, tk.NORMAL if ready else tk.DISABLED)
        for widget in (self._btn_connect, self._btn_hunt):
            self._apply_state(widget, tk.NORMAL if recovery else tk.DISABLED)
        for widget in (
            getattr(self, "_btn_retry_quick", None),
            getattr(self, "_btn_hunt_quick", None),
        ):
            if widget is not None:
                self._apply_state(widget, tk.NORMAL if recovery else tk.DISABLED)
        optional = {
            "状態確認中": (self._btn_status, self._btn_winfo),
            "疎通確認中": (self._btn_ping,),
            "BOOTSEL移行中": (self._btn_bootsel,),
            "無線切替中": (self._btn_w0,),
            "有線切替中": (self._btn_w1,),
            "種別切替中": (self._btn_e0, self._btn_e1, self._btn_e2),
            "Switch再接続要求中": (self._btn_reconnect,),
        }
        for action in self._unsupported:
            for widget in optional.get(action, ()):
                self._apply_state(widget, tk.DISABLED)
        self._update_reason(snapshot)

    def _open_serial(self) -> None:
        callback = getattr(self, "_show_serial", None)
        if callable(callback):
            callback()

    def _update_reason(self, snapshot: ConnectionSnapshot) -> None:
        """主要ボタンが押せない理由と、同じ行の復旧操作を更新する。"""
        busy = bool(snapshot.operation) or self._command_busy()
        if not snapshot.opened:
            reason = "未接続"
            show_serial = callable(getattr(self, "_show_serial", None))
            show_recovery = False
        elif self._command_busy():
            reason = "コマンド実行中"
            show_serial = False
            show_recovery = False
        elif snapshot.operation:
            reason = f"{snapshot.operation}中"
            show_serial = False
            show_recovery = False
        elif snapshot.phase == "failed":
            reason = "HELLO 未確認"
            show_serial = False
            show_recovery = True
        elif snapshot.phase != "ready":
            reason = "接続処理中"
            show_serial = False
            show_recovery = False
        else:
            reason = ""
            show_serial = False
            show_recovery = False
            busy = False
        reason_text = getattr(self, "_reason_text", None)
        if reason_text is not None:
            reason_text.set(reason)
        for name in ("_btn_retry_quick", "_btn_hunt_quick"):
            btn = getattr(self, name, None)
            if btn is None:
                continue
            if show_recovery:
                if hasattr(btn, "grid"):
                    btn.grid()
                self._apply_state(btn, tk.NORMAL if not busy else tk.DISABLED)
            else:
                remover = getattr(btn, "grid_remove", None)
                if callable(remover):
                    remover()
        open_btn = getattr(self, "_btn_open_serial", None)
        if open_btn is not None:
            if show_serial:
                if hasattr(open_btn, "grid"):
                    open_btn.grid()
                self._apply_state(open_btn, tk.NORMAL)
            else:
                remover = getattr(open_btn, "grid_remove", None)
                if callable(remover):
                    remover()
                self._apply_state(open_btn, tk.DISABLED)

    def _admit(self, *, recovery: bool = False) -> bool:
        if self._command_busy():
            self._queue.put(("log", "コマンドを停止してから変更してください。"))
            return False
        snapshot = self._service.connection_snapshot()
        if not snapshot.opened:
            self._queue.put(("log", "未接続です。シリアルタブで接続してください。"))
            return False
        if snapshot.operation:
            return False
        if snapshot.phase != "ready" and not (recovery and snapshot.phase == "failed"):
            self._queue.put(("log", "HELLO をやり直すか、速度を探してください。"))
            return False
        return True

    def _append(self, text: str) -> None:
        try:
            at_bottom = self._log.yview()[1] >= 0.999
        except Exception:
            at_bottom = True
        self._log.configure(state=tk.NORMAL)
        self._log.insert(tk.END, text + "\n")
        self._trim_log()
        if at_bottom:
            try:
                self._log.see(tk.END)
            except Exception:
                pass
        self._log.configure(state=tk.DISABLED)
        # 直近の結果は操作の近くにも出す（ログは追跡用。毎回スクロールしない）。
        result = getattr(self, "_result", None)
        if result is not None:
            try:
                self._apply_text(result, text)
            except Exception:
                pass

    def _trim_log(self) -> None:
        log = getattr(self, "_log", None)
        if log is None:
            return
        try:
            total = int(log.index("end-1c").split(".")[0])
        except Exception:
            return
        if total > BCON_MAX_LINES:
            try:
                log.delete("1.0", f"end-{BCON_MAX_LINES}l")
            except Exception:
                pass

    def _update_player_lamps(
        self, lamp: Any, flags: Any = None, *, known: bool = True
    ) -> None:
        """PLAYER_INFO のランプと IMU/振動を画面へ反映する。

        引数は復号前の生値でよい（lamp・flags の 2 引数、または 2byte の
        payload 1 つ）。b"" や 1byte の短い payload・予約値は全消灯へ倒し、
        例外は投げない。mainloop の無いヘッドレス検査（__new__＋stub）
        からも同じ口で呼べる。作業スレッドから直接触らず、after() 還流の
        先で呼ぶこと。
        """
        if flags is None and isinstance(lamp, (bytes, bytearray, memoryview)):
            payload = bytes(lamp)
            if len(payload) >= 2:
                lamp, flags = payload[0], payload[1]
            else:
                lamp, flags = None, None
        try:
            lamps = decode_player_lamp(lamp) if known else None
        except (TypeError, ValueError):
            # 読めない値は「分からない」扱いにする。
            lamps = None
        items = getattr(self, "_lamp_items", None)
        canvas = getattr(self, "_lamp_canvas", None)
        # 同じ色を入れ直すと Canvas の再描画が積まれる。点灯が変わったときだけ。
        if items is not None and canvas is not None:
            if getattr(self, "_lamps_shown", None) != lamps:
                try:
                    for item, is_on in zip(
                        items, lamps or (False, False, False, False)
                    ):
                        if lamps is None:
                            canvas.itemconfigure(item, fill="", outline="#4D4D4D")
                        else:
                            color = (
                                PLAYER_LAMP_ON_COLOR if is_on else PLAYER_LAMP_OFF_COLOR
                            )
                            canvas.itemconfigure(item, fill=color)
                    self._lamps_shown = lamps
                except Exception:
                    # 閉じた後・stub など、描けない時は色だけ諦める。
                    pass
        # 保存証拠は表示と別に覚える。BEACON では立てない。
        self._note_player_imu_range(flags)

    def _note_player_imu_range(self, flags: Any) -> None:
        """Switch が要求した IMU レンジ(0x41)を変化時だけログへ 1 行出す。

        bit7 は「0x41 を実際に受信したか」。未受信のときの値は既定
        (±2000dps/±8G) なので、要求と取り違えないよう明示する。
        """
        try:
            value = int(flags) & 0xFF
            gyro, accel = decode_player_imu_range(value)
        except (TypeError, ValueError):
            return
        seen = bool(value & 0x80)
        # bit0=0x40(IMU 有効化)受信。0x41 未着でも、こちらが立っていれば
        # USB OUT のサブコマンド経路自体は生きていると判断できる。
        imu_on = bool(value & 0x01)
        shown = (gyro, accel, seen, imu_on)
        if shown == getattr(self, "_imu_range_shown", None):
            return
        self._imu_range_shown = shown
        if seen:
            note = ""
        elif imu_on:
            note = "（未指定のため既定値）"
        else:
            note = "（未接続）"
        try:
            self._append(f"ジャイロ感度: {gyro} / {accel}{note}")
        except Exception:
            pass

    def _note_player_save(self, flags: Any) -> None:
        """PLAYER_INFO flags bit2 を見たら保存済みを覚える。例外は出さない。"""
        try:
            if self._capture_deadline_id is not None and decode_player_save(
                int(flags) & 0xFF
            ):
                self._capture_saved_seen = True
        except (TypeError, ValueError):
            pass

    def _refresh_color_preview(self) -> None:
        """色 4 欄の見た目を小矩形へ映す。打鍵のたびの表示だけ（送らない）。"""
        try:
            update_color_preview(
                getattr(self, "_colors", []),
                getattr(self, "_color_canvas", None),
                getattr(self, "_color_items", []),
            )
        except Exception:
            pass

    def _receive_snapshot(self, _snapshot: ConnectionSnapshot) -> None:
        if self._visible and not self._closed:
            post_latest_to_gui(
                self,
                "connection",
                lambda: self._apply_snapshot(self._service.connection_snapshot()),
            )

    def _receive_log(self, generation: int, text: str) -> None:
        if self._visible and not self._closed:
            self._queue.put(("generation_log", (generation, text)))

    def _apply_snapshot(self, snapshot: ConnectionSnapshot) -> None:
        if not self._visible or self._closed:
            return
        if snapshot.generation != self._service.connection_snapshot().generation:
            return
        previous = self._snapshot
        if snapshot.generation != previous.generation or snapshot.phase in {
            "disconnected",
            "opening",
            "failed",
            "closing",
            "restarting",
            "bootloader",
        }:
            self._cancel_capture_deadline()
            self._capture_saved_seen = False
            self._unsupported.clear()
        self._snapshot = snapshot
        self._update_player_lamps(
            snapshot.player_info,
            known=bool(snapshot.opened and snapshot.player_info),
        )
        if (
            snapshot.player_info != previous.player_info
            and len(snapshot.player_info) >= 2
        ):
            self._note_player_save(snapshot.player_info[1])
        self._render_summary()
        self._set_busy()
        if (
            snapshot.phase == "ready"
            and not snapshot.operation
            and not self._color_loaded
        ):
            self._load_initial_color()

    def _load_initial_color(self) -> None:
        if not self._colors or self._command_busy():
            return
        draft = tuple(var.get() for var in self._colors)
        if draft != COLOR_DEFAULTS:
            self._color_loaded = True
            return
        generation = self._service.connection_snapshot().generation

        def work(transport: Any) -> bool:
            payload = request_color(transport)
            self._post_result("color_prefill", (generation, draft, payload))
            return payload is not None

        self._color_loaded = self._service.run_bcon_operation("本体色読取中", work)

    def _render_summary(self) -> None:
        snapshot = self._snapshot
        opened = bool(snapshot.opened)
        mode = "—"
        if opened:
            mode = "未確認"
            if snapshot.status is not None:
                mode = "有線" if int(snapshot.status[0]) & STATUS_FLAG_WIRED else "無線"
        self._apply_text(getattr(self, "_mode_label", None), mode)
        controller_type = "—"
        if opened and snapshot.phase == "ready":
            if snapshot.controller_role is not None:
                controller_type = {
                    0: "Pro Con",
                    1: "Joy-Con L",
                    2: "Joy-Con R",
                }.get(snapshot.controller_role, "不明")
            elif snapshot.hello_ack is not None and snapshot.hello_ack[1:3] < (0, 16):
                controller_type = "未対応（FW 0.16 以降）"
            else:
                controller_type = "未確認"
        elif opened and snapshot.phase not in {"restarting", "bootloader", "failed"}:
            controller_type = "未確認"
        self._apply_text(getattr(self, "_controller_type_label", None), controller_type)
        imu = "—" if not opened else "未確認"
        if opened and snapshot.player_info:
            try:
                _lamp, flags = snapshot.player_info[0], snapshot.player_info[1]
                imu_ok, _vib = decode_player_flags(flags)
                imu = "あり" if imu_ok else "なし"
            except (TypeError, ValueError, IndexError):
                imu = "未確認"
        self._apply_text(getattr(self, "_imu_label", None), imu)
        if not opened:
            vibration = "—"
        elif snapshot.rumble:
            try:
                left, right = decode_rumble(snapshot.rumble)
                vibration = f"L={left} R={right}"
            except (TypeError, ValueError):
                vibration = "不明"
        else:
            vibration = "未受信"
        self._apply_text(getattr(self, "_vib_label", None), vibration)
        hello = "—" if not opened else "未確認"
        if opened and snapshot.hello_ack is not None:
            adopted, major, minor, result = snapshot.hello_ack
            hello = f"採用 {adopted} / FW {major}.{minor} / 結果 {result}"
        status = "—" if not opened else "未確認"
        if opened and snapshot.status_received_at is not None:
            age = max(0.0, time.monotonic() - snapshot.status_received_at)
            status = f"{age:.0f}秒前" + ("（更新停止）" if age > 3 else "")
        baud = (
            "—"
            if not opened
            else ("未確認" if snapshot.baud is None else f"{snapshot.baud}bps")
        )
        # 同じ文を入れ直すと ttk ラベル（wraplength 付き）が折り返しを
        # 計算し直し、そのたびに画面全体の描画が積まれる。変わったときだけ。
        self._apply_text(
            self._summary,
            f"ポート: {snapshot.port_name or '—'} / {baud}\n"
            f"HELLO: {hello}\nSTATUS: {status}",
        )
        self._apply_text(self._actual_baud, f"実際の速度: {baud}")

    def _arm_capture_deadline(self, seconds: int) -> None:
        """取込期限を張る。要求秒数＋余白の壁時計で 1 発だけ鳴らす。"""
        try:
            wanted = int(seconds)
        except (TypeError, ValueError):
            return
        if wanted < CAPTURE_MIN_S or wanted > CAPTURE_MAX_S:
            return
        self._cancel_capture_deadline()
        self._capture_saved_seen = False
        try:
            delay_ms = (wanted + CAPTURE_DEADLINE_MARGIN_S) * 1000
            self._capture_deadline_id = self.window.after(
                delay_ms, self._on_capture_deadline
            )
        except Exception:
            self._capture_deadline_id = None

    def _cancel_capture_deadline(self) -> None:
        """取込期限の予約を消す。二重・予約なしでも落とさない。"""
        pending = getattr(self, "_capture_deadline_id", None)
        self._capture_deadline_id = None
        if pending is None:
            return
        try:
            cancel = getattr(self.window, "after_cancel", None)
            if callable(cancel):
                cancel(pending)
        except Exception:
            pass

    def _on_capture_deadline(self) -> None:
        """取込期限時に PLAYER_INFO の保存証拠を報告し、期限だけ解除する。"""
        self._capture_deadline_id = None
        if getattr(self, "_closed", False):
            # 閉じ競合でも購読だけは外す（持ち込まない）。
            self._teardown_capture()
            return
        try:
            seen = bool(getattr(self, "_capture_saved_seen", False))
        except Exception:
            seen = False
        # 先に外して残りカスを捨て、完了文だけ残す。
        self._teardown_capture()
        try:
            if seen:
                self._queue.put(("log", SAVE_SUCCESS_TEXT))
            else:
                self._queue.put(("log", SAVE_TIMEOUT_TEXT))
        except Exception:
            pass

    def _drain_stale_capture_entries(self) -> int:
        """取込の残りカスだけ捨てる。log/done は残す。二重安全。

        player_info/rumble/capture_arm は取込期限後の古い届けであり、
        次の取込へ持ち込まない。log/done は画面の履歴のため残す。
        捨てた件数を返す（T1 数えの qsize 証跡の補助）。
        """
        try:
            q = getattr(self, "_queue", None)
            if q is None:
                return 0
        except Exception:
            return 0
        stale = 0
        keep: list[Any] = []
        try:
            while True:
                item = q.get_nowait()
                try:
                    kind, _ = item
                except (TypeError, ValueError):
                    stale += 1
                    continue
                if kind in ("player_info", "rumble", "capture_arm"):
                    stale += 1
                    continue
                keep.append(item)
        except queue.Empty:
            pass
        except Exception:
            pass
        for item in keep:
            try:
                q.put(item)
            except Exception:
                pass
        return stale

    def _teardown_capture(self) -> None:
        self._cancel_capture_deadline()
        self._drain_stale_capture_entries()

    def _ensure_counters(self) -> None:
        # __new__ 素体の検査体でも数えが動くよう欠品分だけ足す。
        if getattr(self, "_rx_counts", None) is None:
            self._rx_counts = {}
        for name, fresh in (
            ("_poll_ticks", 0),
            ("_poll_drained_total", 0),
            ("_poll_last_drained", 0),
            ("_poll_last_ms", 0.0),
            ("_poll_max_ms", 0.0),
            ("_queue_max_qsize", 0),
            ("_queue_dropped_total", 0),
            ("_rx_total", 0),
            ("_rx_start_t", 0.0),
            ("_counters_log_t", 0.0),
        ):
            if not hasattr(self, name):
                setattr(self, name, fresh)

    def counters_snapshot(self) -> dict[str, Any]:
        """H1/H2/H3 切り分け用の写し。呼ぶだけで線・画面に触れない。"""
        self._ensure_counters()
        now = time.perf_counter()
        start = float(self._rx_start_t)
        span = now - start if start > 0.0 else 0.0
        by_ftype = dict(self._rx_counts)
        per_sec = (
            {ftype: count / span for ftype, count in by_ftype.items()}
            if span > 0.0
            else dict.fromkeys(by_ftype, 0.0)
        )
        return {
            "poll_ticks": int(self._poll_ticks),
            "poll_drained_total": int(self._poll_drained_total),
            "poll_last_drained": int(self._poll_last_drained),
            "poll_last_ms": float(self._poll_last_ms),
            "poll_max_ms": float(self._poll_max_ms),
            "queue_max_qsize": int(self._queue_max_qsize),
            "queue_dropped_total": int(self._queue_dropped_total),
            "rx_total": int(self._rx_total),
            "rx_by_ftype": by_ftype,
            "rx_per_sec_by_ftype": per_sec,
        }

    def _maybe_log_counters(self) -> None:
        # debug への書き出しは間引く（既定では出ない・出しても 5 秒に 1 件）。
        try:
            now = time.perf_counter()
            if now - float(self._counters_log_t) < COUNTERS_LOG_INTERVAL_S:
                return
            self._counters_log_t = now
            _LOGGER.debug(
                "bcon計測 tick=%d drain=%d/%d poll_ms=%.3f(max %.3f) qmax=%d rx=%s",
                int(self._poll_ticks),
                int(self._poll_last_drained),
                int(self._poll_drained_total),
                float(self._poll_last_ms),
                float(self._poll_max_ms),
                int(self._queue_max_qsize),
                dict(self._rx_counts),
            )
        except Exception:
            pass

    def _poll(self) -> None:
        self._poll_id = None
        if self._closed or not self._visible:
            return
        self.inbox.drain()
        start = time.perf_counter()
        self._ensure_counters()
        self._queue_max_qsize = max(self._queue_max_qsize, self._queue.qsize())
        current = self._service.connection_snapshot().generation
        drained = 0
        for _ in range(BCON_FLUSH_MAX):
            try:
                kind, value = self._queue.get_nowait()
            except queue.Empty:
                break
            drained += 1
            if kind == "generation_log":
                generation, text = value
                if generation == current:
                    self._append(text)
            elif kind == "unsupported":
                generation, action = value
                if generation == current:
                    self._unsupported.add(action)
            elif kind == "color_prefill":
                generation, draft, payload = value
                if (
                    generation == current
                    and tuple(var.get() for var in self._colors) == draft
                ):
                    if payload is None:
                        self._append(COLOR_READ_FALLBACK_NOTE)
                    else:
                        for index, var in enumerate(self._colors):
                            var.set(bytes(payload[index * 3 : index * 3 + 3]).hex())
                        self._refresh_color_preview()
            elif kind == "success":
                # 操作が成功したあとの後始末。GUI スレッドなので Tk を触れる。
                try:
                    value()
                except Exception:
                    _LOGGER.exception("BCON操作の成功後処理に失敗しました")
            elif kind == "log":
                self._append(str(value))
            elif kind == "capture_cancel":
                generation, revision = value
                if generation == current and revision == self._visibility_revision:
                    self._cancel_capture_deadline()
            elif kind == "capture_arm":
                generation, revision, seconds, sent_at = value
                if generation == current and revision == self._visibility_revision:
                    remaining = max(
                        0,
                        seconds
                        + CAPTURE_DEADLINE_MARGIN_S
                        - (time.monotonic() - sent_at),
                    )
                    self._cancel_capture_deadline()
                    self._capture_deadline_id = self.window.after(
                        int(remaining * 1000), self._on_capture_deadline
                    )
        dropped = self._queue.take_dropped()
        if dropped:
            self._queue_dropped_total += dropped
            self._append(f"... {dropped}行省略 ...")
        self._render_summary()
        self._set_busy()
        if self._embedded:
            self._reflow_columns()
        self._note_poll_tick(start, drained)
        self._poll_id = self.window.after(120, self._poll)

    def _note_poll_tick(self, tick_start: float, drained: int) -> None:
        # 1 tick の吐き出し数と壁 ms を残す（H2 切り分け用・GUI 側のみ）。
        try:
            elapsed_ms = (time.perf_counter() - tick_start) * 1000.0
            self._poll_ticks += 1
            self._poll_drained_total += drained
            self._poll_last_drained = drained
            self._poll_last_ms = elapsed_ms
            if elapsed_ms > self._poll_max_ms:
                self._poll_max_ms = elapsed_ms
        except Exception:
            pass
        self._maybe_log_counters()
