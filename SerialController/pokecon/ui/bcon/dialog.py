"""BCON 設定ダイアログ／埋め込みタブ本体（ライフサイクルと組み立て）。"""

from __future__ import annotations

import queue
import threading
import tkinter as tk
from collections.abc import Callable
from typing import Any

from pokecon.domain.ui_inbox import UiInbox
from pokecon.runtime.serial_service import SerialService
from pokecon.ui.LogPane import DropOldestQueue
from pokecon.ui.bcon.helpers import BCON_QUEUE_MAX, BCON_WINDOW_TITLE
from pokecon.ui.bcon.layout_mixin import BconLayoutMixin
from pokecon.ui.bcon.ops_mixin import BconOpsMixin
from pokecon.ui.bcon.view_mixin import BconViewMixin


class BconSetup(BconLayoutMixin, BconViewMixin, BconOpsMixin):
    """bcon 設定の小窓。1 つだけ開く前提で使う。"""

    def __init__(
        self,
        master: Any,
        service: SerialService,
        *,
        embedded: bool = False,
        command_busy: Callable[[], bool] | None = None,
        keyboard_enabled: Callable[[], bool] | None = None,
        on_show_serial: Callable[[], None] | None = None,
        on_emulate_role: Callable[[int], None] | None = None,
        success_dispatch: Callable[[Callable[[], None]], None] | None = None,
        viewport: Any = None,
    ) -> None:
        """接続と会話の所有者はサービス。画面は写しと操作要求だけを扱う。

        on_emulate_role は種別切替（ProCon / Joy-Con L / R）が成功した
        あとに GUI スレッドで呼ぶ。仮想コントローラの形を合わせるために使う。
        success_dispatch は成功後の処理を GUI スレッドへ渡す口（スレッド安全
        であること）。渡さないときは自分の _poll で実行する。
        """
        self._service = service
        self._command_busy = command_busy or (lambda: False)
        self._keyboard_enabled = keyboard_enabled or (lambda: True)
        self._kb_cancel = threading.Event()
        self._kb_pending = threading.Event()
        self._show_serial = on_show_serial
        self._on_emulate_role = on_emulate_role
        self._success_dispatch = success_dispatch
        self._viewport = viewport
        self._snapshot = service.connection_snapshot()
        self._unsupported: set[str] = set()
        self._color_loaded = False
        self.inbox = UiInbox()
        self._visible = False
        self._visibility_revision = 0
        self._poll_id: Any = None
        self._connection_unsub: Callable[[], None] | None = None
        self._log_unsub: Callable[[], None] | None = None
        self._embedded = embedded
        self._queue: DropOldestQueue = DropOldestQueue(maxsize=BCON_QUEUE_MAX)
        self._queue_dropped_total = 0
        self._busy = False
        self._stop = False
        self._closed = False
        # 常時計測の素朴な数え（H1/H2/H3 切り分け用・永置）。
        # int/float/perf_counter だけで、per-frame の I/O はしない。
        self._poll_ticks = 0
        self._poll_drained_total = 0
        self._poll_last_drained = 0
        self._poll_last_ms = 0.0
        self._poll_max_ms = 0.0
        self._queue_max_qsize = 0
        self._rx_counts: dict[int, int] = {}
        self._rx_total = 0
        self._rx_start_t = 0.0
        self._counters_log_t = 0.0
        # 取込期限の保存証拠と after 予約。期限は要求秒数＋余白の壁時計。
        self._capture_saved_seen = False
        self._capture_deadline_id: Any = None

        if embedded:
            # タブ枠は Notebook が持つ。close で枠ごと壊さず中身（_body）だけ壊す
            self.window = master
        else:
            self.window = tk.Toplevel(master)
            self.window.title(BCON_WINDOW_TITLE)
            self.window.protocol("WM_DELETE_WINDOW", self.close)

        self._build_ui(embedded)
        self.set_visible(True)

    def close(self) -> None:
        """窓を閉じる。二重に呼ばれても安全にする。"""
        if getattr(self, "_closed", False):
            return
        self._kb_cancel.set()
        # 送信中ならそのジョブの finally が離鍵する。待たずに Tk を閉じる。
        if not self._kb_pending.is_set():
            self._service.run_bcon_operation("キー解放", self._release_keyboard)
        self.set_visible(False)
        self.inbox.close()
        self._closed = True
        self._stop = True
        # 取込期限の予約を消す。閉じた後の期限表示は出さない。
        self._cancel_capture_deadline()
        # 溜まった届けは捨てる。_poll は止まるため残すと 1 件漏れる。
        # 使用中も戻す。残したまま閉じると次に開いた窓が塞がったままになる。
        try:
            while True:
                self._queue.get_nowait()
        except queue.Empty:
            pass
        except Exception:
            pass
        self._busy = False
        # 埋め込みは枠（タブ）を残して中身だけ壊す。別窓は窓ごと壊す。
        target = self._body if getattr(self, "_embedded", False) else self.window
        try:
            if target.winfo_exists():
                target.destroy()
        except Exception:
            pass

    def set_visible(self, visible: bool) -> None:
        """非表示時は購読と予約を外し、下書きと表示済みログを保持する。"""
        if self._closed or self._visible == visible:
            return
        self._visible = visible
        self._visibility_revision += 1
        if not visible:
            self._kb_cancel.set()
            for name in ("_connection_unsub", "_log_unsub"):
                unsubscribe = getattr(self, name)
                setattr(self, name, None)
                if unsubscribe is not None:
                    unsubscribe()
            self._cancel_capture_deadline()
            if self._poll_id is not None:
                self.window.after_cancel(self._poll_id)
                self._poll_id = None
            return
        self._connection_unsub = self._service.subscribe_connection(
            self._receive_snapshot
        )
        self._log_unsub = self._service.subscribe_connection_log(self._receive_log)
        self._poll()
