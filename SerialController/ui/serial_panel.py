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
from ui.collapsible import CollapsibleSection
from ui.combo_fit import fit_combo_width
from ui.controller_dock import ControllerDock
from ui.tooltip import Tooltip
from ui.transport_names import display_name as transport_display_name

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
        self.serial_lf = ttk.Frame(self.tab_serial, padding=(6, 4))
        self.serial_lf.columnconfigure(0, weight=1)

        # 接続状態は下端の状態バーへ出す（タブ内に常時表示しない）。
        # ここには失敗理由・復旧操作だけを、起きたときに出す。
        # 失敗理由・復旧操作（成功時は空にして高さを空けない）。
        self.com_port_notice = ttk.Label(self.serial_lf, wraplength=300, justify="left")
        self.com_port_notice.grid(row=1, column=0, sticky="ew", pady=(2, 2))
        self.com_port_notice.grid_remove()

        # 通信方式とポートは、広いときは左右、狭いときは縦に並べる。
        # 縦を詰めたいが、狭い幅で横に詰めると入力欄が痩せるため。
        self._setup_row = ttk.Frame(self.serial_lf)
        self._setup_row.grid(row=2, column=0, sticky="ew", pady=(4, 2))
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
        self.refreshComPort.grid(row=0, column=1, padx=(5, 0), sticky="e")
        self.com_port_cb.bind("<<ComboboxSelected>>", self.onComPortSelected, add="")

        # 主要操作。接続と再接続は同じボタン（状態で文字が変わる）。
        actions = ttk.Frame(self.serial_lf)
        actions.grid(row=3, column=0, sticky="ew", pady=(2, 2))
        actions.columnconfigure(0, weight=1)
        self.reloadComPort = ttk.Button(actions, command=self.reloadSerialPort)
        self.reloadComPort.config(text="接続")
        self.reloadComPort.grid(row=0, column=0, sticky="ew")
        self.disconnectComPort = ttk.Button(
            actions, text="切断", command=self.inactivateSerial
        )
        self.disconnectComPort.grid(row=0, column=1, padx=(6, 0), sticky="e")
        # 操作結果（失敗理由を含む）は押したボタンの直下へ。詳細ログは
        # 診断のまとまりに置き、結果を読むためにスクロールさせない。
        self.connection_result = ttk.Label(
            self.serial_lf, wraplength=300, justify="left"
        )
        self.connection_result.grid(row=4, column=0, sticky="ew", pady=(0, 2))
        self.connection_result.grid_remove()
        # 失敗時だけ出す復旧の導線（速度の探索は BCON の適用条件に従う）。
        self.recovery_button = ttk.Button(
            self.serial_lf, text="速度を探す", command=self.huntBconBaud
        )
        self.recovery_button.grid(row=5, column=0, sticky="w", pady=(2, 0))
        self.recovery_button.grid_remove()

        # Switch 側の状態は下端の状態バー（Pico↔Switch）に出す。

        # 詳細は 1 段だけ折りたたむ。開閉しても値・選択・ログは捨てない。
        self.serial_details = CollapsibleSection(
            self.serial_lf,
            "通信の詳細設定",
            on_toggle=self._on_serial_section_toggled,
        )
        self.serial_details.grid(row=7, column=0, sticky="ew", pady=(2, 0))
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
        self.bcon_hunt_button.pack(side="left", padx=(5, 0))
        self.baud_rate_cb.bind("<<ComboboxSelected>>", self.applyBaudRate, add="")
        # 通信方式の説明は詳細へ（長文で主要操作を押し下げない）。
        self.transport_description = ttk.Label(
            detail_body, wraplength=280, justify="left"
        )
        self.transport_description.grid(row=3, column=0, sticky="ew", pady=(6, 0))

        self.serial_diagnostics = CollapsibleSection(
            self.serial_lf,
            "診断・送信ログ",
            on_toggle=self._on_serial_section_toggled,
        )
        self.serial_diagnostics.grid(row=8, column=0, sticky="ew", pady=(2, 0))
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
        self.serial_lf.place(x=0, y=0)
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

    def _build_control_frame(self) -> None:
        # コントローラタブも 1 列で「状態 → 主要操作 → 詳細」の順に並べる。
        # 表示幅が狭いとき、横 2 列のチェックやコンボは詰まって読めなくなる。
        self.control_lf = ttk.Frame(self.tab_controller, padding=(6, 4))
        self.control_lf.columnconfigure(0, weight=1)
        self.control_lf.place(x=5, y=5)
        # 折りたたみを開くと要求高さが変わる。実際の高さが変わった時点で
        # 操作盤の位置を取り直す（after_idle の順序に依存しない）。
        self.control_lf.bind("<Configure>", self._fit_control_viewport, add="+")

        # 状態（送信先・手動入力の受付）はアプリ下端の状態バーが全タブ共通で
        # 出す。ここに重複して置くと、タブを開かないと状態が見えなくなる。

        # キーボード。操作方法と既存の割り当てへの導線を入口の直下に置く。
        self.is_use_keyboard = tk.BooleanVar()
        self.cb_use_keyboard = ttk.Checkbutton(self.control_lf)
        self.cb_use_keyboard.config(
            text="キーボードで操作",
            variable=self.is_use_keyboard,
            command=self._on_keyboard_toggled,
        )
        self.cb_use_keyboard.grid(row=0, column=0, sticky="w")
        Tooltip(self.cb_use_keyboard, "キーボードのキーを Switch のボタンとして送る")
        # キーの意味はキー割当画面で読める。ここに長い説明は置かない。
        self.key_config_button = ttk.Button(
            self.control_lf, text="キー割当", command=self._open_key_config
        )
        self.key_config_button.grid(row=0, column=1, sticky="e", padx=(6, 0))

        # ゲームパッド。パッド名は横幅を確保し、調整値は詳細へ送る。
        self.is_use_gamepad = tk.BooleanVar()
        self.cb_use_gamepad = ttk.Checkbutton(self.control_lf)
        self.cb_use_gamepad.config(
            text="ゲームパッドで操作",
            variable=self.is_use_gamepad,
            command=self._on_gamepad_toggled,
        )
        self.cb_use_gamepad.grid(row=2, column=0, sticky="w")
        Tooltip(self.cb_use_gamepad, "接続したゲームパッドの入力を Switch へ送る")
        # 状態はここで1回だけ調べ、結果を保持する。設定の流し込み
        # （_apply_settings_to_widgets）は後から is_use_gamepad を
        # 上書きするため、そこでも同じ結果で門を閉じ直す。
        self._hidhide_gamepad_ok = self._gamepad_enabled_by_hidhide()

        self.gamepad_row = ttk.Frame(self.control_lf)
        self.gamepad_row.grid(row=4, column=0, sticky="ew")
        self.gamepad_row.columnconfigure(1, weight=1)
        self.gamepad_label = ttk.Label(self.gamepad_row, text="パッド")
        self.gamepad_label.grid(row=0, column=0, sticky="w", padx=(0, 4))
        self.gamepad_choice = tk.StringVar(value="")
        self.gamepad_cb = ttk.Combobox(
            self.gamepad_row, textvariable=self.gamepad_choice, width=24
        )
        self.gamepad_cb.grid(row=0, column=1, sticky="ew")
        self.gamepad_cb.bind("<<ComboboxSelected>>", self._on_gamepad_selected, add="")
        # 手動の再接続。ホットプラグの自動巡回（2秒）で拾えない
        # 中途半端な状態から、SDL/HID のキャッシュを捨てて戻す。
        self.gamepad_reconnect_btn = ttk.Button(
            self.gamepad_row,
            text="再接続",
            command=self._on_gamepad_reconnect,
        )
        self.gamepad_reconnect_btn.grid(row=0, column=2, padx=(6, 0))
        Tooltip(
            self.gamepad_reconnect_btn,
            "ゲームパッドを探し直す（自動で拾えないとき）",
        )
        # 読み取りの状態。設定の有効/無効とは別に、機器から読めているかを出す。
        self.gamepad_status = tk.StringVar(value="読み取り：未接続")
        self.gamepad_status_label = ttk.Label(
            self.control_lf, textvariable=self.gamepad_status, wraplength=300
        )
        self.gamepad_status_label.grid(row=5, column=0, sticky="ew", pady=(2, 0))

        # HidHide 未設定のときは、無効な入口のすぐ下に理由と入口を並べる。
        self.hidhide_row = ttk.Frame(self.control_lf)
        self.hidhide_row.grid(row=6, column=0, sticky="ew", pady=(2, 0))
        self.hidhide_row.columnconfigure(0, weight=1)
        self.hidhide_reason = ttk.Label(
            self.hidhide_row,
            text="HidHide が未設定のため、ゲームパッド入力を Switch へ送れません。",
            wraplength=240,
            justify="left",
        )
        self.hidhide_reason.grid(row=0, column=0, sticky="w")
        self.hidhide_setup_btn = ttk.Button(
            self.hidhide_row,
            text="HidHide セットアップ",
            command=self._open_hidhide_setup,
        )
        self.hidhide_setup_btn.grid(row=0, column=1, padx=(6, 0), sticky="e")
        self._apply_hidhide_gamepad_gate()

        # 詳細（既定は閉じる）。開閉しても入力値・選択は消さない。
        self.mouse_section = CollapsibleSection(
            self.control_lf, "マウス操作", on_toggle=self._on_control_section_toggled
        )
        self.mouse_section.grid(row=7, column=0, sticky="ew", pady=(4, 0))
        self.cb_left_stick_mouse = ttk.Checkbutton(
            self.mouse_section.body, command=self._on_left_stick_toggled
        )
        self.cb_left_stick_mouse.config(
            text="左スティックをマウスで",
            variable=self.camera_lf.is_use_left_stick_mouse,
        )
        self.cb_left_stick_mouse.pack(anchor="w")
        self.cb_right_stick_mouse = ttk.Checkbutton(
            self.mouse_section.body, command=self._on_right_stick_toggled
        )
        self.cb_right_stick_mouse.config(
            text="右スティックをマウスで",
            variable=self.camera_lf.is_use_right_stick_mouse,
        )
        self.cb_right_stick_mouse.pack(anchor="w")
        Tooltip(self.cb_left_stick_mouse, "マウスを動かして左スティックを操作する")
        Tooltip(self.cb_right_stick_mouse, "マウスを動かして右スティックを操作する")

        self.pad_section = CollapsibleSection(
            self.control_lf,
            "ゲームパッドの調整",
            on_toggle=self._on_control_section_toggled,
        )
        self.pad_section.grid(row=8, column=0, sticky="ew", pady=(2, 0))
        self.gamepad_dz_label = ttk.Label(
            self.pad_section.body, text="スティックの遊び（0〜8192）"
        )
        self.gamepad_dz_label.grid(row=0, column=0, sticky="w")
        # 遊び（デッドゾーン）。既定1638=最大値の5%。drift 時に上げる。
        self.gamepad_dz = tk.StringVar(value="1638")
        self.gamepad_dz_spin = ttk.Spinbox(
            self.pad_section.body,
            textvariable=self.gamepad_dz,
            from_=0,
            to=8192,
            increment=100,
            width=8,
        )
        self.gamepad_dz_spin.grid(row=1, column=0, sticky="w", pady=(2, 0))
        ttk.Label(
            self.pad_section.body,
            text="大きくするほど、スティックの小さな傾きを無視します（既定 1638）。",
            wraplength=280,
            justify="left",
        ).grid(row=2, column=0, sticky="w", pady=(2, 0))
        Tooltip(
            self.gamepad_dz_spin,
            "スティックの小さな傾きを無視する範囲。勝手に動くときに上げる",
        )
        self.gamepad_dz_spin.bind("<FocusOut>", self._on_gamepad_dz_changed, add="")
        self.gamepad_dz_spin.bind("<Return>", self._on_gamepad_dz_changed, add="")

        # 入力調停は「コマンド実行中の手動入力」として、手動入力の近くへ。
        self.arbitration_section = CollapsibleSection(
            self.control_lf,
            "コマンド実行中の手動入力",
            on_toggle=self._on_control_section_toggled,
        )
        self.arbitration_section.grid(row=9, column=0, sticky="ew", pady=(2, 0))
        self.arbitration_label = ttk.Label(
            self.arbitration_section.body, text="入力調停"
        )
        self.arbitration_label.grid(row=0, column=0, sticky="w")
        # 候補は送信側の許可値から引く（画面に名前を書き並べない）。
        # 既定の off は本家と同じ挙動で、script を選ぶと実行中の手操作を断る。
        self.arbitration_cb = ttk.Combobox(self.arbitration_section.body)
        self.arbitration_cb.config(state="readonly", width=24)
        self.arbitration_cb.grid(row=1, column=0, sticky="ew")
        self.arbitration_cb.bind("<<ComboboxSelected>>", self.applyArbitration, add="")
        Tooltip(
            self.arbitration_cb,
            "コマンド実行中に手動操作を受け付けるかどうか",
        )
        self.arbitration_hint = ttk.Label(
            self.arbitration_section.body, wraplength=300, justify="left"
        )
        self.arbitration_hint.grid(row=2, column=0, sticky="ew", pady=(2, 0))

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
        self._update_controller_status()
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
        # タブの内側は全タブ共通の幅なので、見えている幅へ体を合わせる
        # （Notebook 全体が広いままでも、主要操作を横スクロールなしで見せる）。
        dock_area = ttk.Frame(self.tab_controller)
        self._dock_area = dock_area
        dock_area.place(x=5, y=300)
        self.tab_controller.bind("<Configure>", self._fit_control_viewport, add="+")
        self.tab_controller.bind("<Map>", self._fit_control_viewport, add="+")
        viewport = getattr(self, "tabs_scroll", None)
        canvas = getattr(viewport, "canvas", None)
        if canvas is not None:
            canvas.bind("<Configure>", self._fit_control_viewport, add="+")
        self.root.after_idle(self._fit_control_viewport)
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
        self._bind_release_focus(self.tab_controller, self.tab_controller)

    def _on_control_section_toggled(self) -> None:
        """詳細の開閉で高さが変わるので、配置と操作盤の高さを測り直す。"""
        try:
            self.root.after_idle(self._fit_control_viewport)
        except tk.TclError:
            pass

    def _control_available_width(self) -> int:
        """コントローラタブで実際に見えている幅。"""
        viewport = getattr(self, "tabs_scroll", None)
        try:
            canvas = getattr(viewport, "canvas", None)
            if canvas is not None:
                width = int(canvas.winfo_width())
                if width > 1:
                    right = canvas.winfo_rootx() + width
                    avail = right - self.tab_controller.winfo_rootx() - 12
                    return max(160, min(width, avail))
        except (tk.TclError, AttributeError, TypeError):
            pass
        try:
            return max(160, int(self.tab_controller.winfo_width()))
        except (tk.TclError, AttributeError):
            return 320

    def _control_visible_height(self) -> int:
        """コントローラタブで実際に見えている高さ。"""
        viewport = getattr(self, "tabs_scroll", None)
        try:
            canvas = getattr(viewport, "canvas", None)
            if canvas is not None and int(canvas.winfo_height()) > 1:
                return int(canvas.winfo_height())
        except (tk.TclError, AttributeError, TypeError):
            pass
        try:
            return int(self.tab_controller.winfo_height())
        except (tk.TclError, AttributeError):
            return 480

    def _fit_control_viewport(self, event: Any = None) -> None:
        """見えている幅・高さへ、操作欄と操作盤を収める。

        Notebook の内側は全タブ共通の幅なので、これを使わないと狭い窓でも
        広い幅のまま配置され、主要操作に横スクロールが要る。
        """
        if getattr(self, "_closing", False):
            return
        frame = getattr(self, "control_lf", None)
        dock_area = getattr(self, "_dock_area", None)
        if frame is None or dock_area is None:
            return
        width = self._control_available_width()
        wrap = max(120, width - 36)
        for label in (
            getattr(self, "send_target_label", None),
            getattr(self, "manual_state_label", None),
            getattr(self, "gamepad_status_label", None),
            getattr(self, "arbitration_hint", None),
        ):
            if label is not None:
                self._set_wrap(label, wrap)
        self._set_wrap(getattr(self, "hidhide_reason", None), max(100, wrap - 130))
        try:
            if int(float(frame.place_info().get("width") or 0)) != width:
                frame.place_configure(width=width)
            top = frame.winfo_reqheight() + 14
            # 操作盤とジャイロ欄の要求高さをそのまま使う。見えている高さへ
            # 押し込めると、スクロールする欄なのに下側が切れる。タブの高さを
            # 内容に合わせて伸ばし、はみ出た分は ScrollHost にスクロールさせる。
            content = int(dock_area.winfo_reqheight())
            if content <= 1:
                content = max(240, self._control_visible_height() - top - 10)
            dock_height = max(240, content)
            info = dock_area.place_info()
            if (
                int(float(info.get("y") or 0)) != top
                or int(float(info.get("width") or 0)) != width
                or int(float(info.get("height") or 0)) != dock_height
            ):
                dock_area.place_configure(x=5, y=top, width=width, height=dock_height)
            tab_height = max(top + dock_height + 6, _TAB_MIN_HEIGHT)
            size = (min(width, 520), tab_height)
            if getattr(self, "_control_tab_size", None) != size:
                self._control_tab_size = size
                self.tab_controller.config(width=size[0], height=size[1])
        except (tk.TclError, AssertionError):
            pass
        dock = getattr(self, "controller_dock", None)
        if dock is not None:
            try:
                dock._refit_embedded()
            except Exception:
                pass

    def _open_key_config(self) -> None:
        """既存のキー割当画面を開く（割り当ての確認・変更への導線）。"""
        opener = getattr(getattr(self, "menu", None), "OpenKeyConfig", None)
        if not callable(opener):
            print("メニューの『ツール > キーコンフィグ』から確認できます。")
            return
        try:
            opener()
        except Exception as e:
            logger.warning(f"キー割当を開けませんでした: {e}")

    def _set_label_text(self, label: Any, text: str) -> None:
        """同じ文を入れ直さない（ラベルの折り返し再計算を積まない）。"""
        if label is None:
            return
        try:
            if str(label.cget("text")) == text:
                return
        except (tk.TclError, AttributeError, KeyError):
            pass
        try:
            label.config(text=text)
        except (tk.TclError, AttributeError):
            pass

    @staticmethod
    def _walk_widgets(widget: Any) -> list[Any]:
        found: list[Any] = []
        try:
            children = widget.winfo_children()
        except Exception:
            return found
        for child in children:
            found.append(child)
            found.extend(SerialPanelMixin._walk_widgets(child))
        return found

    def _release_focus_later(self, target: Any) -> None:
        """クリックした操作部品からフォーカスを外し、タブへ戻す。

        ボタンやコンボにフォーカスが残ると、矢印キーが選択変更に奪われ、
        キーボード操作（Switch へ送る）と両方に効いてしまう。
        文字を打つ欄（Entry/Text/Spinbox）はそのままにする。
        """

        def release(*_args: Any) -> None:
            try:
                target.focus_set()
            except Exception:
                pass

        try:
            self.root.after_idle(release)
        except Exception:
            pass

    def _bind_release_focus(self, container: Any, target: Any) -> None:
        """操作部品をクリックした後、フォーカスをタブへ戻すよう結線する。"""
        for widget in self._walk_widgets(container):
            try:
                cls = widget.winfo_class()
            except Exception:
                continue
            if cls in (
                "TButton",
                "TCheckbutton",
                "TRadiobutton",
                "Button",
                "Checkbutton",
                "Radiobutton",
            ):
                widget.bind(
                    "<ButtonRelease-1>",
                    lambda _e, t=target: self._release_focus_later(t),
                    add="+",
                )
            elif cls in ("TCombobox", "Combobox"):
                # 開いている候補を閉じた後（選択後）に戻す。押下時だと
                # ドロップダウンが閉じてしまう。
                widget.bind(
                    "<<ComboboxSelected>>",
                    lambda _e, t=target: self._release_focus_later(t),
                    add="+",
                )

    def _update_controller_status(self) -> None:
        """手動入力の受付状態を、取得できる情報だけで更新する。

        送信先・手動入力はタブ内だけの状態にせず、アプリ下端の状態バー
        （layout_panel）へ渡す。どのタブを開いていても同じ 1 か所で見える。
        """
        try:
            snapshot = self.serial.connection_snapshot()
        except Exception:
            return
        runner = getattr(self, "runner", None)
        busy = bool(runner is not None and runner.is_busy())
        if busy:
            state = "手動入力: 停止（コマンド実行中）"
        elif snapshot.phase != "ready" or not snapshot.opened:
            state = "手動入力: 接続待ち"
        else:
            sources: list[str] = []
            if bool(
                getattr(self, "is_use_keyboard", None) and self.is_use_keyboard.get()
            ):
                sources.append("キーボード")
            if bool(
                getattr(self, "is_use_gamepad", None) and self.is_use_gamepad.get()
            ):
                sources.append("パッド")
            mouse_on = bool(
                getattr(self, "cb_left_stick_mouse", None) is not None
                and self.camera_lf.is_use_left_stick_mouse.get()
            ) or bool(
                getattr(self, "cb_right_stick_mouse", None) is not None
                and self.camera_lf.is_use_right_stick_mouse.get()
            )
            if mouse_on:
                sources.append("マウス")
            if sources:
                state = "手動入力: 受付中（" + "・".join(sources) + "）"
            else:
                state = "手動入力: 停止（入力なし）"
        status = getattr(self, "status_input", None)
        if status is not None:
            try:
                if str(status.get()) != state:
                    status.set(state)
            except Exception:
                pass

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
        """HidHide セットアップの入口と理由を、未設定のときだけ出す。"""
        button = getattr(self, "hidhide_setup_btn", None)
        reason = getattr(self, "hidhide_reason", None)
        row = getattr(self, "hidhide_row", None)
        ok = bool(getattr(self, "_hidhide_gamepad_ok", False))
        try:
            if row is not None:
                if ok:
                    row.grid_remove()
                else:
                    row.grid()
            elif button is not None:
                if ok:
                    button.grid_remove()
                else:
                    button.grid()
            if reason is not None:
                if ok:
                    reason.grid_remove()
                else:
                    reason.grid()
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
        self.arbitration_cb.config(values=list(self.serial.arbitration_modes()))
        fit_combo_width(self.arbitration_cb, min_chars=12, max_chars=32)
        mode = self.serial.resolve_arbitration(
            self.settings.arbitration_mode.get(), logger=logger
        )
        self.arbitration_mode.set(mode)
        try:
            self.arbitration_cb.set(mode)
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
        mode = self.serial.resolve_arbitration(
            self.arbitration_mode.get(), logger=logger
        )
        # 解決後の名前を画面へ戻す。知らない名前を選んだまま残さない
        self.arbitration_mode.set(mode)
        try:
            self.arbitration_cb.set(mode)
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
        if not has_ports:
            messages.append(
                "COM ポートが見つかりません。USB 接続を確認して『一覧更新』を押してください。"
            )
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
        if self.serial.uses_bcon(transport):
            ack = snapshot.hello_ack
            hello = (
                "未確認"
                if ack is None
                else f"採用={ack[0]} / v{ack[1]}.{ack[2]} / 結果={ack[3]}"
            )
        else:
            hello = "対象外"
        self._set_label_text(
            self.connection_details,
            f"選択: {chosen}　実際: {actual}\n通信速度: {baud}　HELLO: {hello}",
        )
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
        self._update_controller_status()
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
                    status.set("読み取り：未接続")
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
            text = f"読み取り：{name} [{route}] 軸:{raw_text} {buttons}"
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
        self._update_controller_status()
        self._on_setting_changed()

    def _on_left_stick_toggled(self) -> None:
        self.activate_Left_stick_mouse()
        self._update_controller_status()
        self._on_setting_changed()

    def _on_right_stick_toggled(self) -> None:
        self.activate_Right_stick_mouse()
        self._update_controller_status()
        self._on_setting_changed()

    def activate_Left_stick_mouse(self) -> None:
        if self.preview is not None:
            self.preview.ApplyLStickMouse()

    def activate_Right_stick_mouse(self) -> None:
        if self.preview is not None:
            self.preview.ApplyRStickMouse()
