"""BCON 設定 UI の構築とリフロー（レイアウト専用 Mixin）。"""

from __future__ import annotations

import tkinter as tk
import tkinter.ttk as ttk
from typing import TYPE_CHECKING, Any

from pokecon.domain.bcon_status import COLOR_DEFAULTS, COLOR_SLOT_NAMES
from pokecon.ui.bcon.helpers import _TAB_MIN_HEIGHT, _TWO_COLUMN_MIN_WIDTH
from pokecon.ui.collapsible import CollapsibleSection, SectionFrame
from pokecon.ui.spacing import (
    GAP_GROUP,
    GAP_ITEM,
    PAD_BUTTON,
    TAB_PAD_X,
    TAB_PAD_Y,
    px,
)
from pokecon.ui.tooltip import Tooltip


class BconLayoutMixin:
    """BconSetup のウィジェット生成・並び替え・折り返し計測を担当する。"""

    if TYPE_CHECKING:
        window: Any
        _viewport: Any
        _body: Any
        _col_ops: Any
        _col_info: Any
        _two_columns: bool | None
        _embedded: bool
        _closed: bool
        _show_serial: Any
        _busy_label: Any
        _grp_main: Any
        _grp_pair: Any
        _grp_state: Any
        _grp_color: Any
        _grp_diag: Any
        _grp_keyboard: Any
        _grp_danger: Any
        _btn_status: Any
        _btn_reconnect: Any
        _reason_row: Any
        _recovery_row: Any
        _reason_text: Any
        _reason_label: Any
        _reason_actions: Any
        _btn_retry_quick: Any
        _btn_hunt_quick: Any
        _btn_open_serial: Any
        _result: Any
        _btn_w1: Any
        _btn_w0: Any
        _btn_winfo: Any
        _btn_e0: Any
        _btn_e1: Any
        _btn_e2: Any
        _btn_serial: Any
        _seconds: Any
        _seconds_input: Any
        _btn_cap: Any
        _btn_beacon: Any
        _mode_label: Any
        _lamp_canvas: Any
        _lamp_items: list[int]
        _imu_label: Any
        _vib_label: Any
        _colors: list[Any]
        _color_canvas: Any
        _color_items: list[int]
        _btn_color: Any
        _color_entries: list[Any]
        _summary: Any
        _actual_baud: Any
        _btn_connect: Any
        _btn_ping: Any
        _btn_hunt: Any
        _log: Any
        _kb_text: Any
        _kb_interval: Any
        _kb_interval_input: Any
        _btn_kb_send: Any
        _btn_kb_release: Any
        _kb_note: Any
        _kb_focus_note: Any
        _kb_mode_note: Any
        _btn_bootsel: Any
        _btn_keys: Any
        _keys_note: Any
        _body_size: tuple[int, int]

        def _refresh_color_preview(self) -> None: ...
        def _open_serial(self) -> None: ...
        def _on_status(self) -> None: ...
        def _on_reconnect(self) -> None: ...
        def _on_connect(self) -> None: ...
        def _on_hunt(self) -> None: ...
        def _on_wired_on(self) -> None: ...
        def _on_wired_off(self) -> None: ...
        def _on_wired_show(self) -> None: ...
        def _on_emulate_set(self, role: int) -> None: ...
        def _on_capture(self) -> None: ...
        def _on_beacon(self) -> None: ...
        def _on_color(self) -> None: ...
        def _on_ping(self) -> None: ...
        def _on_keyboard_send(self) -> None: ...
        def _on_keyboard_release(self) -> None: ...
        def _on_bootsel(self) -> None: ...
        def _on_delete_keys(self) -> None: ...

    def _build_ui(self, embedded: bool) -> None:
        """全セクションのウィジェットを構築し、初期レイアウトを整える。"""
        body = ttk.Frame(
            self.window,
            padding=(px(self.window, TAB_PAD_X), px(self.window, TAB_PAD_Y)),
        )
        self._body = body

        # 埋め込みタブは常に 1 列。2 列は幅によって位置が動き、状態が
        # 操作より下になるため使わない。別窓だけ従来の 2 列を残す。
        self._two_columns = None
        if embedded:
            body.pack(fill=tk.BOTH, expand=True)
            col_ops = col_info = body
            body.bind("<Configure>", lambda _e: self._reflow_columns(), add="+")
            self.window.bind("<Configure>", self._fit_viewport, add="+")
            self.window.bind("<Map>", self._fit_viewport, add="+")
            canvas = getattr(self._viewport, "canvas", None)
            if canvas is not None:
                canvas.bind("<Configure>", self._fit_viewport, add="+")
            self.window.after_idle(self._fit_viewport)
        else:
            body.pack(fill=tk.BOTH, expand=True)
            col_ops = col_info = body
        self._col_ops = col_ops
        self._col_info = col_info

        # 実行中は全ボタンを止める。止めた旨の表示だけ別に置く。
        self._busy_label = ttk.Label(col_ops, text="")
        self._busy_label.pack(anchor=tk.W)

        # 接続状態は下端の状態バーが全タブ共通で出す（タブ内に常時表示しない）。
        self._build_main_group(col_ops)
        self._build_pair_group(col_ops)
        self._build_state_group(col_info)
        self._build_color_group(col_info)
        self._build_diag_group(col_info, embedded=embedded)
        self._build_keyboard_group(col_info)
        self._build_danger_group(col_info)
        self._bind_tooltips()

        if embedded:
            self._order_embedded_groups()
        self._reflow_columns()
        self._bind_release_focus()

    def _build_main_group(self, col_ops: Any) -> None:
        """主要操作（状態更新・再接続・復旧ボタン）の枠を作る。"""
        grp_main = SectionFrame(col_ops, "")
        self._grp_main = grp_main
        grp_main.pack(fill=tk.X, pady=(0, px(self.window, GAP_GROUP)))
        main_body = grp_main.body
        main_body.columnconfigure(0, weight=1, uniform="btn")
        main_body.columnconfigure(1, weight=1, uniform="btn")
        self._btn_status = ttk.Button(
            main_body,
            text="状態を更新",
            command=self._on_status,
        )
        self._btn_status.grid(
            row=0,
            column=0,
            sticky=tk.EW,
            padx=px(self.window, PAD_BUTTON),
            pady=px(self.window, GAP_ITEM),
        )
        self._btn_reconnect = ttk.Button(
            main_body,
            text="Switchへ再接続",
            command=self._on_reconnect,
        )
        self._btn_reconnect.grid(
            row=0,
            column=1,
            sticky=tk.EW,
            padx=px(self.window, PAD_BUTTON),
            pady=px(self.window, GAP_ITEM),
        )
        # 理由と復旧操作は同じ 1 行。理由が空でもボタン高さを確保し、
        # 失敗時に下の折りたたみが動かないようにする。
        self._reason_row = ttk.Frame(main_body)
        self._recovery_row = self._reason_row
        self._reason_row.grid(
            row=1,
            column=0,
            columnspan=2,
            sticky=tk.EW,
            pady=(px(self.window, GAP_ITEM), 0),
        )
        self._reason_row.columnconfigure(0, weight=1)
        self._reason_text = tk.StringVar(value="")
        self._reason_label = ttk.Label(
            self._reason_row, textvariable=self._reason_text, justify=tk.LEFT
        )
        self._reason_label.grid(row=0, column=0, sticky=tk.W)
        self._reason_actions = ttk.Frame(self._reason_row)
        self._reason_actions.grid(row=0, column=1, sticky=tk.E)
        self._reason_actions.columnconfigure(0, weight=1, uniform="btn")
        self._reason_actions.columnconfigure(1, weight=1, uniform="btn")
        self._btn_retry_quick = ttk.Button(
            self._reason_actions,
            text="HELLO をやり直す",
            command=self._on_connect,
        )
        self._btn_retry_quick.grid(
            row=0, column=0, sticky=tk.EW, padx=px(self.window, PAD_BUTTON)
        )
        self._btn_hunt_quick = ttk.Button(
            self._reason_actions,
            text="速度を探す",
            command=self._on_hunt,
        )
        self._btn_hunt_quick.grid(
            row=0, column=1, sticky=tk.EW, padx=px(self.window, PAD_BUTTON)
        )
        self._btn_open_serial = ttk.Button(
            self._reason_actions,
            text="シリアルタブを開く",
            command=self._open_serial,
        )
        self._btn_open_serial.grid(
            row=0,
            column=0,
            columnspan=2,
            sticky=tk.EW,
            padx=px(self.window, PAD_BUTTON),
        )
        for btn in (self._btn_retry_quick, self._btn_hunt_quick):
            btn.grid_remove()
        self._btn_open_serial.grid_remove()
        main_body.rowconfigure(1, minsize=px(self.window, 30))
        self._result = ttk.Label(col_ops, text="", wraplength=250, justify=tk.LEFT)
        self._result.pack(fill=tk.X, pady=(0, 4))

    def _build_pair_group(self, col_ops: Any) -> None:
        """接続モード・種別・無線ペアリングの折りたたみ枠を作る。"""
        grp_pair = CollapsibleSection(
            col_ops,
            "接続モード・ペアリング",
            on_toggle=self._reflow_columns,
            bold=True,
        )
        grp_pair.pack(fill=tk.X, pady=(0, 0))
        self._grp_pair = grp_pair
        pair_body = grp_pair.body
        pair_body.columnconfigure(0, weight=1)

        # 操作の前に影響を示す（再起動でポートが外れる）。自動再接続はしない。
        ttk.Label(
            pair_body,
            text="接続方式・種別を変えると Pico が再起動し、COM ポートが外れます。"
            "再起動後はシリアルタブで接続し直してください。",
            wraplength=250,
            justify=tk.LEFT,
        ).grid(row=0, column=0, sticky=tk.W, pady=(2, 4))

        mode_row = ttk.Frame(pair_body)
        mode_row.grid(row=1, column=0, sticky=tk.EW)
        for column in (1, 2, 3):
            mode_row.columnconfigure(column, weight=1, uniform="btn")
        ttk.Label(mode_row, text="接続方式").grid(
            row=0, column=0, sticky=tk.W, padx=2, pady=2
        )
        self._btn_w1 = ttk.Button(
            mode_row, text="有線にする", command=self._on_wired_on
        )
        self._btn_w1.grid(row=0, column=1, sticky=tk.EW, padx=2, pady=2)
        self._btn_w0 = ttk.Button(
            mode_row, text="無線にする", command=self._on_wired_off
        )
        self._btn_w0.grid(row=0, column=2, sticky=tk.EW, padx=2, pady=2)
        self._btn_winfo = ttk.Button(
            mode_row, text="現在の方式", command=self._on_wired_show
        )
        self._btn_winfo.grid(row=0, column=3, sticky=tk.EW, padx=2, pady=2)

        type_row = ttk.Frame(pair_body)
        type_row.grid(row=2, column=0, sticky=tk.EW)
        for column in (1, 2, 3):
            type_row.columnconfigure(column, weight=1, uniform="btn")
        ttk.Label(type_row, text="種別").grid(
            row=0, column=0, sticky=tk.W, padx=2, pady=2
        )
        self._btn_e0 = ttk.Button(
            type_row, text="Pro Con", command=lambda: self._on_emulate_set(0)
        )
        self._btn_e0.grid(row=0, column=1, sticky=tk.EW, padx=2, pady=2)
        self._btn_e1 = ttk.Button(
            type_row, text="Joy-Con L", command=lambda: self._on_emulate_set(1)
        )
        self._btn_e1.grid(row=0, column=2, sticky=tk.EW, padx=2, pady=2)
        self._btn_e2 = ttk.Button(
            type_row, text="Joy-Con R", command=lambda: self._on_emulate_set(2)
        )
        self._btn_e2.grid(row=0, column=3, sticky=tk.EW, padx=2, pady=2)

        if self._show_serial is not None:
            self._btn_serial = ttk.Button(
                pair_body,
                text="シリアルタブを開く（一覧更新）",
                command=self._show_serial,
            )
            self._btn_serial.grid(row=3, column=0, sticky=tk.EW, pady=(0, 4))

        ttk.Separator(pair_body).grid(row=4, column=0, sticky=tk.EW, pady=6)
        ttk.Label(
            pair_body,
            text="無線ペアリング（無線モードのみ）",
            wraplength=250,
            justify=tk.LEFT,
        ).grid(row=5, column=0, sticky=tk.W)
        ttk.Label(
            pair_body,
            text="前提：無線モードで、Pico と Switch が未接続。"
            "取込時間内に Joy-Con の HOME を長押しします。PC と Pico の接続は維持されます。",
            wraplength=250,
            justify=tk.LEFT,
        ).grid(row=6, column=0, sticky=tk.W, pady=(2, 4))

        cap_row = ttk.Frame(pair_body)
        cap_row.grid(row=7, column=0, sticky=tk.EW)
        cap_row.columnconfigure(1, weight=1)
        ttk.Label(cap_row, text="取込時間").grid(row=0, column=0, sticky=tk.W, padx=2)
        self._seconds = tk.IntVar(value=15)
        self._seconds_input = ttk.Spinbox(
            cap_row,
            from_=1,
            to=60,
            width=4,
            textvariable=self._seconds,
        )
        self._seconds_input.grid(row=0, column=1, sticky=tk.W, padx=2, pady=2)
        ttk.Label(cap_row, text="秒").grid(row=0, column=2, sticky=tk.W, padx=2)
        self._btn_cap = ttk.Button(
            cap_row,
            text="取込開始",
            command=self._on_capture,
        )
        self._btn_cap.grid(row=0, column=3, sticky=tk.EW, padx=(6, 2), pady=2)

        self._btn_beacon = ttk.Button(
            pair_body,
            text="保存した入力を再生",
            command=self._on_beacon,
        )
        self._btn_beacon.grid(row=8, column=0, sticky=tk.EW, pady=(4, 2))
        ttk.Label(
            pair_body,
            text="取込は完了まで待ち、保存を確認してから『Switchへ再接続』を押します。",
            wraplength=250,
            justify=tk.LEFT,
        ).grid(row=9, column=0, sticky=tk.W, pady=(0, 2))

    def _build_state_group(self, col_info: Any) -> None:
        """コントローラ状態（方式・プレイヤーLED・ジャイロ・振動）の枠を作る。"""
        grp_state = SectionFrame(col_info, "コントローラ状態")
        self._grp_state = grp_state
        grp_state.pack(fill=tk.X, pady=(0, 0))
        state_body = grp_state.body
        state_body.columnconfigure(1, weight=1)
        ttk.Label(state_body, text="接続方式").grid(
            row=0, column=0, sticky=tk.W, padx=2, pady=2
        )
        self._mode_label = ttk.Label(state_body, text="—", justify=tk.LEFT)
        self._mode_label.grid(row=0, column=1, sticky=tk.W, padx=2, pady=2)
        ttk.Label(state_body, text="プレイヤーLED").grid(
            row=1,
            column=0,
            sticky=tk.W,
            padx=2,
            pady=2,
        )
        # PLAYER_INFO の LED は bit0=LED1..bit3=LED4。実機は縦 4 灯で
        # LED1 が最下段・LED4 が最上段。ここは左から LED1..LED4 の横並びで出す。
        self._lamp_canvas = tk.Canvas(
            state_body,
            width=120,
            height=30,
            highlightthickness=0,
        )
        self._lamp_canvas.grid(row=2, column=1, sticky=tk.W, padx=2, pady=2)
        self._lamp_items = []
        for index in range(4):
            left = 4 + index * 30
            self._lamp_items.append(
                self._lamp_canvas.create_rectangle(
                    left,
                    4,
                    left + 22,
                    26,
                    fill="",
                    outline="#4D4D4D",
                )
            )
        ttk.Label(state_body, text="ジャイロ").grid(
            row=3, column=0, sticky=tk.W, padx=2, pady=2
        )
        self._imu_label = ttk.Label(state_body, text="—")
        self._imu_label.grid(row=3, column=1, sticky=tk.W, padx=2, pady=2)
        ttk.Label(state_body, text="振動").grid(
            row=4, column=0, sticky=tk.W, padx=2, pady=2
        )
        self._vib_label = ttk.Label(state_body, text="—")
        self._vib_label.grid(row=4, column=1, sticky=tk.W, padx=2, pady=2)

    def _build_color_group(self, col_info: Any) -> None:
        """本体色の 4 スロット入力とプレビュー矩形の折りたたみ枠を作る。"""
        grp_color = CollapsibleSection(
            col_info,
            "本体色",
            on_toggle=self._reflow_columns,
            bold=True,
        )
        grp_color.pack(fill=tk.X, pady=(4, 0))
        self._grp_color = grp_color
        color_body = grp_color.body
        color_body.columnconfigure(1, weight=1)
        self._colors = []
        color_entries: list[Any] = []
        # 4 欄を 2x2 に並べる（横 1 列は狭い列で最も幅を取る）。
        slots = ttk.Frame(color_body)
        slots.grid(row=0, column=0, columnspan=2, sticky=tk.EW, padx=2, pady=2)
        per_row = 2
        for index, (slot, default) in enumerate(zip(COLOR_SLOT_NAMES, COLOR_DEFAULTS)):
            r, c = divmod(index, per_row)
            ttk.Label(slots, text=slot).grid(
                row=r,
                column=c * 2,
                padx=(6 if c == 0 else 2, 0),
                sticky=tk.W,
            )
            var = tk.StringVar(value=default)
            entry = ttk.Entry(slots, width=8, textvariable=var)
            entry.grid(row=r, column=c * 2 + 1, padx=2, pady=1)
            self._colors.append(var)
            color_entries.append(entry)
        self._color_canvas = tk.Canvas(
            color_body,
            width=96,
            height=20,
            highlightthickness=0,
        )
        self._color_canvas.grid(row=1, column=0, sticky=tk.W, padx=2, pady=2)
        self._color_items = []
        for index in range(4):
            left = 2 + index * 24
            self._color_items.append(
                self._color_canvas.create_rectangle(
                    left, 2, left + 20, 18, fill="#000000", outline="#4D4D4D"
                )
            )
        for entry, var in zip(color_entries, self._colors):
            entry.bind("<KeyRelease>", lambda _e: self._refresh_color_preview())
            var.trace_add("write", lambda *_a: self._refresh_color_preview())
        self._btn_color = ttk.Button(
            color_body,
            text="色を送る",
            command=self._on_color,
        )
        self._btn_color.grid(row=1, column=1, sticky=tk.EW, padx=2, pady=2)
        self._color_entries = color_entries
        self._refresh_color_preview()

    def _build_diag_group(self, col_info: Any, *, embedded: bool) -> None:
        """通信診断（HELLO 再試行・PING・速度探索）と詳細ログの枠を作る。"""
        grp_diag = CollapsibleSection(
            col_info,
            "通信テスト・詳細ログ",
            expand=True,
            on_toggle=self._reflow_columns,
            bold=True,
        )
        grp_diag.pack(fill=tk.BOTH, expand=True, pady=(4, 0))
        self._grp_diag = grp_diag
        diag = grp_diag.body
        diag.columnconfigure(0, weight=1)
        diag.rowconfigure(3, weight=1)
        self._summary = ttk.Label(diag, text="未接続", wraplength=250, justify=tk.LEFT)
        self._summary.grid(row=0, column=0, sticky=tk.EW)
        self._actual_baud = ttk.Label(diag, text="実際の速度: 不明")
        self._actual_baud.grid(row=1, column=0, sticky=tk.W)
        diag_buttons = ttk.Frame(diag)
        diag_buttons.grid(row=2, column=0, sticky=tk.EW, pady=(4, 4))
        for column in range(3):
            diag_buttons.columnconfigure(column, weight=1, uniform="btn")
        self._btn_connect = ttk.Button(
            diag_buttons,
            text="HELLO をやり直す",
            command=self._on_connect,
        )
        self._btn_connect.grid(row=0, column=0, sticky=tk.EW, padx=2, pady=2)
        self._btn_ping = ttk.Button(
            diag_buttons,
            text="疎通確認（PING）",
            command=self._on_ping,
        )
        self._btn_ping.grid(row=0, column=1, sticky=tk.EW, padx=2, pady=2)
        self._btn_hunt = ttk.Button(
            diag_buttons,
            text="速度を探す",
            command=self._on_hunt,
        )
        self._btn_hunt.grid(row=0, column=2, sticky=tk.EW, padx=2, pady=2)

        log_frame = ttk.Frame(diag)
        log_frame.grid(row=3, column=0, sticky=tk.NSEW)
        log_frame.rowconfigure(0, weight=1)
        log_frame.columnconfigure(0, weight=1)
        self._log = tk.Text(
            log_frame,
            height=8 if embedded else 16,
            # タブでは列の幅に合わせて伸びるので、要求幅は小さくしておく。
            width=36 if embedded else 72,
            state=tk.DISABLED,
        )
        self._log.grid(row=0, column=0, sticky=tk.NSEW)
        log_scroll = ttk.Scrollbar(
            log_frame, orient=tk.VERTICAL, command=self._log.yview
        )
        log_scroll.grid(row=0, column=1, sticky=tk.NS)
        self._log.configure(yscrollcommand=log_scroll.set)

    def _build_keyboard_group(self, col_info: Any) -> None:
        """キーボード入力検証用の折りたたみ枠を作る。"""
        grp_keyboard = CollapsibleSection(
            col_info, "キーボード入力（検証）", on_toggle=self._reflow_columns
        )
        self._grp_keyboard = grp_keyboard
        grp_keyboard.pack(fill=tk.X, pady=(px(self.window, GAP_GROUP), 0))
        kb_body = grp_keyboard.body
        for column in range(2):
            kb_body.columnconfigure(column, weight=1, uniform="kb")
        self._kb_text = ttk.Entry(kb_body)
        self._kb_text.grid(row=0, column=0, columnspan=2, sticky=tk.EW, pady=2)
        interval_row = ttk.Frame(kb_body)
        interval_row.grid(row=1, column=0, columnspan=2, sticky=tk.W, pady=2)
        ttk.Label(interval_row, text="間隔(ms)").pack(side=tk.LEFT)
        self._kb_interval = tk.IntVar(value=50)
        self._kb_interval_input = ttk.Spinbox(
            interval_row,
            from_=20,
            to=200,
            increment=10,
            textvariable=self._kb_interval,
            width=5,
        )
        self._kb_interval_input.pack(side=tk.LEFT, padx=2)
        self._btn_kb_send = ttk.Button(
            kb_body, text="入力して送信", command=self._on_keyboard_send
        )
        self._btn_kb_send.grid(row=2, column=0, sticky=tk.EW, padx=2, pady=2)
        self._btn_kb_release = ttk.Button(
            kb_body, text="キー解放", command=self._on_keyboard_release
        )
        self._btn_kb_release.grid(row=2, column=1, sticky=tk.EW, padx=2, pady=2)
        self._kb_note = ttk.Label(
            kb_body,
            text="US 配列の ASCII のみ。有線モード専用。日本語は送れません。",
            wraplength=250,
            justify=tk.LEFT,
        )
        self._kb_note.grid(row=3, column=0, columnspan=2, sticky=tk.W)
        self._kb_focus_note = ttk.Label(kb_body, wraplength=250, justify=tk.LEFT)
        self._kb_focus_note.grid(row=4, column=0, columnspan=2, sticky=tk.W)
        self._kb_mode_note = ttk.Label(kb_body, wraplength=250, justify=tk.LEFT)
        self._kb_mode_note.grid(row=5, column=0, columnspan=2, sticky=tk.W)

    def _build_danger_group(self, col_info: Any) -> None:
        """取り消せない保守操作の折りたたみ枠を最下部に作る。"""
        # 取り消せない操作だけ集める。他のボタンはここへ置かない
        # （押し間違いを防ぐための隔離であり、処理は何も変えない）。
        # 作る順＝Tab の移動順なので、状態・色・診断の後（最下部）に作る。
        grp_danger = CollapsibleSection(
            col_info,
            "保守（取り消せない操作）",
            on_toggle=self._reflow_columns,
            bold=True,
        )
        self._grp_danger = grp_danger
        danger_body = grp_danger.body
        danger_body.columnconfigure(0, weight=1, uniform="btn")
        danger_body.columnconfigure(1, weight=1, uniform="btn")
        self._btn_bootsel = ttk.Button(
            danger_body,
            text="BOOTSEL再起動",
            command=self._on_bootsel,
        )
        self._btn_bootsel.grid(
            row=0,
            column=0,
            columnspan=2,
            sticky=tk.EW,
            padx=2,
            pady=2,
        )
        self._btn_keys = ttk.Button(
            danger_body,
            text="ペア鍵を全削除",
            command=self._on_delete_keys,
        )
        self._btn_keys.grid(row=1, column=1, sticky=tk.EW, padx=2, pady=2)
        self._keys_note = ttk.Label(
            danger_body,
            text="有線モードでは鍵を削除できません。無線で起動してください。",
            wraplength=230,
            justify=tk.LEFT,
        )
        self._keys_note.grid(row=2, column=0, columnspan=2, sticky=tk.W, padx=2)
        self._keys_note.grid_remove()
        grp_danger.pack(fill=tk.X, pady=(4, 0))

    def _bind_tooltips(self) -> None:
        """各操作ボタンへ補足説明のツールチップを結ぶ。"""
        # ボタン面から外した用語と注意は吹き出しへ移す（面の文言を短く保つ）。
        Tooltip(
            self._btn_connect,
            "Pico と接続し、Switch を操作できる状態にする。",
        )
        Tooltip(
            self._btn_status,
            "Pico の状態と、接続中のコントローラ情報を表示する。",
        )
        Tooltip(self._btn_ping, "Pico との通信が通るか確認し、応答時間を表示する。")
        Tooltip(
            self._btn_cap,
            "指定した秒数だけ、実際のコントローラ操作を記録する。",
        )
        Tooltip(
            self._btn_beacon,
            "保存した操作を Pico から再生する。",
        )
        Tooltip(
            self._btn_reconnect,
            "Switch との接続をやり直す。",
        )
        Tooltip(
            self._btn_w1,
            "USB 接続に切り替える（Bluetooth は停止。約0.5秒後に再起動）。",
        )
        Tooltip(
            self._btn_w0,
            "Bluetooth 接続に切り替える（約0.5秒後に再起動）。",
        )
        Tooltip(
            self._btn_winfo,
            "いま USB 接続か Bluetooth 接続かを表示する。",
        )
        Tooltip(
            self._btn_e0,
            "Switch に Pro Controller として認識させる（再起動後に反映）。",
        )
        Tooltip(
            self._btn_e1,
            "Switch に Joy-Con (L) として認識させる（再起動後に反映）。",
        )
        Tooltip(
            self._btn_e2,
            "Switch に Joy-Con (R) として認識させる（再起動後に反映）。",
        )
        Tooltip(
            self._btn_bootsel,
            "ファームウェア書き込み用のモードで再起動する（COM ポートが外れる）。",
        )
        Tooltip(
            self._btn_keys,
            "無線モードで起動したときだけ、Classic リンク鍵と保存ホストを削除する。取込は残る。有線では実行できない。",
        )
        Tooltip(
            self._btn_color,
            "コントローラ本体の色を Pico に送る（プレイヤーLEDの色ではありません）。",
        )

    def _bind_release_focus(self) -> None:
        """操作部品をクリックした後、フォーカスをタブ／窓へ戻す。

        コンボにフォーカスが残ると、矢印キーが選択変更に奪われる。
        文字を打つ欄（Entry/Spinbox）はそのままにする。
        """
        target = self.window

        def walk(widget: Any) -> list[Any]:
            found: list[Any] = []
            try:
                children = widget.winfo_children()
            except Exception:
                return found
            for child in children:
                found.append(child)
                found.extend(walk(child))
            return found

        def release(*_args: Any) -> None:
            try:
                target.focus_set()
            except Exception:
                pass

        for widget in walk(self._body):
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
                widget.bind("<ButtonRelease-1>", release, add="+")
            elif cls in ("TCombobox", "Combobox"):
                widget.bind("<<ComboboxSelected>>", release, add="+")
            elif cls in ("TEntry", "Entry", "TSpinbox", "Spinbox"):
                # 入力後は Enter / FocusOut で確定する既存動作に任せる。
                continue

    def _order_embedded_groups(self) -> None:
        """埋め込み 1 列の並びを固定する（状態→操作→折りたたみ）。"""
        groups = [
            getattr(self, "_grp_state", None),
            getattr(self, "_grp_main", None),
            getattr(self, "_grp_pair", None),
            getattr(self, "_grp_diag", None),
            getattr(self, "_grp_color", None),
            getattr(self, "_grp_keyboard", None),
            getattr(self, "_grp_danger", None),
        ]
        for group in groups:
            if group is not None:
                group.pack_forget()
        busy = getattr(self, "_busy_label", None)
        if busy is not None:
            busy.pack_forget()
        result = getattr(self, "_result", None)
        if result is not None:
            result.pack_forget()
        # 理由行は main の中。残りのグループを上から並べ直す。
        first = True
        for group in groups:
            if group is None:
                continue
            pad = 0 if first else px(self.window, GAP_GROUP)
            group.pack(fill=tk.X, pady=(pad, 0))
            first = False
            if group is getattr(self, "_grp_main", None) and result is not None:
                result.pack(fill=tk.X, pady=(px(self.window, GAP_ITEM), 0))

    def _available_width(self) -> int:
        """タブの実際に見えている幅（Notebook の内側の全タブ共通幅ではない）。"""
        viewport = getattr(self, "_viewport", None)
        try:
            canvas = getattr(viewport, "canvas", None)
            if canvas is not None:
                width = int(canvas.winfo_width())
                if width > 1:
                    vbar = getattr(viewport, "vbar", None)
                    if vbar is not None:
                        try:
                            width -= int(vbar.winfo_reqwidth())
                        except Exception:
                            pass
                    right = canvas.winfo_rootx() + width
                    avail = right - self.window.winfo_rootx() - 12
                    return max(160, min(width, avail))
        except (tk.TclError, AttributeError, TypeError):
            pass
        try:
            width = int(self.window.winfo_width())
            if width > 1:
                return max(160, width - 12)
        except (tk.TclError, AttributeError):
            pass
        try:
            return max(160, int(self._body.winfo_width()))
        except (tk.TclError, AttributeError):
            return 320

    def _fit_viewport(self, _event: Any = None) -> None:
        """見えている幅が変わったら、体の幅と並べ方を測り直す。"""
        if getattr(self, "_closed", False):
            return
        self._reflow_columns()

    def _update_wrap(self, width: int) -> None:
        """1 列時のラベル折り返し幅。バー幅は常に引いた値を使う。"""
        column_w = max(160, width - px(self.window, TAB_PAD_X) * 2)
        for name in (
            "_result",
            "_summary",
            "_imu_label",
            "_vib_label",
            "_reason_label",
            "_kb_note",
            "_kb_focus_note",
            "_kb_mode_note",
        ):
            label = getattr(self, name, None)
            if label is None:
                continue
            try:
                current = int(float(label.cget("wraplength") or 0))
            except (tk.TclError, ValueError, TypeError):
                current = 0
            if abs(current - column_w) > 2:
                try:
                    label.configure(wraplength=column_w)
                except tk.TclError:
                    pass

    def _reflow_columns(self) -> None:
        """見えている幅に合わせて、操作列と情報列を左右に並べるか縦に積むか決める。

        幅は Notebook の内側（全タブ共通）ではなく、スクロールする欄で
        実際に見えている幅を使う。全タブ共通の幅で決めると、狭い窓でも
        左右に並べた幅が要求として残り、横スクロールが出続ける。
        """
        body, ops, info = self._body, self._col_ops, self._col_info
        if ops is info:
            self._update_wrap(self._available_width())
            return
        try:
            gap = 16
            ow, oh = ops.winfo_reqwidth(), ops.winfo_reqheight()
            iw, ih = info.winfo_reqwidth(), info.winfo_reqheight()
            width = self._available_width()
            # 左右に並べる下限。中身の要求幅で決めると、いったん 1 列に
            # 折り返した幅が要求として残り、広げても 2 列へ戻れなくなる。
            two = width >= _TWO_COLUMN_MIN_WIDTH
            if two != self._two_columns:
                self._two_columns = two
                if two:
                    # grid は前の指定（columnspan）を引き継ぐので明示して戻す。
                    ops.grid(row=0, column=0, columnspan=1, sticky=tk.NW)
                    info.grid(
                        row=0, column=1, columnspan=1, sticky=tk.NSEW,
                        padx=(gap, 0), pady=0,
                    )  # fmt: skip
                    body.rowconfigure(0, weight=1)
                    body.rowconfigure(1, weight=0)
                else:
                    ops.grid(row=0, column=0, columnspan=2, sticky=tk.NW)
                    info.grid(
                        row=1, column=0, columnspan=2, sticky=tk.NSEW, padx=0,
                        pady=(gap, 0),
                    )  # fmt: skip
                    body.rowconfigure(0, weight=0)
                    body.rowconfigure(1, weight=1)
            # padding=8 の上下左右の分を足す。
            height = (max(oh, ih) if two else oh + gap + ih) + 16
            body_width = max(160, width - 8)
            # 状態・ログの折り返しも見えている幅に合わせる（長い機器名や
            # エラー文で列を押し広げない）。
            column_w = (
                (max(160, width - gap - 32) // 2) if two else max(160, width - 32)
            )
            for name in (
                "_result",
                "_summary",
                "_imu_label",
                "_vib_label",
            ):
                label = getattr(self, name, None)
                if label is None:
                    continue
                try:
                    current = int(float(label.cget("wraplength") or 0))
                except (tk.TclError, ValueError, TypeError):
                    current = 0
                if current != column_w:
                    try:
                        label.configure(wraplength=column_w)
                    except tk.TclError:
                        pass
            size = (body_width, height)
            if getattr(self, "_body_size", None) != size:
                self._body_size = size
                body.place_configure(width=body_width, height=height)
            # タブの要求も狭い方にそろえる（全タブ最大幅で横スクロールが
            # 出続けないように）。中身が後から増えても追従させる。
            tab_width = min(width, max(ow, iw) + 16)
            for option, value in (
                ("width", tab_width),
                ("height", max(height + 4, _TAB_MIN_HEIGHT)),
            ):
                try:
                    if int(self.window.cget(option) or 0) != value:
                        self.window.configure(**{option: value})
                except (tk.TclError, ValueError, TypeError):
                    pass
        except tk.TclError:
            pass
