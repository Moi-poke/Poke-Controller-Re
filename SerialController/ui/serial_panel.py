#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""serial_panel.py - シリアル枠・操作枠の組み立てと接続手順の画面側。

PokeControllerApp に混ぜて使う（多重継承）。開く手順そのものは
services.SerialService が持ち、ここでは確認ダイアログ・tk 変数の
読み書き・見た目の反映だけを行う。
"""

from __future__ import annotations

import threading
import time
import tkinter as tk
import tkinter.messagebox as tkmsg
import tkinter.ttk as ttk
from typing import Any

import WindowUtils
from core.connection_state import ConnectionSnapshot
from core.transport.bcon_baud import (
    BAUD_DEFAULT_INDEX,
    baud_bps_for_index,
    baud_combo_state,
    baud_combo_values,
    baud_index_for_label,
    baud_index_sendable,
    baud_label_for_index,
)
from core.transport.bcon_protocol import BAUD_TABLE
from core.transport.registry import canonical_transport_name, describe_transports
from core.ui_inbox import post_latest_to_gui, post_to_gui
from loguru import logger
from services.serial_service import SenderSpec, SerialService
from ui.combo_fit import fit_combo_width
from ui.controller_dock import ControllerDock
from ui.tooltip import Tooltip

# STATUS flags の bit5=有線。SSOTは Switch-bcon の spec/protocol_v3.md。
_STATUS_FLAG_WIRED = 0x20


#: 文字を打つ部品の Tk クラス名。ここにフォーカスがある間は、打鍵を
#: コントローラーへ送らない（検索語がボタン操作として Switch へ届く）。
_TEXT_INPUT_CLASSES = frozenset(
    {"Entry", "TEntry", "Spinbox", "TSpinbox", "TCombobox", "Text"}
)


def _is_text_input(widget: Any) -> bool:
    """文字を打てる状態の入力欄か。読み取り専用・無効なら False。"""
    try:
        if widget is None or widget.winfo_class() not in _TEXT_INPUT_CLASSES:
            return False
        state = str(widget.cget("state"))
    except (tk.TclError, AttributeError):
        return False
    return state not in ("disabled", "readonly")


class SerialPanelMixin:
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
    bcon_wired_label: Any
    bcon_wired: Any
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
    connection_status: Any
    connection_details: Any
    transport_description: Any
    refreshComPort: Any
    bcon_hunt_button: Any

    def _build_serial_frame(self) -> None:
        self._connection_view = ConnectionSnapshot()
        self._connection_unsubscribe: Any = None
        self._connection_log_unsubscribe: Any = None
        self._pending_transport_switch: tuple[int, str, bool, str] | None = None
        self.serial_lf = ttk.Labelframe(self.tab_serial)

        # 項目名｜値の縦並び（左の列に項目名、右の列に値）。横に並べ切ると
        # 870px 前後を要し、ログを右に置く配置では常に横スクロールが出ていた。
        self.com_port_label = ttk.Label(self.serial_lf)
        self.com_port_label.config(text="COMポート: ")
        self.com_port_label.grid(column=0, padx="5", pady="2", row=0, sticky="w")

        self.com_port = tk.IntVar()
        self.com_port_name = tk.StringVar()
        # Combobox に出す表示名（'COM3: USB シリアル デバイス'）を持つ。
        # 実際に開くデバイス名は com_port_name、末尾の番号は com_port。
        # com_port_label は上の Label ウィジェットが使っているため別名にする。
        self.com_port_text = tk.StringVar()
        self._com_port_map: dict[str, str] = {}
        port_row = ttk.Frame(self.serial_lf)
        port_row.grid(column=1, padx=5, pady=2, row=0, sticky="ew")
        self.com_port_cb = ttk.Combobox(port_row)
        self.com_port_cb.config(
            state="readonly", textvariable=self.com_port_text, width=16
        )
        port_row.columnconfigure(0, weight=1)
        self.com_port_cb.grid(row=0, column=0, sticky="ew")
        self.refreshComPort = ttk.Button(
            port_row, text="更新", width=4, command=self.refreshComPorts
        )
        self.refreshComPort.grid(row=0, column=1, padx=(5, 0), sticky="e")
        self.com_port_cb.bind("<<ComboboxSelected>>", self.onComPortSelected, add="")

        # 行を分けて 1 行の幅を抑える（同じ行に並べると最も幅を取る行になる）。
        self.baud_rate_label = ttk.Label(self.serial_lf)
        self.baud_rate_label.config(text="ボーレート: ")
        self.baud_rate_label.grid(column=0, padx="5", pady="2", row=5, sticky="w")

        self.baud_rate = tk.StringVar()
        # bcon のときだけ使う表示値（115200 / 460800 / 921600 / 1M / 2M）。
        # レガシーの baud_rate と混ぜない（切替で設定が壊れないように）。
        self.bcon_baud = tk.StringVar()
        baud_row = ttk.Frame(self.serial_lf)
        baud_row.grid(column=1, padx=5, pady=2, row=5, sticky="w")
        self.baud_rate_cb = ttk.Combobox(baud_row)
        # values の int 群は実行時に文字列化される。注釈だけの問題のため無視する。
        self.baud_rate_cb.config(  # type: ignore[call-overload]
            justify="right",
            state=self.baud_rate_state,
            textvariable=self.baud_rate,
            values=WindowUtils.BAUD_RATE_VALUES,
            width=6,
        )
        self.baud_rate_cb.pack(side="left")
        self.bcon_hunt_button = ttk.Button(
            baud_row, text="速度を探す", command=self.huntBconBaud
        )
        self.bcon_hunt_button.pack(side="left", padx=(5, 0))
        self.baud_rate_cb.bind("<<ComboboxSelected>>", self.applyBaudRate, add="")

        # 接続操作と確認結果は、スクロールしなくても見える先頭へ置く。
        actions = ttk.Frame(self.serial_lf)
        actions.grid(column=0, columnspan=2, padx=5, pady=(4, 2), row=1, sticky="w")
        self.reloadComPort = ttk.Button(actions)
        self.reloadComPort.config(text="接続", command=self.reloadSerialPort)
        self.reloadComPort.pack(side="left")

        self.disconnectComPort = ttk.Button(actions)
        self.disconnectComPort.config(text="切断", command=self.inactivateSerial)
        self.disconnectComPort.pack(side="left", padx=(5, 0))

        self.is_show_serial = tk.BooleanVar()
        # 値の変化を送信側へ押し込む。送信スレッドに Tk 変数を読ませると、
        # GUI が忙しい間そこで待たされる（実機で 1.5 秒を実測し、その間は
        # 姿勢が1本も送られなかった）。変化は必ず GUI スレッドで起きるため、
        # ここで読んで渡してよい。
        self.is_show_serial.trace_add("write", lambda *_: self._push_show_serial())
        # 操作ボタンと同じ行に並べると最も幅を取る行になるので、次の行へ。
        self.cb_show_serial = ttk.Checkbutton(self.serial_lf)
        self.cb_show_serial.config(
            text="送信内容を表示",
            variable=self.is_show_serial,
            command=self._on_setting_changed,
        )
        self.cb_show_serial.grid(column=1, padx="5", pady="2", row=11, sticky="w")

        # 通信方式の選択。
        #   候補は通信方式の一覧から引く。ここに名前を
        #     書き並べない（実装を足したのに画面に出ない、を防ぐ）。
        self.transport_label = ttk.Label(self.serial_lf)
        self.transport_label.config(text="通信方式: ")
        self.transport_label.grid(column=0, padx="5", pady="2", row=6, sticky="w")

        self.transport_cb = ttk.Combobox(self.serial_lf)
        self.transport_cb.config(
            state="readonly", textvariable=self.transport_name, width=16
        )
        self.transport_cb.grid(column=1, padx="5", pady="2", row=6, sticky="ew")
        self.transport_cb.bind("<<ComboboxSelected>>", self.applyTransport, add="")
        self.transport_description = ttk.Label(
            self.serial_lf, wraplength=230, justify="left"
        )
        self.transport_description.grid(
            column=1, padx=5, pady=(0, 3), row=7, sticky="ew"
        )

        # 入力の優先付けの選択。候補は送信側の許可値から
        #   引く（画面に名前を書き並べない）。既定の off は本家と同じ
        #   挙動で、script を選ぶと実行中の手操作を断る。
        self.arbitration_label = ttk.Label(self.serial_lf)
        self.arbitration_label.config(text="入力調停: ")
        self.arbitration_label.grid(column=0, padx="5", pady="2", row=10, sticky="w")

        self.arbitration_cb = ttk.Combobox(self.serial_lf)
        self.arbitration_cb.config(
            state="readonly", textvariable=self.arbitration_mode, width=16
        )
        self.arbitration_cb.grid(column=1, padx="5", pady="2", row=10, sticky="ew")
        self.arbitration_cb.bind("<<ComboboxSelected>>", self.applyArbitration, add="")
        Tooltip(
            self.arbitration_cb,
            "コマンド実行中に手動操作を受け付けるかどうか",
        )

        # プレイヤーランプ（bcon の PLAYER_INFO を省スペース表示）と振動。
        # Bcon設定小窓の4灯と同じく bit0=LED1..bit3=LED4 の横並び。
        # bcon のときだけ出す「状態」行に束ねる（通信方式の行の右へ足すと
        # 横幅が伸び、狭いタブ欄で横スクロールになる）。
        self.bcon_status_label = ttk.Label(self.serial_lf, text="状態: ")
        self.bcon_status_label.grid(column=0, padx="5", pady="2", row=9, sticky="w")
        self.bcon_status = ttk.Frame(self.serial_lf)
        self.bcon_status.grid(column=1, padx="5", pady="2", row=9, sticky="w")
        self.player_lamp_label = ttk.Label(self.bcon_status)
        self.player_lamp_label.config(text="LED: ")
        self.player_lamp_label.pack(side="left")

        self.player_lamp_canvas = tk.Canvas(
            self.bcon_status, width=86, height=18, highlightthickness=0
        )
        self.player_lamp_canvas.pack(side="left")
        self._player_lamp_items = []
        for index in range(4):
            left = 2 + index * 21
            self._player_lamp_items.append(
                self.player_lamp_canvas.create_rectangle(
                    left,
                    2,
                    left + 14,
                    16,
                    fill="gray",
                    outline="#4D4D4D",
                )
            )
        self._player_lamp_after = None
        self.rumble_label = ttk.Label(self.bcon_status)
        self.rumble_label.config(text="振動: ")
        self.rumble_label.pack(side="left", padx=(12, 0))

        # 実際に届いた STATUS だけを表示する。設定の変更は BCON タブへ集める。
        self.bcon_wired_label = ttk.Label(self.serial_lf, text="Switch 接続: ")
        self.bcon_wired_label.grid(column=0, padx=5, pady=2, row=8, sticky="w")
        self.bcon_row = ttk.Frame(self.serial_lf)
        self.bcon_row.grid(column=1, padx=5, pady=2, row=8, sticky="ew")
        self.bcon_wired = tk.StringVar(value="不明")
        ttk.Label(self.bcon_row, textvariable=self.bcon_wired, wraplength=95).pack(
            side="left"
        )
        ttk.Button(
            self.bcon_row, text="BCON 設定を開く", command=self.select_bcon_tab
        ).pack(side="left", padx=(12, 0))
        self.connection_status = ttk.Label(
            self.serial_lf, text="未接続", wraplength=300, justify="left"
        )
        self.connection_status.grid(
            column=0, columnspan=2, padx=5, pady=(2, 2), row=2, sticky="ew"
        )
        self.connection_details = ttk.Label(
            self.serial_lf, wraplength=300, justify="left"
        )
        self.connection_details.grid(
            column=0, columnspan=2, padx=5, pady=(0, 4), row=3, sticky="ew"
        )
        self.com_port_notice = ttk.Label(self.serial_lf, wraplength=300, justify="left")
        self.com_port_notice.grid(
            column=0, columnspan=2, padx=5, pady=(0, 4), row=4, sticky="ew"
        )
        # 値の列を伸ばし、広い窓では長いポート名・機器名を省略せずに出す。
        self.serial_lf.columnconfigure(1, weight=1)

        self.serial_lf.config(text="シリアル設定")
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
        self.serial_lf.place(x=5, y=5)
        self.tab_serial.bind("<Configure>", self._fit_serial_viewport, add="+")
        self.tab_serial.bind("<Map>", self._fit_serial_viewport, add="+")
        self.serial_lf.bind("<Configure>", self._fit_serial_viewport, add="+")
        if viewport is not self.tab_serial:
            viewport.bind("<Configure>", self._fit_serial_viewport, add="+")
        self.root.after_idle(self._fit_serial_viewport)
        self._refresh_bcon_rows()
        self._poll_connection_summary()

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
        label_width = max(
            widget.winfo_reqwidth()
            for widget in (
                self.com_port_label,
                self.baud_rate_label,
                self.transport_label,
                self.arbitration_label,
                self.bcon_wired_label,
            )
        )
        for label in (
            self.connection_status,
            self.connection_details,
            self.com_port_notice,
        ):
            wrap = max(1, min(300, width - 10))
            if int(float(label.cget("wraplength"))) != wrap:
                label.config(wraplength=wrap)
        description_wrap = max(1, min(230, width - label_width - 10))
        if (
            int(float(self.transport_description.cget("wraplength")))
            != description_wrap
        ):
            self.transport_description.config(wraplength=description_wrap)
        size = (min(requested_width, viewport_width), frame.winfo_reqheight() + 10)
        if getattr(self, "_serial_requested_size", None) != size:
            self._serial_requested_size = size
            self.tab_serial.config(width=size[0], height=size[1])

    def _build_control_frame(self) -> None:
        self.control_lf = ttk.Labelframe(
            self.tab_controller, text="コントローラ", padding=(8, 2)
        )
        self.control_lf.columnconfigure(0, weight=1)
        self.control_lf.columnconfigure(1, weight=1)

        # HidHide が未設定なら、ゲームパッド操作のチェックを無効にし、
        # パッド行の右端に「HidHide セットアップ」を出す。長い案内文は
        # 置かず、無効な入口の隣に必要な操作を並べる。

        self.is_use_keyboard = tk.BooleanVar()
        self.cb_use_keyboard = ttk.Checkbutton(self.control_lf)
        self.cb_use_keyboard.config(
            text="キーボードで操作",
            variable=self.is_use_keyboard,
            command=self._on_keyboard_toggled,
        )
        self.cb_use_keyboard.grid(column=0, row=0, padx=6, pady=2, sticky="ew")
        Tooltip(self.cb_use_keyboard, "キーボードのキーを Switch のボタンとして送る")

        self.is_use_gamepad = tk.BooleanVar()
        self.cb_use_gamepad = ttk.Checkbutton(self.control_lf)
        self.cb_use_gamepad.config(
            text="ゲームパッドで操作",
            variable=self.is_use_gamepad,
            command=self._on_gamepad_toggled,
        )
        self.cb_use_gamepad.grid(column=1, row=0, padx=6, pady=2, sticky="ew")
        Tooltip(self.cb_use_gamepad, "接続したゲームパッドの入力を Switch へ送る")
        # 状態はここで1回だけ調べ、結果を保持する。設定の流し込み
        # （_apply_settings_to_widgets）は後から is_use_gamepad を
        # 上書きするため、そこでも同じ結果で門を閉じ直す。
        self._hidhide_gamepad_ok = self._gamepad_enabled_by_hidhide()
        self._apply_hidhide_gamepad_gate()

        self.cb_left_stick_mouse = ttk.Checkbutton(self.control_lf)
        self.cb_left_stick_mouse.config(
            text="左スティックをマウスで",
            variable=self.camera_lf.is_use_left_stick_mouse,
            command=self._on_left_stick_toggled,
        )

        self.cb_right_stick_mouse = ttk.Checkbutton(self.control_lf)
        self.cb_right_stick_mouse.config(
            text="右スティックをマウスで",
            variable=self.camera_lf.is_use_right_stick_mouse,
            command=self._on_right_stick_toggled,
        )
        self.cb_left_stick_mouse.grid(column=0, row=1, padx=6, pady=2, sticky="ew")
        self.cb_right_stick_mouse.grid(column=1, row=1, padx=6, pady=2, sticky="ew")
        Tooltip(self.cb_left_stick_mouse, "マウスを動かして左スティックを操作する")
        Tooltip(self.cb_right_stick_mouse, "マウスを動かして右スティックを操作する")
        # パッドの選択と遊びを 1 行にまとめる。HidHide が未設定なら
        # この行の右端にセットアップを出す（無効なチェックの理由が隣にある）。
        self.gamepad_row = ttk.Frame(self.control_lf)
        self.gamepad_row.grid(
            column=0, row=2, columnspan=2, padx=6, pady=2, sticky="ew"
        )
        self.gamepad_row.columnconfigure(1, weight=1)
        self.gamepad_label = ttk.Label(self.gamepad_row, text="パッド")
        self.gamepad_label.grid(row=0, column=0, sticky="w")
        self.gamepad_choice = tk.StringVar(value="")
        self.gamepad_cb = ttk.Combobox(
            self.gamepad_row, textvariable=self.gamepad_choice, width=24
        )
        self.gamepad_cb.grid(row=0, column=1, padx=(6, 0), sticky="ew")
        self.gamepad_cb.bind("<<ComboboxSelected>>", self._on_gamepad_selected, add="")
        # 手動の再接続。ホットプラグの自動巡回（2秒）で拾えない
        # 中途半端な状態から、SDL/HID のキャッシュを捨てて戻す。
        self.gamepad_reconnect_btn = ttk.Button(
            self.gamepad_row,
            text="再接続",
            width=6,
            command=self._on_gamepad_reconnect,
        )
        self.gamepad_reconnect_btn.grid(row=0, column=2, padx=(6, 0))
        Tooltip(
            self.gamepad_reconnect_btn,
            "ゲームパッドを探し直す（自動で拾えないとき）",
        )
        # 遊び（デッドゾーン）。既定1638=最大値の5%。drift 時に上げる。
        self.gamepad_dz_label = ttk.Label(self.gamepad_row, text="遊び")
        self.gamepad_dz_label.grid(row=0, column=3, padx=(14, 0), sticky="e")
        self.gamepad_dz = tk.StringVar(value="1638")
        self.gamepad_dz_spin = ttk.Spinbox(
            self.gamepad_row,
            textvariable=self.gamepad_dz,
            from_=0,
            to=8192,
            increment=100,
            width=7,
        )
        self.gamepad_dz_spin.grid(row=0, column=4, padx=(6, 0))
        Tooltip(
            self.gamepad_dz_spin,
            "スティックの小さな傾きを無視する範囲。勝手に動くときに上げる",
        )
        self.gamepad_dz_spin.bind("<FocusOut>", self._on_gamepad_dz_changed, add="")
        self.gamepad_dz_spin.bind("<Return>", self._on_gamepad_dz_changed, add="")
        # HidHide が未設定のときだけ、パッド行の右端に出す。
        self.hidhide_setup_btn = ttk.Button(
            self.gamepad_row,
            text="HidHide セットアップ",
            command=self._open_hidhide_setup,
        )
        self.hidhide_setup_btn.grid(row=0, column=5, padx=(10, 0))
        self.hidhide_setup_btn.grid_remove()
        # ゲームパッドの状態行。読取中の名前・経路・生軸値を出す。
        self.gamepad_status = tk.StringVar(value="")
        self.gamepad_status_label = ttk.Label(
            self.control_lf, textvariable=self.gamepad_status
        )
        self.gamepad_status_label.grid(
            column=0, columnspan=2, row=3, padx=6, pady=(0, 4), sticky="ew"
        )

        self.control_lf.config(text="コントローラ")
        self.control_lf.pack(fill="x", padx=5, pady=5)
        self._gamepad_after = None
        self._gamepad_ids = {}
        self._gamepad_names = {}
        self._gamepad_identity = ""
        self._gamepad_name = ""
        # 列挙と掴み直しはワーカーで行う。Tkスレッドで hid.enumerate や
        # stop_gamepad の join を待つと、コントローラ入れ替え時に固まる。
        self._gamepad_refresh_busy = False
        self._gamepad_refresh_thread: threading.Thread | None = None
        self._gamepad_restart_busy = False
        self._gamepad_restart_thread: threading.Thread | None = None
        self._gamepad_restart_pending: int | None = None
        self._gamepad_reconnect_busy = False
        self._gamepad_reconnect_thread: threading.Thread | None = None
        # 手動再接続では掴み直しを最後に1回だけ行う（一覧の反映と
        # 二重に走らせない）。
        self._gamepad_skip_auto_restart = False
        self.refreshGamepads()
        self._poll_gamepads()

        # ジャイロ送出。コントローラの機能のためこのタブに置く。
        # 仮想コントローラと連れにして別窓にも一緒に連れて行く。
        # 送り先・設定は都度引く（作り直し・切替に追従する）。
        def _make_imu_panel(master: Any) -> Any:
            from ImuPanel import ImuPanel

            return ImuPanel(
                master,
                lambda: getattr(self.serial.sender, "transport", None),
                self._read_imu_settings,
                self._write_imu_settings,
                lambda text: print(text),
            )

        # 仮想コントローラ。タブの中に常に置き、別ウィンドウへも出せる
        # （ui/controller_dock.py）。送り先は開くたびに今の物を引く。
        dock_area = ttk.Frame(self.tab_controller)
        dock_area.pack(fill="both", expand=True, padx=5, pady=(0, 5))
        try:
            self.controller_dock = ControllerDock(
                self.root,
                dock_area,
                lambda: self.serial.sender,
                viewport=getattr(self, "tabs_scroll", None),
                companion_factory=_make_imu_panel,
            )
        except Exception as e:
            logger.warning(f"コントローラ欄を作れませんでした: {e}")
            self.controller_dock = ControllerDock(
                self.root,
                dock_area,
                lambda: self.serial.sender,
                viewport=getattr(self, "tabs_scroll", None),
            )
        self.simpleConButton = self.controller_dock.pop_button

    def _gamepad_enabled_by_hidhide(self) -> bool:
        """「ゲームパッドで操作」を選べる状態か。

        HidHide が未設定なら HID 直結パッドは開けない。通常の読み取り
        経路は HID 直結だけで、SDL は POKECON_SDL_DEBUG のときしか使わない
        ため、チェックを押せても操作はできない。起動時に1回判定し、
        HidHide 設定の直後（`_after_hidhide_setup`）にやり直す。
        """
        try:
            from core.hidhide import hidhide_ready

            return bool(hidhide_ready())
        except Exception:
            return False

    def _apply_hidhide_gamepad_gate(self) -> None:
        """HidHide 未設定ならゲームパッド操作のチェックを無効にする。

        `_build_control_frame`（組み立て時）、`_apply_settings_to_widgets`
        （設定の流し込み時）、HidHide 設定後（`_after_hidhide_setup`）から
        呼ぶ。判定結果は `_hidhide_gamepad_ok` に固定し、ここでは再検出
        しない。未設定のときはパッド行の右端にセットアップを出す。
        """
        ok = bool(getattr(self, "_hidhide_gamepad_ok", False))
        self.cb_use_gamepad.config(state="normal" if ok else "disabled")
        self._sync_hidhide_setup_button()
        if ok:
            return
        if not self.is_use_gamepad.get():
            return
        # 設定に True が残っていても開始しない。既に動いている場合
        # （通常は起動直後で実体なし）は止める。
        self.is_use_gamepad.set(False)
        try:
            self.serial.set_gamepad_enabled(False)
        except Exception as e:
            logger.warning(f"ゲームパッド操作を止められませんでした: {e}")

    def _sync_hidhide_setup_button(self) -> None:
        """HidHide セットアップの入口を、未設定のときだけ出す。"""
        button = getattr(self, "hidhide_setup_btn", None)
        if button is None:
            return
        try:
            if getattr(self, "_hidhide_gamepad_ok", False):
                button.grid_remove()
            else:
                button.grid()
        except tk.TclError:
            pass

    def _after_hidhide_setup(self) -> bool:
        """HidHide 設定の直後に、起動時の判定をやり直して使い始める。

        設定が揃っていれば「ゲームパッドで操作」を自動で入れる。
        コントローラを挿し直さずに HID を開けることを実機で確認済み。
        """
        self._hidhide_gamepad_ok = self._gamepad_enabled_by_hidhide()
        self._apply_hidhide_gamepad_gate()
        if not self._hidhide_gamepad_ok:
            return False
        if self.is_use_gamepad.get():
            return True
        try:
            ready = self.serial.connection_snapshot().phase == "ready"
        except Exception:
            ready = False
        if not ready:
            return False
        self.is_use_gamepad.set(True)
        self.activateGamepad()
        enabled = bool(self.is_use_gamepad.get())
        self._on_setting_changed()
        return enabled

    def _open_hidhide_setup(self) -> None:
        """HidHide 設定の窓を開く（窓の単一管理は ui.hidhide_setup）。"""
        try:
            from ui.hidhide_setup import open_hidhide_setup

            open_hidhide_setup(self.root, on_applied=self._after_hidhide_setup)
        except Exception as e:
            logger.warning(f"HidHide設定を開けませんでした: {e}")

    def _read_imu_settings(self) -> dict[str, str]:
        """IMU節を読む。無ければ空辞書（呼出側が既定で補う）。"""
        try:
            parser = getattr(self.settings, "setting", None)
            if parser is None or not parser.has_section("IMU"):
                return {}
            return dict(parser["IMU"])
        except Exception:
            return {}

    def _write_imu_settings(self, values: dict[str, str]) -> None:
        """IMU節を書いて保存する。失敗は捨てる（送出は続ける）。"""
        try:
            parser = getattr(self.settings, "setting", None)
            if parser is None:
                return
            if not parser.has_section("IMU"):
                parser.add_section("IMU")
            for key, value in values.items():
                parser["IMU"][str(key)] = str(value)
            save = getattr(self.settings, "save", None)
            if callable(save):
                save()
        except Exception:
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

    def _refreshTransportChoices(self) -> None:
        """選択欄の候補を登録簿から組み直し、現在値を選ぶ。"""
        names = self.serial.list_transports()
        self.transport_cb.config(values=names)
        fit_combo_width(self.transport_cb, min_chars=12, max_chars=32)
        current = self._selectedTransportName()
        self.transport_name.set(current)
        self._refresh_transport_description()
        # 選択値が決まった今が Bcon タブを出す最初の機会
        self._refresh_bcon_tab()

    def applyTransport(self, event: Any = None) -> None:
        requested = str(self.transport_name.get())
        actual = (
            self.serial.connection_snapshot().transport_name
            or self._selectedTransportName()
        )
        actual = canonical_transport_name(actual) or actual
        self.transport_name.set(actual)
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
        self.arbitration_cb.config(values=list(self.serial.arbitration_modes()))
        fit_combo_width(self.arbitration_cb, min_chars=12, max_chars=32)
        mode = self.serial.resolve_arbitration(
            self.settings.arbitration_mode.get(), logger=logger
        )
        self.arbitration_mode.set(mode)

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
        mode = self.serial.resolve_arbitration(
            self.arbitration_mode.get(), logger=logger
        )
        # 解決後の名前を画面へ戻す。知らない名前を選んだまま残さない
        self.arbitration_mode.set(mode)
        self.settings.arbitration_mode.set(mode)
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
        show = self.serial.uses_bcon(self.transport_name.get())
        for widget in (
            self.bcon_status_label,
            self.bcon_status,
            self.bcon_wired_label,
            self.bcon_row,
        ):
            if show:
                widget.grid()
            else:
                widget.grid_remove()
        if show:
            self.bcon_hunt_button.pack(side="left", padx=(5, 0))
        else:
            self.bcon_hunt_button.pack_forget()

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
        self.com_port_notice.config(
            text="COM ポートが見つかりません。USB 接続を確認して『更新』を押してください。"
            if not has_ports
            else ""
        )
        refresh = getattr(self, "_refresh_bcon_controls", None)
        if callable(refresh):
            refresh()

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
        phases = {
            "disconnected": "未接続",
            "opening": "ポート接続中…",
            "hello": "HELLO 確認中…",
            "ready": "接続済み",
            "closing": "切断中…",
            "restarting": "再起動待ち",
            "bootloader": "BOOTSEL モード",
        }
        state = phases.get(
            snapshot.phase, "HELLO 失敗" if snapshot.opened else "接続失敗"
        )
        if snapshot.phase == "failed":
            state += f"：{snapshot.message}"
            if snapshot.opened and snapshot.baud is None:
                state += "\n速度を確認できません。『速度を探す』で確認してください。"
        elif snapshot.phase in {"restarting", "bootloader"}:
            state += f"\n{snapshot.message}"
        self.connection_status.config(text=state)
        chosen = str(self.com_port_name.get()) or "未選択"
        actual = snapshot.port_name or "不明"
        baud = f"{snapshot.baud}bps" if snapshot.baud is not None else "不明"
        if self.serial.uses_bcon(snapshot.transport_name or self.transport_name.get()):
            ack = snapshot.hello_ack
            hello = (
                "未確認"
                if ack is None
                else f"採用={ack[0]} / v{ack[1]}.{ack[2]} / 結果={ack[3]}"
            )
        else:
            hello = "対象外"
        self.connection_details.config(
            text=f"選択: {chosen}　実際: {actual}\n通信速度: {baud}　HELLO: {hello}"
        )
        status = snapshot.status if snapshot.opened else None
        switch = (
            "不明"
            if status is None
            else ("有線" if status[0] & _STATUS_FLAG_WIRED else "無線")
        )
        if (
            status is not None
            and snapshot.status_received_at is not None
            and time.monotonic() - snapshot.status_received_at > 3.0
        ):
            switch += "　更新停止（最終受信値）"
        self.bcon_wired.set(switch)
        payload = snapshot.player_info if snapshot.opened else b""
        lamp = payload[0] & 0x0F if payload else 0
        for index, item in enumerate(self._player_lamp_items):
            self.player_lamp_canvas.itemconfigure(
                item, fill="green yellow" if (lamp >> index) & 1 else "gray"
            )
        rumble = snapshot.rumble if snapshot.opened else b""
        from core.transport.bcon import decode_rumble

        left, right = decode_rumble(rumble)
        self.rumble_label.config(
            text=f"振動: L={left} R={right}" if rumble else "振動: （なし）"
        )

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

    def activateKeyboard(self) -> None:
        """キーボード操作の有効・無効を切り替える。

        実体の寿命は serial サービスが持つ。ここでは画面の辻褄
        （チェックの巻き戻し・フォーカス追従の結線）だけを見る。
        フォーカス追従は全 OS で行う（Tk の FocusIn/Out は共通）。
        """
        if self.is_use_keyboard.get():
            if self.serial.connection_snapshot().phase != "ready":
                return
            err = self.serial.set_keyboard_enabled(True, self.settings.setting_path)
            if err is not None:
                self.is_use_keyboard.set(False)
                return
            self._bindKeyboardFocus()
        else:
            self.serial.stop_keyboard()
            self._unbindKeyboardFocus()

    def _bindKeyboardFocus(self) -> None:
        """フォーカス追従を結線し、現在の状態へ即時合わせる。

        結線だけだと結線前の状態が残るため、直後に同期する。
        bind は同じ結び直しで置き換わるので重ね掛けは無害。
        """
        self.root.bind("<FocusIn>", self.onFocusInController)
        self.root.bind("<FocusOut>", self.onFocusOutController)
        self._syncKeyboardFocus()

    def _unbindKeyboardFocus(self) -> None:
        """フォーカス追従を外し、門を下ろす。"""
        self.root.unbind("<FocusIn>")
        self.root.unbind("<FocusOut>")
        self._kb_window_active.clear()

    def onFocusInController(self, event: Any) -> None:
        """窓内へフォーカスが戻ったら打鍵を受け付ける。

        部品間の移動でも来るが、立てるだけなので無害。実体が無い
        ときの作り直し（切断後の復帰用）は従来どおり残す。
        文字入力欄（ログの検索欄など）に入ったときは下ろす。打った
        文字がそのままコントローラーの操作として送られてしまうため。
        """
        if _is_text_input(getattr(event, "widget", None)):
            self._kb_window_active.clear()
            return
        if self.serial.connection_snapshot().phase != "ready":
            self._kb_window_active.clear()
            return
        self._kb_window_active.set()
        if self.serial.keyboard is not None:
            return
        if not self.is_use_keyboard.get():
            return
        err = self.serial.set_keyboard_enabled(True, self.settings.setting_path)
        if err is not None:
            self.is_use_keyboard.set(False)

    def onFocusOutController(self, event: Any) -> None:
        """窓外へ出たら打鍵を止める。

        部品間の移動でも来るため、その場で下ろさず直後に確かめる。
        従来の widget 照合（root のみ）は、子部品にフォーカスがある
        まま他アプリへ移ると外れて止まらなかった。終了間際の破棄で
        after 自体が例外のときは下ろしておく。
        """
        try:
            self.root.after(100, self._syncKeyboardFocus)
        except Exception:
            self._kb_window_active.clear()

    def _syncKeyboardFocus(self) -> None:
        """遅延判定：いま窓内のどこにも無ければ下ろす。"""
        try:
            widget = self.root.focus_get()
            focused = widget is not None and not _is_text_input(widget)
        except Exception:
            focused = False
        if focused:
            self._kb_window_active.set()
        else:
            self._kb_window_active.clear()

    def createControllerWindow(self) -> None:
        """仮想コントローラを別ウィンドウで開く（タブの埋め込みから移す）。"""
        self.controller_dock.pop_out()

    def closingController(self) -> None:
        """終了時。押しっぱなしを離してから、埋め込みも別窓も消す。"""
        try:
            self._cancel_gamepad_patrol()
        except Exception:
            pass
        dock = getattr(self, "controller_dock", None)
        if dock is not None:
            dock.shutdown()

    def _on_keyboard_toggled(self) -> None:
        """Use Keyboard の切り替え。有効化に失敗した場合も保存する。

        activateKeyboard は「シリアル未接続なら False へ戻す」ことがある。
        保存はその後に行い、画面に見えている状態と設定を一致させる。
        """
        self.activateKeyboard()
        self._on_setting_changed()

    def activateGamepad(self) -> None:
        """ゲームパッド操作の有効・無効を切り替える。

        実体の寿命は serial サービスが持つ。ここでは画面の辻褄
        （チェックの巻き戻し）だけを見る。読み取りの門はキーボードと
        同じフォーカス述語を使い、窓外の操作を流さない。
        """
        if self.is_use_gamepad.get():
            if self.serial.connection_snapshot().phase != "ready":
                return
            self.refreshGamepads()
            err = self.serial.set_gamepad_enabled(
                True, self._gamepad_index(), self._gamepad_deadzone()
            )
            if err is not None:
                self.is_use_gamepad.set(False)
                return
            self._bindGamepadMirror()
            # 専用の門を開ける。キーボードのフォーカス門とは独立。
            try:
                self._gamepad_active.set()
            except Exception:
                pass
        else:
            self.serial.stop_gamepad()
            self._clearGamepadMirror()
            try:
                self._gamepad_active.clear()
            except Exception:
                pass

    def _bindGamepadMirror(self) -> None:
        """ゲームパッド入力の鏡を結線する。Tk への受け渡しまで持つ。

        読取スレッドから直接 Canvas を触ると Tcl が壊れるため、
        root.after(0) で Tk スレッドへ予約してから描く。終了・無効化
        では _clearGamepadMirror で鏡を消す。
        """
        try:
            self.serial.set_gamepad_display(self._onGamepadDisplay)
        except Exception as e:
            logger.warning(f"ゲームパッド表示の結線で例外: {e}")

    def _onGamepadDisplay(self, snapshot: Any) -> None:
        """読取スレッドからの通知。Tk スレッドへ予約して描く。"""
        try:
            # 表示の更新は最新 1 件で足りる。裏から after は呼ばない。
            from core.ui_inbox import post_latest_to_gui

            post_latest_to_gui(self, "gamepad", self._paintGamepadMirror, snapshot)
        except Exception as e:
            logger.debug(f"ゲームパッド表示の受け渡しに失敗: {e}")

    def _paintGamepadMirror(self, snapshot: Any) -> None:
        """仮想パッドへゲームパッドの現在状態を描く（Tk スレッド専用）。"""
        try:
            dock = getattr(self, "controller_dock", None)
            pad = None
            if dock is not None:
                pad = getattr(dock, "embedded", None) or getattr(dock, "floating", None)
            data = dict(snapshot or {})
            self._setGamepadStatus(data)
            if not data.get("connected"):
                if pad is not None:
                    pad.clearExternal()
                return
            if pad is not None:
                pad.showExternal(
                    data.get("buttons", ()),
                    data.get("hat_dirs", ()),
                    tuple(data.get("stick_l", (128, 128))),
                    tuple(data.get("stick_r", (128, 128))),
                )
        except Exception as e:
            logger.warning(f"ゲームパッド表示の描画で例外: {e}")

    #: 状態行（名前・経路・生の軸値）を書き換える最小間隔。
    #: 入力は 60Hz で来るが、人が読める速さは数 Hz。毎フレーム書き換えると
    #: ラベルの再配置まで走り、描画スレッドの時間を削る。
    GAMEPAD_STATUS_INTERVAL_S = 0.1

    def _setGamepadStatus(self, data: dict[str, Any]) -> None:
        """状態行に名前・経路・生軸値を出す（Tk スレッド専用）。"""
        try:
            status = getattr(self, "gamepad_status", None)
            if status is None:
                return
            if not data.get("connected"):
                if getattr(self, "_gamepad_status_text", None) != "none":
                    self._gamepad_status_text = "none"
                    status.set("ゲームパッド: 未接続")
                return
            now = time.monotonic()
            last = getattr(self, "_gamepad_status_at", 0.0)
            if now - last < self.GAMEPAD_STATUS_INTERVAL_S:
                return
            self._gamepad_status_at = now
            name = str(data.get("pad_name", "") or "?")
            route = str(data.get("route", "") or "?")
            raw = tuple(data.get("raw_axes", ()) or ())
            raw_text = " ".join(f"{float(v):+.2f}" for v in raw[:6])
            buttons = ",".join(data.get("buttons", ()))
            text = f"{name} [{route}] 軸:{raw_text} {buttons}"
            if getattr(self, "_gamepad_status_text", None) == text:
                return
            self._gamepad_status_text = text
            status.set(text)
        except Exception:
            pass

    def _clearGamepadMirror(self) -> None:
        """鏡を消す（無効化・切断時。Tk スレッド専用）。"""
        try:
            self.serial.set_gamepad_display(None)
        except Exception:
            pass
        try:
            self._paintGamepadMirror({"connected": False})
        except Exception:
            pass

    def _gamepad_index(self) -> int:
        """ゲームパッド番号を通常の int で返す。読めないときだけ 0。"""
        try:
            index = int(self.settings.gamepad_index.get())
        except (TypeError, ValueError, tk.TclError):
            return 0
        return index if 0 <= index <= 3 else 0

    def _gamepad_deadzone(self) -> int:
        """遊びを通常の int で返す。読めない・範囲外は既定1638。"""
        try:
            value = int(self.settings.gamepad_deadzone.get())
        except (TypeError, ValueError, tk.TclError):
            return 1638
        return value if 0 <= value <= 8192 else 1638

    def _on_gamepad_dz_changed(self, event: Any = None) -> None:
        """遊びの変更を保存し、有効化中なら作り直して適用する。"""
        try:
            value = int(str(self.gamepad_dz.get()).strip())
        except (TypeError, ValueError, tk.TclError):
            value = self._gamepad_deadzone()
            try:
                self.gamepad_dz.set(str(value))
            except Exception:
                pass
            return
        value = max(0, min(8192, value))
        try:
            self.gamepad_dz.set(str(value))
            self.settings.gamepad_deadzone.set(value)
        except Exception:
            pass
        if self.is_use_gamepad.get():
            try:
                gamepad = self.serial.gamepad
                if gamepad is not None:
                    # 作り直さない。実体・スレッド・表示結線を保ったまま
                    # 遊びだけ変える。作り直すと開き直りの瞬間に未接続に
                    # 落ち、以後の申告が止まる。
                    gamepad.deadzone = max(0, min(8192, int(value)))
                else:
                    self.serial.stop_gamepad()
                    err = self.serial.set_gamepad_enabled(
                        True, self._gamepad_index(), value
                    )
                    if err is not None:
                        self.is_use_gamepad.set(False)
                        return
                    self._bindGamepadMirror()
            except Exception as e:
                logger.warning(f"遊びの適用で例外: {e}")
        self._on_setting_changed()

    def refreshGamepads(self) -> None:
        """繋がっているパッド一覧を取り直して選択肢へ入れる。

        抜き差ししても選び直せるよう、接続ボタン・チェック切替の
        際に呼び出す。選んでいた物が残っていれば残す。名前は SDL の
        実名があれば実名、XInput のみは汎名になる（PadSource.names）。

        列挙（SDLのpump・HIDのenumerate）はワーカーで行い、結果だけを
        `_apply_gamepad_listing` で GUI へ反映する。HID直結時は
        enumerate が USB 待ちで数十ms〜秒かかることがあり、2秒巡回の
        Tkスレッドで待つと固まる。前回のラベルはそのまま残す。
        選択保持・遊び値の非上書き・個体追従は従来どおり。
        """
        if getattr(self, "_gamepad_refresh_busy", False):
            return
        if getattr(self, "_gamepad_reconnect_busy", False):
            # 手動の再接続中は巡回を重ねない（列挙と掴み直しの競合を防ぐ）。
            return
        self._gamepad_refresh_busy = True
        thread = threading.Thread(
            target=self._refresh_gamepads_worker,
            name="gamepad-list",
            daemon=True,
        )
        self._gamepad_refresh_thread = thread
        try:
            thread.start()
        except Exception as e:
            self._gamepad_refresh_busy = False
            logger.warning(f"パッド一覧の更新を開始できません: {e}")

    def _refresh_gamepads_worker(self) -> None:
        """パッド一覧の列挙（ワーカー）。Tk には触らず inbox へ積む。"""
        try:
            from core.hidhide import hidhide_ready
            from core.pad_source import PadSource, sdl_debug_enabled

            source = PadSource(allow_sdl=sdl_debug_enabled(), allow_hid=hidhide_ready)
            names = source.names()
            # 名前と識別子を続けて取る。列挙の間に抜き差しが起きても、
            # ずれは次の巡回で直る（片方だけ古いまま固定しない）。
            try:
                identities = source.identities()
            except Exception:
                identities = {}
        except Exception as e:
            logger.warning(f"パッド一覧の列挙で例外: {e}")
            names = {}
            identities = {}
        try:
            # 表示の更新は最新 1 件で足りる。裏から after は呼ばない。
            from core.ui_inbox import post_latest_to_gui

            post_latest_to_gui(
                self,
                "gamepad_listing",
                self._apply_gamepad_listing,
                names,
                identities,
            )
        except Exception as e:
            logger.debug(f"パッド一覧の受け渡しに失敗: {e}")
        finally:
            self._gamepad_refresh_busy = False

    def _apply_gamepad_listing(
        self, names: dict[int, str], identities: dict[int, str]
    ) -> int:
        """列挙結果を一覧へ反映する（GUIスレッド専用）。

        戻り値は反映後の選択番号。手動再接続はこの戻り値で
        掴み直す（自動の掴み直しは `_gamepad_skip_auto_restart` で抑える）。
        """
        try:
            self._gamepad_ids = dict(identities)
            self._gamepad_names = dict(names)
            current = self._gamepad_index()
            moved = self._follow_gamepad(current, names)
            if moved is not None and moved != current:
                current = moved
                self.settings.gamepad_index.set(current)
                if not getattr(self, "_gamepad_skip_auto_restart", False):
                    self._restart_gamepad_at(current)
                self._on_setting_changed()
            labels = [f"{i}: {names.get(i, '(未接続)')}" for i in range(4)]
            self._gamepad_map = {label: i for i, label in enumerate(labels)}
            self.gamepad_cb["values"] = labels
            for label, index in self._gamepad_map.items():
                if index == current:
                    self.gamepad_choice.set(label)
                    break
            self._refresh_gamepad_deadzone_field()
            return current
        except Exception as e:
            logger.warning(f"パッド一覧の更新で例外: {e}")
            return self._gamepad_index()

    def _follow_gamepad(
        self, current: int, names: dict[int, str] | None = None
    ) -> int | None:
        """同じ個体が別の番号へ移っていれば、その番号を返す。

        SDL の番号（index）は抜き差しで詰め直されるため、番号だけで
        選んでいると別のパッドを掴む。接続中は使い回されない
        instance_id を鍵に、同じ個体の新しい番号を探す。
        再接続では instance_id 自体が変わるため、次に名前で探す
        （同名が複数あるときは選ばない）。
        見つからなければ None（今の番号のまま）を返し、今の番号の
        個体を新しい選択として覚え直す（抜けた個体を追い続けない）。
        """
        table = {} if names is None else names
        identities = getattr(self, "_gamepad_ids", {})
        ident = getattr(self, "_gamepad_identity", "")
        if ident:
            for index, value in identities.items():
                if value == ident:
                    key = int(index)
                    self._gamepad_name = table.get(key, self._gamepad_name)
                    return key
        # instance_id が変わった（＝抜き差しした）個体を名前で追う。
        name = getattr(self, "_gamepad_name", "")
        if name:
            same = [int(i) for i, value in table.items() if value == name]
            if len(same) == 1:
                key = same[0]
                self._gamepad_identity = identities.get(key, "")
                return key
        # 選択中の番号が一覧に無いなら、繋がっているのが1台だけのときは
        # その1台へ移る（設定に古い番号が残った起動や、HIDとSDLで
        # 番号がずれた状態を復帰させる）。
        if table and current not in table and len(table) == 1:
            key = next(iter(table))
            self._gamepad_identity = identities.get(key, "")
            self._gamepad_name = table[key]
            return int(key)
        if current in table:
            self._gamepad_identity = identities.get(current, "")
            self._gamepad_name = table[current]
        return None

    def _on_gamepad_reconnect(self) -> None:
        """手動の再接続。SDL/HID のキャッシュを捨てて列挙し直す。

        2秒の自動巡回は「繋がっている番号」を拾い直すだけで、
        掴んだままの実体や open のバックオフは捨てない。抜き差しが
        中途半端に終わったときは、ここで明示的にやり直す。
        破棄と列挙はワーカー、停止と作り直しは既存の掴み直し
        ワーカーで行う（Tk を止めない）。
        """
        if getattr(self, "_gamepad_reconnect_busy", False):
            return
        self._gamepad_reconnect_busy = True
        try:
            self.gamepad_reconnect_btn.state(["disabled"])
        except Exception:
            pass
        thread = threading.Thread(
            target=self._gamepad_reconnect_worker,
            name="gamepad-reconnect",
            daemon=True,
        )
        self._gamepad_reconnect_thread = thread
        try:
            thread.start()
        except Exception as e:
            self._gamepad_reconnect_busy = False
            self._re_enable_gamepad_reconnect()
            logger.warning(f"パッドの再接続を開始できません: {e}")

    def _gamepad_reconnect_worker(self) -> None:
        """キャッシュ破棄と再列挙（ワーカー）。Tk には触らない。"""
        err: str | None = None
        try:
            from core.hidhide import hidhide_ready
            from core.pad_source import PadSource, sdl_debug_enabled

            # 動いている読取器の経路・実体をまず捨てる。止めるのは
            # GUI 側の掴み直し（_restart_gamepad_at）に任せる。
            invalidated_live = False
            live = getattr(self.serial, "invalidate_gamepad", None)
            if callable(live):
                try:
                    invalidated_live = bool(live())
                except Exception as e:
                    logger.debug(f"読取器のキャッシュ破棄で例外: {e}")
            source = PadSource(allow_sdl=sdl_debug_enabled(), allow_hid=hidhide_ready)
            if not invalidated_live:
                # 実体が無い（無効中）ときも、共有 SDL のキャッシュは
                # ここで捨てる。一覧の取得前に列挙をやり直すため。
                source.invalidate()
            names = source.names()
            identities = source.identities()
        except Exception as e:
            logger.warning(f"パッドの再接続で例外: {e}")
            names, identities = {}, {}
            err = str(e) or "パッドを列挙できません"
        try:
            from core.ui_inbox import post_to_gui

            post_to_gui(
                self,
                self._finish_gamepad_reconnect,
                names,
                identities,
                err,
                on_drop=self._drop_gamepad_reconnect,
            )
        except Exception as e:
            logger.warning(f"パッドの再接続の受け渡しに失敗: {e}")
            self._drop_gamepad_reconnect()

    def _finish_gamepad_reconnect(
        self,
        names: dict[int, str],
        identities: dict[int, str],
        err: str | None,
    ) -> None:
        """再接続の結果を GUI へ反映する（GUIスレッド専用）。"""
        try:
            if err is not None:
                logger.warning(f"パッドの再列挙に失敗: {err}")
            # 一覧の反映では掴み直さず、このあと1回だけ行う。
            self._gamepad_skip_auto_restart = True
            try:
                current = self._apply_gamepad_listing(names, identities)
            finally:
                self._gamepad_skip_auto_restart = False
            # 番号が同じでも実体は掴み直す（手動再接続の目的）。
            self._restart_gamepad_at(current)
        except Exception as e:
            logger.warning(f"パッドの再接続の反映で例外: {e}")
        finally:
            self._gamepad_reconnect_busy = False
            self._re_enable_gamepad_reconnect()

    def _drop_gamepad_reconnect(self) -> None:
        """受け渡しが捨てられたときの後始末（ボタンを戻す）。"""
        self._gamepad_reconnect_busy = False
        self._re_enable_gamepad_reconnect()

    def _re_enable_gamepad_reconnect(self) -> None:
        try:
            self.gamepad_reconnect_btn.state(["!disabled"])
        except Exception:
            pass

    def _restart_gamepad_at(self, index: int) -> None:
        """番号が移った個体を掴み直す。有効化中だけ実体を作り直す。

        読取器は生成時の番号を固定で持つため、選択だけ移すと画面と
        実際に読むパッドが食い違う。stop_gamepad は読取スレッドの
        join（最大1秒）を待つため、Tkスレッドでは実行しない。ワーカーで
        止めてから作り直し、完了だけを GUI へ返す。連打時は最新の
        番号だけを残し、多重再起動しない。
        """
        try:
            if not self.is_use_gamepad.get():
                return
            deadzone = self._gamepad_deadzone()
        except Exception:
            return
        index = int(index)
        if getattr(self, "_gamepad_restart_busy", False):
            self._gamepad_restart_pending = index
            return
        self._gamepad_restart_busy = True
        thread = threading.Thread(
            target=self._restart_gamepad_worker,
            args=(index, deadzone),
            name="gamepad-restart",
            daemon=True,
        )
        self._gamepad_restart_thread = thread
        try:
            thread.start()
        except Exception as e:
            self._gamepad_restart_busy = False
            logger.warning(f"パッドの掴み直しを開始できません: {e}")

    def _restart_gamepad_worker(self, index: int, deadzone: int) -> None:
        """読取器の停止と作り直し（ワーカー）。Tk には触らない。"""
        err: str | None = None
        try:
            self.serial.stop_gamepad()
            err = self.serial.set_gamepad_enabled(True, index, deadzone)
        except Exception as e:
            logger.warning(f"パッドの掴み直しで例外: {e}")
            err = str(e) or "パッドを掴み直せません"
        try:
            from core.ui_inbox import post_to_gui

            post_to_gui(self, self._finish_gamepad_restart, index, err)
        except Exception as e:
            logger.debug(f"パッドの掴み直しの受け渡しに失敗: {e}")
            self._gamepad_restart_busy = False

    def _finish_gamepad_restart(self, index: int, err: str | None) -> None:
        """掴み直しの結果を GUI へ反映する（GUIスレッド専用）。"""
        try:
            if err is not None:
                self.is_use_gamepad.set(False)
            else:
                self._bindGamepadMirror()
        except Exception as e:
            logger.warning(f"パッドの掴み直しの反映で例外: {e}")
        finally:
            self._gamepad_restart_busy = False
        pending, self._gamepad_restart_pending = self._gamepad_restart_pending, None
        if pending is not None and pending != index:
            self._restart_gamepad_at(pending)

    def _refresh_gamepad_deadzone_field(self) -> None:
        """遊び欄を設定値へ戻す。ただし入力中は上書きしない。

        一覧の2秒巡回が、ユーザーが打っている途中の値を勝手に戻す
        実測があった。フォーカスが残っている間は触らず、外れている
        ときも設定値と同じなら書き換えない。
        """
        spin = getattr(self, "gamepad_dz_spin", None)
        try:
            if spin is not None and spin.focus_get() is spin:
                return
        except Exception:
            pass
        value = str(self._gamepad_deadzone())
        try:
            if str(self.gamepad_dz.get()).strip() == value:
                return
            self.gamepad_dz.set(value)
        except Exception:
            pass

    def _poll_gamepads(self) -> None:
        """抜き差しで一覧と状態行を追従させる画面側巡回（2秒毎）。

        Tk から線への往復はしない。選択肢の名前（実名・汎名・未接続）と
        状態行だけを取り直す。操作中（値を読んでいる最中）の上書きで
        選択が飛ばないよう、選んでいる番号は保つ（refreshGamepads が
        現在値を選び直す）。終了時は _cancel_gamepad_patrol で止める。
        """
        try:
            self.refreshGamepads()
        except Exception:
            pass
        try:
            self._gamepad_after = self.root.after(2000, self._poll_gamepads)
        except Exception:
            self._gamepad_after = None

    def _cancel_gamepad_patrol(self) -> None:
        """巡回の予約を取り消す。終了・破棄の前に呼ぶ。"""
        try:
            after_id = getattr(self, "_gamepad_after", None)
        except Exception:
            after_id = None
        if after_id is None:
            return
        try:
            self.root.after_cancel(after_id)
        except Exception:
            pass
        try:
            self._gamepad_after = None
        except Exception:
            pass

    def _on_gamepad_selected(self, event: Any = None) -> None:
        """選択肢で選んだパッドへ切り替える。途中でも切り替える。
        有効化中なら実体を作り直す（番号は生成時に固定のため）。
        無効中なら設定だけ残し、次回有効化で使う。
        """
        try:
            raw = str(self.gamepad_choice.get()).strip()
            index = self._gamepad_map.get(raw, None)
            if index is None:
                # 「0」のような番号の直接入力も受ける
                index = int(raw.split(":")[0].strip())
        except Exception:
            index = 0
        try:
            index = max(0, min(3, int(index)))
            self.settings.gamepad_index.set(index)
        except Exception:
            index = 0
        # 選んだ個体を覚える。次の巡回で番号が詰め直されても追える。
        try:
            self._gamepad_identity = getattr(self, "_gamepad_ids", {}).get(index, "")
            self._gamepad_name = getattr(self, "_gamepad_names", {}).get(index, "")
        except Exception:
            self._gamepad_identity = ""
            self._gamepad_name = ""
        if self.is_use_gamepad.get():
            # stop_gamepad は読取スレッドの join（最大1秒）を待つ。
            # Tk スレッドで止めると選択のたびに画面が固まるため、
            # 既存の掴み直しワーカーへ任せる（完了時に鏡を結線する）。
            self._restart_gamepad_at(index)
        self._on_setting_changed()

    def _on_gamepad_toggled(self) -> None:
        """ゲームパッド操作の切り替え。有効化に失敗した場合も保存する。"""
        self.activateGamepad()
        self._on_setting_changed()

    def _on_left_stick_toggled(self) -> None:
        self.activate_Left_stick_mouse()
        self._on_setting_changed()

    def _on_right_stick_toggled(self) -> None:
        self.activate_Right_stick_mouse()
        self._on_setting_changed()

    def activate_Left_stick_mouse(self) -> None:
        if self.preview is not None:
            self.preview.ApplyLStickMouse()

    def activate_Right_stick_mouse(self) -> None:
        if self.preview is not None:
            self.preview.ApplyRStickMouse()
