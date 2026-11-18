#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""serial_panel.py - シリアル枠・操作枠の組み立てと接続手順の画面側。

PokeControllerApp に混ぜて使う（多重継承）。開く手順そのものは
services.SerialService が持ち、ここでは確認ダイアログ・tk 変数の
読み書き・見た目の反映だけを行う。
"""

from __future__ import annotations

import threading
import time as time
import tkinter as tk
import tkinter.messagebox as tkmsg
import tkinter.ttk as ttk
from typing import Any

from loguru import logger
from pokecon.domain.connection_state import ConnectionSnapshot
from pokecon.domain.transport.bcon_baud import (
    BAUD_DEFAULT_INDEX,
    baud_bps_for_index,
    baud_combo_state,
    baud_combo_values,
    baud_index_for_label,
    baud_index_sendable,
    baud_label_for_index,
)
from pokecon.domain.transport.bcon_protocol import BAUD_TABLE
from pokecon.domain.transport.registry import (
    canonical_transport_name,
    describe_transports,
)
from pokecon.domain.ui_inbox import post_latest_to_gui, post_to_gui
from pokecon.runtime.serial_service import SenderSpec, SerialService
from pokecon.ui import WindowUtils
from pokecon.ui.collapsible import CollapsibleSection
from pokecon.ui.combo_fit import fit_combo_width
from pokecon.ui.controller_panel import (
    ControllerPanelMixin,
    _is_text_input as _is_text_input,
)
from pokecon.ui.tooltip import Tooltip
from pokecon.ui.transport_names import display_name as transport_display_name

#: 設定タブの最低の高さ（px）。内容が短くても、タブ欄が数行しか見えない
#: 高さまで縮むと操作できない。窓が小さいときはスクロールで届く。
_TAB_MIN_HEIGHT = 280

#: 入力調停の候補が意味する動作。送信側の実装（core/serial/arbitration.py）
#: に合わせて書く。未知の候補は名前だけを出す（意味を推測しない）。
_ARBITRATION_HINTS = {
    "off": "どちらも断りません（本家と同じ挙動）。",
    "script": "コマンド実行中は手動入力を断ります（一時停止すれば操作できます）。",
    "human": "手で触った直後は、スクリプトの操作を断ります。",
}
#: 画面に出す名前。設定ファイルの内部値（off/human/script）は変えない。
_ARBITRATION_LABELS = {
    "off": "両方受け付ける（本家）",
    "human": "手動を優先",
    "script": "コマンドを優先",
}
_ARBITRATION_KEYS = {label: key for key, label in _ARBITRATION_LABELS.items()}


class SerialPanelMixin(ControllerPanelMixin):
    """シリアルパネルMixin。単体では使わない。"""

    frame_1: Any
    tab_serial: Any
    tab_controller: Any
    root: Any
    settings: Any
    os_name: str
    serial: SerialService
    transport_name: Any
    arbitration_mode: Any
    serial_lf: Any
    com_port_label: Any
    com_port: Any
    com_port_name: Any
    com_port_text: Any
    _com_port_map: dict[str, str]
    com_port_cb: Any
    baud_rate_label: Any
    baud_rate: Any
    bcon_baud: Any
    baud_rate_cb: Any
    baud_rate_state: str
    reloadComPort: Any
    disconnectComPort: Any
    is_show_serial: Any
    cb_show_serial: Any
    transport_label: Any
    transport_cb: Any
    arbitration_label: Any
    arbitration_cb: Any
    player_lamp_label: Any
    player_lamp_canvas: Any
    rumble_label: Any
    _player_lamp_items: list
    _player_lamp_after: Any
    bcon_row: Any
    control_lf: Any
    is_use_keyboard: Any
    _kb_window_active: Any
    cb_use_keyboard: Any
    is_use_gamepad: Any
    _gamepad_active: Any
    cb_use_gamepad: Any
    _hidhide_gamepad_ok: bool
    gamepad_label: Any
    gamepad_choice: Any
    gamepad_cb: Any
    gamepad_reconnect_btn: Any
    gamepad_dz_label: Any
    gamepad_dz: Any
    _gamepad_after: Any
    _gamepad_ids: dict[int, str]
    _gamepad_names: dict[int, str]
    _gamepad_identity: str
    _gamepad_name: str
    _gamepad_reconnect_busy: bool
    _gamepad_reconnect_thread: threading.Thread | None
    _gamepad_skip_auto_restart: bool
    gamepad_dz_spin: Any
    layout_label: Any
    layout_cb: Any
    controller_layout_choice: Any
    _controller_layout_labels: dict[str, str]
    cb_left_stick_mouse: Any
    cb_right_stick_mouse: Any
    simpleConButton: Any
    controller_dock: Any
    preview: Any
    camera_lf: Any
    _on_setting_changed: Any
    _update_title: Any
    _refresh_bcon_tab: Any
    select_bcon_tab: Any
    com_port_notice: Any
    runner: Any
    _connection_view: ConnectionSnapshot
    connection_details: Any
    transport_description: Any
    refreshComPort: Any
    bcon_hunt_button: Any

    def _build_serial_frame(self) -> None:
        self._connection_view = ConnectionSnapshot()
        self._connection_unsubscribe: Any = None
        self._connection_log_unsubscribe: Any = None
        self._pending_transport_switch: tuple[int, str, bool, str] | None = None
        # 1 列で「状態 → 主要操作 → 詳細」の順に並べる。項目名は入力欄の
        # 上へ置く（左に並べると視線移動が長く、狭い幅で入力欄が痩せる。
        # Penzo 2006 の視線計測 / デジタル庁デザインシステム）。
        try:
            ttk.Style(self.root).configure(
                "Accent.TButton", foreground="#0B57D0", font=("", 9, "bold")
            )
        except tk.TclError:
            pass
        self.serial_lf = ttk.Frame(self.tab_serial, padding=(8, 4))
        self.serial_lf.columnconfigure(0, weight=1)

        # 接続状態は下端の状態バーへ出す（タブ内に常時表示しない）。
        # ここには失敗理由・復旧操作だけを、起きたときに出す。
        # 失敗理由・復旧操作（成功時は空にして高さを空けない）。
        self.com_port_notice = ttk.Label(self.serial_lf, wraplength=300, justify="left")
        self.com_port_notice.grid(row=1, column=0, sticky="ew", pady=(0, 4))
        self.com_port_notice.grid_remove()

        # 通信方式とポートは、広いときは左右、狭いときは縦に並べる。
        # 縦を詰めたいが、狭い幅で横に詰めると入力欄が痩せるため。
        self._setup_row = ttk.Frame(self.serial_lf)
        self._setup_row.grid(row=2, column=0, sticky="ew", pady=(4, 4))
        self._transport_block = ttk.Frame(self._setup_row)
        self._transport_block.columnconfigure(0, weight=1)
        self.transport_label = ttk.Label(self._transport_block, text="通信方式")
        self.transport_label.grid(row=0, column=0, sticky="w")
        self.transport_cb = ttk.Combobox(self._transport_block)
        self.transport_cb.config(state="readonly", width=20)
        self.transport_cb.grid(row=1, column=0, sticky="ew")
        self.transport_cb.bind("<<ComboboxSelected>>", self.applyTransport, add="")

        self.com_port = tk.IntVar()
        self.com_port_name = tk.StringVar()
        # Combobox に出す表示名（'COM3: USB シリアル デバイス'）を持つ。
        # 実際に開くデバイス名は com_port_name、末尾の番号は com_port。
        self.com_port_text = tk.StringVar()
        self._com_port_map: dict[str, str] = {}
        self._port_block = ttk.Frame(self._setup_row)
        self._port_block.columnconfigure(0, weight=1)
        self.com_port_label = ttk.Label(self._port_block, text="ポート")
        self.com_port_label.grid(row=0, column=0, sticky="w")
        port_row = ttk.Frame(self._port_block)
        port_row.grid(row=1, column=0, sticky="ew")
        port_row.columnconfigure(0, weight=1)
        self._port_row = port_row
        self.com_port_cb = ttk.Combobox(port_row)
        self.com_port_cb.config(
            state="readonly", textvariable=self.com_port_text, width=16
        )
        self.com_port_cb.grid(row=0, column=0, sticky="ew")
        self.refreshComPort = ttk.Button(
            port_row, text="一覧更新", command=self.refreshComPorts
        )
        self.refreshComPort.grid(row=0, column=1, padx=(4, 0), sticky="e")
        self.port_empty = ttk.Label(self._port_block, text="", foreground="#6B6B6B")
        self.port_empty.grid(row=2, column=0, columnspan=2, sticky="w", pady=(4, 0))
        self.port_empty.grid_remove()
        Tooltip(
            self.refreshComPort,
            "COM ポートの一覧を更新する。ポートが無いときは USB 接続を確認する。",
        )
        self.com_port_cb.bind("<<ComboboxSelected>>", self.onComPortSelected, add="")

        # 主要操作。接続と再接続は同じボタン（状態で文字が変わる）。
        actions = ttk.Frame(self.serial_lf)
        actions.grid(row=3, column=0, sticky="ew", pady=(4, 4))
        actions.columnconfigure(0, weight=1)
        self.connect_hint = ttk.Label(
            actions, text="", foreground="#6B6B6B", wraplength=220
        )
        self.connect_hint.grid(row=0, column=0, sticky="w")
        buttons = ttk.Frame(actions)
        buttons.grid(row=0, column=1, sticky="e")
        self.reloadComPort = ttk.Button(
            buttons, command=self.reloadSerialPort, width=14
        )
        self.reloadComPort.config(text="接続")
        self.reloadComPort.pack(side="left")
        self.disconnectComPort = ttk.Button(
            buttons, text="切断", command=self.inactivateSerial
        )
        self.disconnectComPort.pack(side="left", padx=(8, 0))
        # 操作結果（失敗理由を含む）は押したボタンの直下へ。詳細ログは
        # 診断のまとまりに置き、結果を読むためにスクロールさせない。
        self.connection_result = ttk.Label(
            self.serial_lf, wraplength=300, justify="left"
        )
        self.connection_result.grid(row=4, column=0, sticky="ew", pady=(0, 4))
        self.connection_result.grid_remove()
        # 失敗時だけ出す復旧の導線（速度の探索は BCON の適用条件に従う）。
        self.recovery_button = ttk.Button(
            self.serial_lf, text="速度を探す", command=self.huntBconBaud
        )
        self.recovery_button.grid(row=5, column=0, sticky="w", pady=(4, 0))
        self.recovery_button.grid_remove()

        # Switch 側の状態は下端の状態バー（Pico↔Switch）に出す。

        # 詳細は 1 段だけ折りたたむ。開閉しても値・選択・ログは捨てない。
        self.serial_details = CollapsibleSection(
            self.serial_lf,
            "通信の詳細設定",
            on_toggle=self._on_serial_section_toggled,
        )
        self.serial_details.grid(row=7, column=0, sticky="ew", pady=(4, 0))
        detail_body = self.serial_details.body
        detail_body.columnconfigure(0, weight=1)
        self.baud_rate_label = ttk.Label(detail_body, text="通信速度")
        self.baud_rate_label.grid(row=0, column=0, sticky="w")
        self.baud_current = ttk.Label(detail_body, wraplength=280, justify="left")
        self.baud_current.grid(row=1, column=0, sticky="ew")
        self.baud_rate = tk.StringVar()
        # bcon のときだけ使う表示値（115200 / 460800 / 921600 / 1M / 2M）。
        # レガシーの baud_rate と混ぜない（切替で設定が壊れないように）。
        self.bcon_baud = tk.StringVar()
        baud_row = ttk.Frame(detail_body)
        baud_row.grid(row=2, column=0, sticky="w", pady=(4, 0))
        self.baud_rate_cb = ttk.Combobox(baud_row)
        # values の int 群は実行時に文字列化される。注釈だけの問題のため無視する。
        self.baud_rate_cb.config(  # type: ignore[call-overload]
            justify="right",
            state=self.baud_rate_state,
            textvariable=self.baud_rate,
            values=WindowUtils.BAUD_RATE_VALUES,
            width=8,
        )
        self.baud_rate_cb.pack(side="left")
        self.bcon_hunt_button = ttk.Button(
            baud_row, text="速度を探す", command=self.huntBconBaud
        )
        self.bcon_hunt_button.pack(side="left", padx=(4, 0))
        self.baud_rate_cb.bind("<<ComboboxSelected>>", self.applyBaudRate, add="")
        # 通信方式の説明は詳細へ（長文で主要操作を押し下げない）。
        self.transport_description = ttk.Label(
            detail_body, wraplength=280, justify="left"
        )
        self.transport_description.grid(row=3, column=0, sticky="ew", pady=(8, 0))

        self.serial_diagnostics = CollapsibleSection(
            self.serial_lf,
            "接続診断",
            on_toggle=self._on_serial_section_toggled,
        )
        self.serial_diagnostics.grid(row=8, column=0, sticky="ew", pady=(4, 0))
        self.connection_details = ttk.Label(
            self.serial_diagnostics.body, wraplength=280, justify="left"
        )
        self.connection_details.pack(fill="x", pady=(0, 4))
        self.is_show_serial = tk.BooleanVar()
        # 値の変化を送信側へ押し込む。送信スレッドに Tk 変数を読ませると、
        # GUI が忙しい間そこで待たされる（実機で 1.5 秒を実測し、その間は
        # 姿勢が1本も送られなかった）。変化は必ず GUI スレッドで起きるため、
        # ここで読んで渡してよい。
        self.is_show_serial.trace_add("write", lambda *_: self._push_show_serial())
        self.cb_show_serial = ttk.Checkbutton(self.serial_diagnostics.body)
        self.cb_show_serial.config(
            text="送信内容を表示（ログ）",
            variable=self.is_show_serial,
            command=self._on_setting_changed,
        )
        self.cb_show_serial.pack(anchor="w")

        self._player_lamp_after: Any = None

        # Notebook は他タブの要求幅まで広がるため、fill では可視範囲を超える。
        # この枠だけを可視 Canvas の幅へ合わせ、タブには要求高さを返して縦スクロールを保つ。
        viewport = self.tab_serial
        ancestor = self.tab_serial.master
        while ancestor is not None:
            if isinstance(ancestor, tk.Canvas):
                viewport = ancestor
                break
            ancestor = ancestor.master
        self._serial_viewport = viewport
        self.serial_lf.place(x=5, y=0)
        self.tab_serial.bind("<Configure>", self._fit_serial_viewport, add="+")
        self.tab_serial.bind("<Map>", self._fit_serial_viewport, add="+")
        self.serial_lf.bind("<Configure>", self._fit_serial_viewport, add="+")
        if viewport is not self.tab_serial:
            viewport.bind("<Configure>", self._fit_serial_viewport, add="+")
        self._bind_release_focus(self.serial_lf, self.tab_serial)
        self.root.after_idle(self._fit_serial_viewport)
        self._refresh_bcon_rows()
        self._poll_connection_summary()

    def _on_serial_section_toggled(self) -> None:
        """詳細の開閉で高さが変わったら、見えている幅とタブの高さを測り直す。"""
        try:
            self.root.after_idle(self._fit_serial_viewport)
        except tk.TclError:
            pass

    @staticmethod
    def _set_wrap(label: Any, wrap: int) -> None:
        """折り返し幅を変える。同じ値なら触らない（再描画を積まない）。"""
        try:
            current = int(float(label.cget("wraplength") or 0))
        except (tk.TclError, ValueError, TypeError, AttributeError):
            current = -1
        if current == wrap:
            return
        try:
            label.config(wraplength=wrap)
        except (tk.TclError, AttributeError):
            pass

    def _fit_serial_viewport(self, event: Any = None) -> None:
        """他タブで広がった内枠ではなく、実際に見える幅へ接続欄を収める。"""
        if getattr(self, "_closing", False):
            return
        frame = self.serial_lf
        viewport = self._serial_viewport
        if (
            event is not None
            and event.type == tk.EventType.Map
            and isinstance(viewport, tk.Canvas)
        ):
            viewport.xview_moveto(0.0)
            viewport.yview_moveto(0.0)
        requested_width = frame.winfo_reqwidth() + 10
        viewport_width = viewport.winfo_width()
        root_width = 1
        try:
            root_width = int(self.root.winfo_width())
        except Exception:
            root_width = 1
        if viewport is self.tab_serial:
            if root_width <= 1:
                # まだ実寸が無い（組み立て直後）。ここで折り返すと、要求幅と
                # 折り返しが互いを変えて振動する。Map/Configure で測り直す。
                return
            # タブ自身の幅は、折り返しを変えると要求幅が変わって振動する。
            # 親（窓）の実寸を使い、幅→折り返し→幅 の循環を作らない。
            viewport_width = max(viewport_width, root_width)
        if viewport_width <= 1:
            viewport_width = requested_width
        if viewport is self.tab_serial:
            available = viewport_width - 10
        else:
            right = viewport.winfo_rootx() + viewport_width
            available = right - self.tab_serial.winfo_rootx() - 10
        width = max(1, min(viewport_width - 10, available))
        if int(float(frame.place_info().get("width") or 0)) != width:
            frame.place_configure(width=width)
        # 通信方式とポートは、広ければ 1 行に並べて縦を詰める。
        self._reflow_serial_setup(width >= 470)
        # 失敗理由・復旧案内は、見えている幅に収まるなら 1 行で出す。
        # 300 固定だと、広いタブでも 2 行になり、縦を無駄に食う。
        notice_wrap = max(120, width - 12)
        for label in (
            self.com_port_notice,
            self.connection_result,
        ):
            self._set_wrap(label, notice_wrap)
        # 説明・詳細は読みやすい行長に抑える（幅いっぱいには伸ばさない）。
        wrap = max(120, min(420, width - 12))
        for label in (
            self.transport_description,
            self.connection_details,
            self.baud_current,
        ):
            self._set_wrap(label, wrap)
        # 幅は枠の place で決める。タブ自身の幅は変えない
        # （変えると、幅→折り返し→要求幅→幅 の循環になる）。
        height = max(frame.winfo_reqheight() + 10, _TAB_MIN_HEIGHT)
        if getattr(self, "_serial_requested_height", None) != height:
            self._serial_requested_height = height
            self.tab_serial.config(height=height)

    def _reflow_serial_setup(self, wide: bool) -> None:
        """通信方式とポートを、幅に応じて左右／縦に並べ替える。"""
        if getattr(self, "_setup_wide", None) == wide:
            return
        self._setup_wide = wide
        try:
            self._transport_block.grid_forget()
            self._port_block.grid_forget()
            if wide:
                self._setup_row.columnconfigure(0, weight=1, uniform="setup")
                self._setup_row.columnconfigure(1, weight=1, uniform="setup")
                self._transport_block.grid(row=0, column=0, sticky="ew", padx=(0, 8))
                self._port_block.grid(row=0, column=1, sticky="ew")
            else:
                self._setup_row.columnconfigure(0, weight=1, uniform="")
                self._setup_row.columnconfigure(1, weight=0, uniform="")
                self._transport_block.grid(row=0, column=0, sticky="ew")
                self._port_block.grid(row=1, column=0, sticky="ew", pady=(4, 0))
        except tk.TclError:
            pass

    def _bconBaudIndex(self) -> int:
        """設定に保存された bcon の index。範囲外・読めない値は既定へ。"""
        try:
            index = int(self.settings.bcon_baud_index.get())
        except (TypeError, ValueError, tk.TclError):
            return BAUD_DEFAULT_INDEX
        return index if index in BAUD_TABLE else BAUD_DEFAULT_INDEX

    def _legacyBaudValues(self) -> list[str]:
        """レガシー側の候補。設定値が候補に無ければ足して選べるようにする。

        BAUD_RATE_VALUES はよく使う値であって許可値ではない。設定ファイル
        へ書き戻す経路があるため、値の矯正はしない。
        """
        values = [str(value) for value in WindowUtils.BAUD_RATE_VALUES]
        try:
            current = str(self.settings.baud_rate.get())
        except Exception:
            current = ""
        if current and current not in values:
            values.append(current)
        return values

    def _refreshBaudUi(self) -> None:
        """通信方式に合わせて baud 欄の候補・可否・表示値を切り替える。

        bcon 系だけ変更できる。レガシーは固定のまま（9600 前提の
        マイコンを誤って変えないため）。
        """
        uses_bcon = self.serial.uses_bcon(self.transport_name.get())
        if uses_bcon:
            self.baud_rate_cb.config(
                state=baud_combo_state(True),
                values=baud_combo_values(True, []),
                textvariable=self.bcon_baud,
            )
            self.bcon_baud.set(baud_label_for_index(self._bconBaudIndex()))
        else:
            self.baud_rate_cb.config(
                state=baud_combo_state(False),
                values=baud_combo_values(False, WindowUtils.BAUD_RATE_VALUES),
                textvariable=self.baud_rate,
            )
        self._render_baud_summary()

    def _render_baud_summary(self) -> None:
        """詳細欄の「現在／次回の速度」を出す（接続中は実測、未接続は設定）。"""
        label = getattr(self, "baud_current", None)
        if label is None:
            return
        snapshot = self._connection_view
        actual = f"{snapshot.baud}bps" if snapshot.baud is not None else "不明"
        try:
            if self.serial.uses_bcon(self.transport_name.get()):
                next_bps = baud_bps_for_index(self._bconBaudIndex())
                saved = (
                    f"{next_bps}bps（{baud_label_for_index(self._bconBaudIndex())}）"
                )
            else:
                next_bps = self._currentBaudRate()
                saved = f"{next_bps}bps"
        except (ValueError, TypeError):
            saved = "不明"
        text = f"次回の接続: {saved}"
        if snapshot.opened:
            text += f"　接続中: {actual}"
        self._set_label_text(label, text)

    def applyBaudRate(self, event: Any = None) -> None:
        """オフラインは次回値を保存し、接続中は確認済み snapshot を待つ。"""
        if not self._connection_change_allowed():
            self._refreshBaudUi()
            self._refresh_connection_controls()
            return
        if not self.serial.uses_bcon(self.transport_name.get()):
            return
        index = baud_index_for_label(self.bcon_baud.get())
        if index is None or not baud_index_sendable(index):
            self._refreshBaudUi()
            self._refresh_connection_controls()
            return
        snapshot = self.serial.connection_snapshot()
        if (
            snapshot.phase in {"opening", "hello", "closing"}
            or snapshot.operation is not None
        ):
            self._refreshBaudUi()
            self._refresh_connection_controls()
            return
        if snapshot.opened:
            if not self.serial.change_bcon_baud(index):
                self._refreshBaudUi()
            self._refresh_connection_controls()
            return
        self.settings.bcon_baud_index.set(index)
        self._on_setting_changed()
        print(f"次回の BCON 接続は {baud_bps_for_index(index)}bps を使用します。")

    def huntBconBaud(self) -> None:
        """速度の検出も接続 worker に依頼する。"""
        if not self._connection_change_allowed():
            return
        self.serial.hunt_bcon_baud()
        self._refresh_connection_controls()

    def _currentBaudRate(self) -> int:
        """Baud Rate を通常の int で返す。数値として読めないときだけ既定。

        BAUD_RATE_VALUES は Combobox に出す「よく使う値」であって、
        使ってよい値の一覧ではない。GameCube の自動化では別の速度を
        使うし、独自のマイコンを載せている人はもっと速い値を入れる。
        候補に無いことを理由に書き換えてはいけない。設定ファイルへ
        書き戻す経路があるので、矯正すると利用者の設定が壊れて
        元の値も分からなくなる。

        ここで守るのは「数値として読めること」だけ。読めない値を
        そのまま流すと接続の途中で例外になり、開かない理由も出ない。

        bcon 系はレガシーの baud_rate ではなく、BAUD_TABLE の index を
        使う（1M などはコンボ表示の文字列で、int としては読めない）。
        """
        if self.serial.uses_bcon(self.transport_name.get()):
            return baud_bps_for_index(self._bconBaudIndex())
        try:
            return int(self.baud_rate.get())
        except (TypeError, ValueError, tk.TclError):
            fallback = WindowUtils.BAUD_RATE_VALUES[0]
            logger.warning(f"Baud Rate を数値として読めないため {fallback} を使います")
            return fallback

    def refreshComPorts(self, keep_current: bool = True) -> None:
        """一覧だけを更新する。消えたポートを別の機器へ置き換えない。"""
        before = str(self.com_port_name.get()) if keep_current else ""
        if keep_current and not before:
            before = str(self.settings.com_port_name.get())
        ports = WindowUtils.listComPorts()
        self._com_port_map = {label: device for device, label in ports}
        self.com_port_cb["values"] = [label for _, label in ports]
        fit_combo_width(self.com_port_cb, min_chars=12, max_chars=22)
        selected = next(
            (label for label, device in self._com_port_map.items() if device == before),
            "",
        )
        self.com_port_text.set(selected)
        self._apply_selected_port()
        # 設定はここで保存しない。抜けた機器の保存済み名前を残す。
        self._refresh_connection_controls()

    def _apply_selected_port(self) -> None:
        device = self._com_port_map.get(self.com_port_text.get(), "")
        self.com_port_name.set(device)
        self.com_port.set(WindowUtils.portToNumber(device) if device else 0)
        self._update_title()

    def onComPortSelected(self, event: Any = None) -> None:
        if not self._connection_change_allowed():
            current = str(self.com_port_name.get())
            self.com_port_text.set(
                next(
                    (
                        label
                        for label, device in self._com_port_map.items()
                        if device == current
                    ),
                    "",
                )
            )
            return
        snapshot = self.serial.connection_snapshot()
        if (
            snapshot.phase in {"opening", "hello", "closing"}
            or snapshot.operation is not None
        ):
            return
        self._apply_selected_port()
        self._refresh_connection_controls()
        self._on_setting_changed()

    def reloadSerialPort(self) -> None:
        """選択している機器へ接続する。機器一覧の更新は『更新』で行う。"""
        self.activateSerial()

    def _push_show_serial(self) -> None:
        """表示の切替を送信側へ渡す（GUI スレッドから呼ばれる）。

        送信スレッドは Tk 変数を読まないため、値の変化はここで知らせる。
        送信側がまだ無いとき（起動直後）は SerialService 側が何もしない。
        """
        try:
            value = bool(self.is_show_serial.get())
        except Exception:
            return
        self.serial.set_show_serial(value)

    def _sender_spec(self) -> SenderSpec:
        """tk 変数の現在値を通常の値へ落として Sender 生成用にまとめる。

        Sender（とその先の worker）は tk 変数を読まない。ここで読むのは
        GUI スレッドのいまこの瞬間だけ。以後は Tk と無関係な値になる。
        """
        # 記録する操作の絞り込み。空なら書式ごとの既定に任せる
        actions = self.settings.input_log_actions.get().strip()
        return SenderSpec(
            transport_name=self._selectedTransportName(),
            is_show_serial=bool(self.is_show_serial.get()),
            arbitration_mode=self.arbitration_mode.get(),
            arbitration_cooldown=self._arbitrationCooldown(),
            input_log_format=self.settings.input_log_format.get(),
            input_log_actions=actions,
            input_log_enabled=self.settings.input_log_enabled.get(),
            input_log_stick_change=self.settings.input_log_stick_change.get(),
            # ライブ入力の最低保持ミリ秒（ini＋既定のみ）
            live_min_dwell_ms=self.settings.live_min_dwell_ms.get(),
        )

    def _apply_input_log_settings(self) -> None:
        """入力ログの設定を Sender へ反映する。

        書式の解釈はサービス側。メニューの「入力ログの書式」から
        呼ばれると、開いている最中でもすぐ反映される。
        """
        self.serial.apply_input_log(
            self.settings.input_log_format.get(),
            self.settings.input_log_actions.get(),
            self.settings.input_log_enabled.get(),
            self.settings.input_log_stick_change.get(),
        )

    # ------------------------------------------------------------------
    # 通信方式のプリセット
    # ------------------------------------------------------------------
    # 運び方を差し替えられる形は作ったが、選ぶ手段が無かった。
    #   ここで設定・起動引数・画面の3つから名前で選べるようにする。
    #   本体は実装を知らない。名前を登録簿へ渡すだけ。

    def _selectedTransportName(self) -> str:
        """これから使う通信方式の名前を決める（起動引数 > 設定ファイル）。"""
        return self.serial.selected_transport_name(self.settings.transport_name.get())

    def _loadTransportPlugins(self) -> None:
        """利用者が置いた自作の Transport を読み込む。"""
        self.serial.load_plugins(self.settings.transport_plugin_dir.get())

    def _transport_display(self, name: str) -> str:
        """内部キー→画面の表示名。未知の方式はキーのまま（落とさない）。"""
        return transport_display_name(name)

    def _transport_key(self, label: str) -> str:
        """画面の表示名→内部キー。表に無ければ入力をキーとして扱う。"""
        text = str(label).strip()
        for name in self.serial.list_transports():
            if self._transport_display(name) == text:
                return name
        return text

    def _refreshTransportChoices(self) -> None:
        """選択欄の候補を登録簿から組み直し、現在値を選ぶ。"""
        names = self.serial.list_transports()
        self.transport_cb.config(
            values=[self._transport_display(name) for name in names]
        )
        fit_combo_width(self.transport_cb, min_chars=12, max_chars=32)
        current = self._selectedTransportName()
        self.transport_name.set(current)
        self.transport_cb.set(self._transport_display(current))
        self._refresh_transport_description()
        self._refresh_bcon_rows()
        # 選択値が決まった今が Bcon タブを出す最初の機会
        self._refresh_bcon_tab()

    def applyTransport(self, event: Any = None) -> None:
        try:
            raw = str(self.transport_cb.get())
        except (AttributeError, TypeError):
            raw = ""
        requested = self._transport_key(raw) if raw else str(self.transport_name.get())
        actual = (
            self.serial.connection_snapshot().transport_name
            or self._selectedTransportName()
        )
        actual = canonical_transport_name(actual) or actual
        self.transport_name.set(actual)
        try:
            self.transport_cb.set(self._transport_display(actual))
        except (AttributeError, tk.TclError):
            pass
        if not self._connection_change_allowed():
            return
        if requested not in self.serial.list_transports():
            return
        snapshot = self.serial.connection_snapshot()
        self._pending_transport_switch = (
            snapshot.generation + 1,
            requested,
            snapshot.opened,
            str(self.com_port_name.get()),
        )
        if not self.serial.request_switch_transport(requested, transport_logger=logger):
            self._pending_transport_switch = None
        self._refresh_connection_controls()

    def _refresh_transport_description(self) -> None:
        name = str(self.transport_name.get())
        description = dict(describe_transports()).get(name, "") or name
        self.transport_description.config(text=description)

    # 入力の優先付け（誰の操作を優先するか）
    #   既定は off で本家と同じ挙動。画面から選び直せる。

    def _refreshArbitrationChoices(self) -> None:
        """選択欄の候補を許可値から組み直し、現在値を選ぶ。"""
        modes = list(self.serial.arbitration_modes())
        labels = [_ARBITRATION_LABELS.get(mode, mode) for mode in modes]
        self.arbitration_cb.config(values=labels)
        fit_combo_width(self.arbitration_cb, min_chars=12, max_chars=32)
        mode = self.serial.resolve_arbitration(
            self.settings.arbitration_mode.get(), logger=logger
        )
        self.arbitration_mode.set(mode)
        try:
            self.arbitration_cb.set(_ARBITRATION_LABELS.get(mode, mode))
        except (AttributeError, tk.TclError):
            pass
        self._render_arbitration_hint(mode)

    def _render_arbitration_hint(self, mode: str) -> None:
        """候補が意味する動作を、選択肢の下に文章で書く（色・吹き出しだけにしない）。"""
        hint = getattr(self, "arbitration_hint", None)
        if hint is None:
            return
        text = _ARBITRATION_HINTS.get(mode, f"{mode}：送信側の実装に従います。")
        self._set_label_text(hint, text)

    def _arbitrationCooldown(self) -> float:
        """設定の秒数を数へ直す。読めない値は既定の 2 秒として扱う。"""
        try:
            return max(0.0, float(self.settings.arbitration_cooldown.get()))
        except (TypeError, ValueError):
            logger.warning("入力調停の cooldown を読めません。2.0 秒とします")
            return 2.0

    def applyArbitration(self, event: Any = None) -> None:
        """選ばれた入力調停を Sender へ反映する。

        線を開いていなくても選べる。次に開いたときへ効かせるため、
        画面の値は設定へ残す。Sender があればその場で適用する。
        """
        raw = ""
        try:
            raw = str(self.arbitration_cb.get())
        except (AttributeError, tk.TclError):
            pass
        requested = _ARBITRATION_KEYS.get(raw, raw or self.arbitration_mode.get())
        mode = self.serial.resolve_arbitration(requested, logger=logger)
        # 解決後の名前を画面へ戻す。知らない名前を選んだまま残さない
        self.arbitration_mode.set(mode)
        try:
            self.arbitration_cb.set(_ARBITRATION_LABELS.get(mode, mode))
        except (AttributeError, tk.TclError):
            pass
        self.settings.arbitration_mode.set(mode)
        self._render_arbitration_hint(mode)
        self.serial.set_arbitration(mode=mode, cooldown=self._arbitrationCooldown())
        message = f"入力調停を {mode} に切り替えました。"
        if mode == "script":
            message += "　実行中の手操作は断ります（一時停止すれば操作できます）。"
        elif mode == "human":
            message += "　手で触った直後はスクリプトの操作を断ります。"
        else:
            message += "　どちらも断りません（本家と同じ挙動）。"
        print(message)
        logger.info(message)
        self._on_setting_changed()

    def _refresh_bcon_rows(self) -> None:
        """BCON のときだけ速度探索を出す（状態は下端の状態バー）。"""
        show = self.serial.uses_bcon(self.transport_name.get())
        try:
            if show:
                self.bcon_hunt_button.pack(side="left", padx=(5, 0))
            else:
                self.bcon_hunt_button.pack_forget()
        except (tk.TclError, AttributeError):
            pass

    def _subscribe_connection_state(self) -> None:
        if getattr(self, "_closing", False):
            return
        if getattr(self, "_connection_unsubscribe", None) is None:
            self._connection_unsubscribe = self.serial.subscribe_connection(
                self._queue_connection_snapshot
            )
        if getattr(self, "_connection_log_unsubscribe", None) is None:
            self._connection_log_unsubscribe = self.serial.subscribe_connection_log(
                self._queue_connection_log
            )

    def _queue_connection_log(self, generation: int, message: str) -> None:
        post_to_gui(self, self._apply_connection_log, generation, message)

    def _apply_connection_log(self, generation: int, message: str) -> None:
        if (
            not getattr(self, "_closing", False)
            and self.serial.connection_snapshot().generation == generation
        ):
            print(message)

    def _queue_connection_snapshot(self, _snapshot: ConnectionSnapshot) -> None:
        # 通知順ではなく GUI 側で現在の写しを読む。
        post_latest_to_gui(
            self,
            "serial_connection",
            lambda: self._apply_connection_snapshot(self.serial.connection_snapshot()),
        )

    def _dispose_connection_subscription(self) -> None:
        unsubscribe, self._connection_unsubscribe = (
            getattr(self, "_connection_unsubscribe", None),
            None,
        )
        if callable(unsubscribe):
            unsubscribe()
        unsubscribe, self._connection_log_unsubscribe = (
            getattr(self, "_connection_log_unsubscribe", None),
            None,
        )
        if callable(unsubscribe):
            unsubscribe()

    def _connection_change_allowed(self) -> bool:
        runner = getattr(self, "runner", None)
        if runner is not None and runner.is_busy():
            print("コマンドを停止してから変更してください。")
            return False
        return not bool(getattr(self, "_closing", False))

    def _refresh_connection_controls(self) -> None:
        snapshot = self.serial.connection_snapshot()
        runner = getattr(self, "runner", None)
        busy = runner is not None and runner.is_busy()
        pending = (
            snapshot.phase in {"opening", "hello", "closing"}
            or snapshot.operation is not None
        )
        blocked = busy or pending or bool(getattr(self, "_closing", False))
        has_ports = bool(self._com_port_map)
        selected = bool(self._com_port_map.get(self.com_port_text.get()))
        self.com_port_cb.config(
            state="readonly" if has_ports and not blocked else "disabled"
        )
        self.transport_cb.config(state="disabled" if blocked else "readonly")
        self.reloadComPort.config(
            text="再接続" if snapshot.phase == "ready" else "接続",
            state="normal" if selected and not blocked else "disabled",
        )
        self.disconnectComPort.config(
            state="normal"
            if snapshot.opened or snapshot.phase in {"opening", "hello"}
            else "disabled"
        )
        hint = getattr(self, "connect_hint", None)
        if hint is not None:
            try:
                if has_ports and not selected and not blocked:
                    hint.config(text="ポートを選ぶと接続できます")
                else:
                    hint.config(text="")
            except (tk.TclError, AttributeError):
                pass
        self.refreshComPort.config(state="normal")
        keyboard = getattr(self, "cb_use_keyboard", None)
        if keyboard is not None:
            keyboard.config(state="disabled" if pending else "normal")
        gamepad = getattr(self, "cb_use_gamepad", None)
        if gamepad is not None and getattr(self, "_hidhide_gamepad_ok", False):
            gamepad.config(state="disabled" if pending else "normal")
        uses_bcon = self.serial.uses_bcon(self.transport_name.get())
        self.baud_rate_cb.config(
            state="readonly" if uses_bcon and not blocked else "disabled"
        )
        self.bcon_hunt_button.config(
            state="normal"
            if uses_bcon and snapshot.opened and not blocked
            else "disabled"
        )
        # 失敗したときだけ、基本表示に復旧の導線（速度を探す）を出す。
        failed = snapshot.phase == "failed" and not blocked
        try:
            if failed and uses_bcon:
                self.recovery_button.grid()
                self.recovery_button.config(state="normal")
            else:
                self.recovery_button.grid_remove()
        except (tk.TclError, AttributeError):
            pass
        self._render_connection_notice(snapshot, has_ports)
        self._render_baud_summary()
        self._update_controller_status()
        refresh = getattr(self, "_refresh_bcon_controls", None)
        if callable(refresh):
            refresh()

    def _render_connection_notice(
        self, snapshot: ConnectionSnapshot, has_ports: bool
    ) -> None:
        """基本表示に出す注意だけをまとめる（成功時は空）。"""
        messages: list[str] = []
        empty = getattr(self, "port_empty", None)
        if empty is not None:
            try:
                if not has_ports:
                    empty.config(text="ポートなし")
                    empty.grid()
                else:
                    empty.grid_remove()
            except (tk.TclError, AttributeError):
                pass
        try:
            self.refreshComPort.config(
                style="Accent.TButton" if not has_ports else "TButton"
            )
        except (tk.TclError, AttributeError):
            pass
        chosen = str(self.com_port_name.get())
        actual = snapshot.port_name
        if snapshot.opened and chosen and actual and chosen != actual:
            messages.append(f"選択 {chosen} と実際 {actual} が異なります。")
        text = "\n".join(messages)
        self._set_label_text(self.com_port_notice, text)
        try:
            if text:
                self.com_port_notice.grid()
            else:
                self.com_port_notice.grid_remove()
        except (tk.TclError, AttributeError):
            pass

    def _apply_connection_snapshot(self, snapshot: ConnectionSnapshot) -> None:
        """確認済み状態だけを GUI へ映し、同世代の STATUS では listener を作り直さない。"""
        if getattr(self, "_closing", False):
            return
        if snapshot.generation != self.serial.connection_snapshot().generation:
            return
        previous = self._connection_view
        if snapshot.generation < previous.generation:
            return
        self._connection_view = snapshot
        name = (
            canonical_transport_name(snapshot.transport_name) or snapshot.transport_name
        )
        pending_switch = getattr(self, "_pending_transport_switch", None)
        completed_switch = bool(
            pending_switch is not None
            and snapshot.generation == pending_switch[0]
            and snapshot.phase == "disconnected"
            and snapshot.operation is None
            and name == pending_switch[1]
        )
        if pending_switch is not None and (
            snapshot.generation > pending_switch[0]
            or (
                snapshot.generation == pending_switch[0]
                and snapshot.phase == "failed"
                and snapshot.operation is None
            )
        ):
            self._pending_transport_switch = None
        changed_transport = bool(name) and name != self.transport_name.get()
        if (changed_transport or completed_switch) and snapshot.phase != "opening":
            self.transport_name.set(name)
            try:
                self.transport_cb.set(self._transport_display(name))
            except (AttributeError, tk.TclError):
                pass
            if completed_switch:
                self.serial.clear_override()
            self._refreshBaudUi()
            self._refresh_transport_description()
            self._refresh_bcon_tab()
            self._refresh_bcon_rows()
            self._on_setting_changed()
        if snapshot.phase == "ready":
            if (
                self.serial.uses_bcon(snapshot.transport_name)
                and snapshot.baud in BAUD_TABLE.values()
            ):
                index = next(
                    index for index, rate in BAUD_TABLE.items() if rate == snapshot.baud
                )
                if self._bconBaudIndex() != index:
                    self.settings.bcon_baud_index.set(index)
                    self._on_setting_changed()
                self.bcon_baud.set(baud_label_for_index(index))
            if previous.generation != snapshot.generation or previous.phase != "ready":
                if self.is_use_keyboard.get():
                    self._bindKeyboardFocus()
                if self.is_use_gamepad.get():
                    self.activateGamepad()
        elif snapshot.phase in {
            "opening",
            "hello",
            "closing",
            "failed",
            "disconnected",
            "restarting",
            "bootloader",
        }:
            self._unbindKeyboardFocus()
            self._gamepad_active.clear()
            self._clearGamepadMirror()
        if snapshot.phase == "failed" and snapshot.opened and snapshot.baud is None:
            self.bcon_baud.set(baud_label_for_index(self._bconBaudIndex()))
        self._render_connection_summary()
        self._refresh_connection_controls()
        self._update_title()
        if completed_switch and pending_switch is not None:
            self._pending_transport_switch = None
            _, _, was_open, previous_port = pending_switch
            if was_open and previous_port and previous_port == self.com_port_name.get():
                self.activateSerial()

    def _render_connection_summary(self) -> None:
        snapshot = self._connection_view
        transport = snapshot.transport_name or self.transport_name.get()
        # 接続状態は下端の状態バーが全タブ共通で出す。ここは操作結果だけ。

        # 操作結果・失敗理由は押したボタンの直下へ。詳細ログは診断に置く。
        result = ""
        if snapshot.phase == "failed":
            result = snapshot.message or "接続に失敗しました。"
            if snapshot.opened and snapshot.baud is None:
                result += "\n速度を確認できません。『速度を探す』で確認してください。"
        elif snapshot.operation:
            result = f"{snapshot.operation}…"
        elif snapshot.phase in {"restarting", "bootloader"} and snapshot.message:
            result = snapshot.message
        self._set_label_text(getattr(self, "connection_result", None), result)
        result_label = getattr(self, "connection_result", None)
        if result_label is not None:
            try:
                if result:
                    result_label.grid()
                else:
                    result_label.grid_remove()
            except (tk.TclError, AttributeError):
                pass

        chosen = str(self.com_port_name.get()) or "未選択"
        actual = snapshot.port_name or "不明"
        baud = f"{snapshot.baud}bps" if snapshot.baud is not None else "不明"
        details = f"選択: {chosen}　実際: {actual}\n通信速度: {baud}"
        if self.serial.uses_bcon(transport):
            ack = snapshot.hello_ack
            hello = (
                "未確認"
                if ack is None
                else f"採用={ack[0]} / v{ack[1]}.{ack[2]} / 結果={ack[3]}"
            )
            details += f"　HELLO: {hello}"
        self._set_label_text(self.connection_details, details)
        self._render_baud_summary()

    def _poll_connection_summary(self) -> None:
        # 経過時間の表示だけを更新する。受信要求や transport の照会は行わない。
        if getattr(self, "_closing", False):
            return
        self._render_connection_summary()
        self._refresh_connection_controls()
        self._player_lamp_after = self.root.after(500, self._poll_connection_summary)

    def _cancel_player_lamp_patrol(self) -> None:
        after_id, self._player_lamp_after = self._player_lamp_after, None
        if after_id is not None:
            self.root.after_cancel(after_id)

    def _start_serial(self) -> None:
        if getattr(self, "_serial_started", False):
            return
        self._serial_started = True
        self.serial.build_sender(self._sender_spec(), transport_logger=logger)
        self._refresh_bcon_rows()
        # 保存した機器が列挙結果に残っている場合に限り、一度だけ自動接続する。
        if (
            self.com_port_name.get()
            and self.com_port_name.get() in self._com_port_map.values()
        ):
            self.activateSerial()

    def activateSerial(self) -> None:
        if not self._connection_change_allowed():
            return
        snapshot = self.serial.connection_snapshot()
        if (
            snapshot.phase in {"opening", "hello", "closing"}
            or snapshot.operation is not None
        ):
            return
        port_name = str(self.com_port_name.get())
        if not port_name or port_name not in self._com_port_map.values():
            self._refresh_connection_controls()
            return
        uses_bcon = self.serial.uses_bcon(self.transport_name.get())
        if not uses_bcon and self.baud_rate.get() == "4800":
            if (
                tkmsg.askquestion(
                    "確認",
                    "Baud Rateを4800にすると動かなくなる可能性があります。\n変更しますか？",
                )
                != "yes"
            ):
                return
        self.serial.request_connect(
            port_num=int(self.com_port.get()),
            port_name=port_name,
            baud=self._currentBaudRate(),
            keyboard_enabled=bool(self.is_use_keyboard.get()),
            setting_path=str(self.settings.setting_path),
        )
        self._refresh_connection_controls()

    def inactivateSerial(self) -> None:
        runner = getattr(self, "runner", None)
        if runner is not None and runner.is_busy():
            runner.request_stop()
        self.serial.disconnect()
        self._unbindKeyboardFocus()
        self.is_use_keyboard.set(False)
        self.is_use_gamepad.set(False)
        self._gamepad_active.clear()
        self._clearGamepadMirror()
        self._on_setting_changed()
        self._refresh_connection_controls()
        self._update_title()
