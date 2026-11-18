#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ImuPanel.py - ジャイロ送出の操作面（BconSetupへ組み込む部品）。

BconSetupのcol_infoへ部品として組み込む。入力源の選択・波形の
設定・送出の開始/停止・統計の表示を担う。線の操作は作業スレッドで行い、
画面の更新だけafter()で戻す（BconSetupと同一規律）。

使い方:
    panel = ImuPanel(parent, transport_provider, settings_provider, log)
    panel.pack(fill=tk.X, pady=(0, 8))
    panel.close()  # 終了時に送出を止める
"""

from __future__ import annotations

import math
import os
import queue
import threading
import time
import tkinter as tk
from collections.abc import Callable
from tkinter import font as tkfont, ttk
from typing import Any

from pokecon.domain.controller_preview import PreviewControls, controls_from_pad
from pokecon.runtime import imu_service
from pokecon.runtime.imu_service import (
    CURRENT_INVERT_LABEL as CURRENT_INVERT_LABEL,
    DEBUG_SOURCES as DEBUG_SOURCES,
    INVERT_LABELS as INVERT_LABELS,
    INVERT_SIGNS as INVERT_SIGNS,
    ROLL_LABELS as ROLL_LABELS,
    SCALE_PRESETS as SCALE_PRESETS,
    SOURCE_LABELS as SOURCE_LABELS,
    LiveTrim as _LiveTrim,
)
from pokecon.ui.collapsible import CollapsibleSection, SectionFrame
from pokecon.ui.imu_preview import MotionPreview
from pokecon.ui.spacing import GAP_GROUP, GAP_ITEM, px

# マウスキャプチャ中だけ各ウィジェットの bindtags 先頭へ差し込むタグ。
# ウィジェット自身のマウス処理（仮想パッドのスティック掴み等）より先に
# 走らせ、キャプチャ中のクリックをTkへ届かせない。
_CAPTURE_BLOCK_TAG = "PokeConMouseCaptureBlock"


class ImuPanel:
    """ジャイロ送出の操作面。"""

    def __init__(
        self,
        master: Any,
        transport_provider: Callable[[], Any],
        settings_provider: Callable[[], dict[str, str]],
        settings_writer: Callable[[dict[str, str]], None],
        log: Callable[[str], None],
        *,
        pad_provider: Callable[[], Any] | None = None,
        preview_provider: Callable[[], Any] | None = None,
    ) -> None:
        """transport_providerは送り先、settingsはIMU節の読み書き、logは行出し。

        pad_providerは仮想パッド、preview_providerはカメラ面のCaptureArea。
        どちらもキャプチャ開始/停止時にマウス掴みを解放するために引く。
        """
        self._transport_provider = transport_provider
        self._settings_provider = settings_provider
        self._settings_writer = settings_writer
        self._log = log
        self._pad_provider = pad_provider
        self._preview_provider = preview_provider
        self._queue: queue.Queue = queue.Queue()
        self._stop = False
        self._streaming = False
        # 開始/停止の作業スレッドが走っている間のフラグ。連打で
        # _do_start と _do_stop が並走しないようにする。
        self._starting = False
        self._stopping = False
        # 開始中にEscが押されたら、開始完了直後に止める。
        self._stop_after_start = False
        # 送出中に毎サンプル読む加工値（倍率, 不感帯raw, 加速度符号,
        # ジャイロ符号, HIDバイアスraw, HID軸別倍率）。断面を不変タプルで
        # 持つので、GUIスレッドからの差し替えは次のサンプルから効く。
        self._live_trim: _LiveTrim | None = None
        self._sensor_reader: Any = None
        self._step_gen: Any = None
        # 記録と再生。記録器は送出ループのスレッドが書き、停止時に閉じる。
        # 再生は「記録の再生」源として同じ送出ループに渡す。
        self._recorder: Any = None
        self._player: Any = None
        self._motion_preview: MotionPreview | None = None
        self._preview_controls = PreviewControls()
        self._preview_controls_at = 0.0
        self._preview_control_reader: Any = None
        self._playing = False
        self._source_error = ""
        # マウス入力（Raw Input）。キャプチャは送出中だけ動かす。
        self._stream_source = ""
        self._mouse_source: Any = None
        self._mouse_capture: Any = None
        self._mouse_interval_s = 0.0
        self._mouse_capture_on = False
        # 入力イベントレート表示用（前回の累計と時刻）。
        self._mouse_last_events: float | None = None
        self._mouse_last_events_t: float | None = None
        # 開始時にGUIスレッドで読む。非表示窓の窓関数からTkを触らない。
        self._owner_hwnd = 0
        self._clip_rect: tuple[int, int, int, int] | None = None
        self._saved_cursors: list[tuple[Any, str]] = []
        self._saved_bindtags: list[tuple[Any, tuple[str, ...]]] = []
        self._esc_bindings: list[tuple[Any, str]] = []
        # 開始時にGUIスレッドで読み取った値。作業スレッドからTk変数を
        # 読むと「main thread is not in main loop」で落ちる。
        self._start_play_path = ""
        self._start_record_path = ""
        self._start_record_on = False
        # 直近1秒の最大振幅。表示は瞬時値のため、単発の跳び(spike)を
        # 見落とす。Switch側の誤動作切り分け用に統計行へ出す。
        self._peak_g = 0.0
        self._peak_a = 0.0
        self._acc_min: list[int | None] = [None, None, None]
        self._acc_max: list[int | None] = [None, None, None]
        # 送出レート表示用（前回の累計と時刻）。
        self._last_sent: int | None = None
        self._last_sent_t: float | None = None

        frame = ttk.Frame(master)
        frame.columnconfigure(1, weight=1)
        self.frame = frame
        # キャプチャ中は全ウィジェットの先頭でマウスイベントを止める。
        # Escはメイン窓と（別窓表示中の）この窓の両方で受ける。ゲームへ
        # 出さないため、グローバルホットキーにはしない。
        try:
            toplevel = frame.winfo_toplevel()
            for sequence in (
                "<ButtonPress>",
                "<ButtonRelease>",
                "<MouseWheel>",
                "<B1-Motion>",
                "<B3-Motion>",
                # パッドの _hover が cursor を fleur/hand2 へ変えて
                # 非表示カーソルが復活するのを防ぐ。
                "<Motion>",
                "<Enter>",
                "<Leave>",
            ):
                toplevel.bind_class(
                    _CAPTURE_BLOCK_TAG, sequence, self._block_mouse_event
                )
            # キャプチャ中は入力欄などがEscを消費しても、必ず停止を優先する。
            # このタグはキャプチャ中だけ bindtags の先頭に入る。
            toplevel.bind_class(_CAPTURE_BLOCK_TAG, "<Escape>", self._on_escape)
            targets = [toplevel]
            root_getter = getattr(frame, "_root", None)
            root = root_getter() if callable(root_getter) else None
            if root is not None and str(root) != str(toplevel):
                targets.append(root)
            for target in targets:
                funcid = target.bind("<Escape>", self._on_escape, add="+")
                if funcid:
                    self._esc_bindings.append((target, funcid))
        except Exception:
            self._esc_bindings = []

        # 見出しと状態と開始/停止を 1 行にまとめる。枠線は使わない。
        heading = ttk.Label(frame, text="ジャイロ")
        try:
            bold = tkfont.nametofont("TkDefaultFont").copy()
            bold.configure(weight="bold")
            heading.configure(font=bold)
        except Exception:
            pass
        heading.grid(row=0, column=0, sticky=tk.W, padx=(0, px(frame, GAP_GROUP)))
        self._stats_var = tk.StringVar(value="停止中")
        self._stats_label = ttk.Label(
            frame, textvariable=self._stats_var, wraplength=180, justify=tk.LEFT
        )
        self._stats_label.grid(row=0, column=1, sticky=tk.W, padx=4)
        controls = ttk.Frame(frame)
        controls.grid(row=0, column=2, sticky=tk.E)
        ttk.Button(controls, text="3Dプレビュー", command=self._on_motion_preview).pack(
            side=tk.LEFT, padx=(0, px(frame, GAP_ITEM))
        )
        self._btn_toggle = ttk.Button(controls, text="開始", command=self._on_toggle)
        self._btn_toggle.pack(side=tk.LEFT)

        self._settings = CollapsibleSection(frame, "ジャイロ設定")
        self._settings.grid(
            row=1, column=0, columnspan=3, sticky=tk.EW, pady=(px(frame, GAP_ITEM), 0)
        )
        body = self._settings.body
        self._body = body
        body.columnconfigure(1, weight=1)

        ttk.Label(body, text="入力源").grid(
            row=0, column=0, sticky=tk.W, padx=4, pady=4
        )
        self._source_var = tk.StringVar(value="HID直結")
        self._source_cb = ttk.Combobox(
            body,
            width=14,
            state="readonly",
            textvariable=self._source_var,
            values=[label for _, label in SOURCE_LABELS],
        )
        self._source_cb.grid(row=0, column=1, sticky=tk.W, padx=4, pady=4)

        # パッド番号は SDL センサー入力のときだけ意味がある。行ごと
        # 出し分け、ラベルで説明しない（レイアウトで示す）。
        self._pad_frame = ttk.Frame(body)
        ttk.Label(self._pad_frame, text="パッド").grid(
            row=0, column=0, sticky=tk.W, padx=2, pady=2
        )
        self._pad_var = tk.StringVar(value="0")
        self._pad_cb = ttk.Combobox(
            self._pad_frame,
            width=10,
            state="readonly",
            textvariable=self._pad_var,
            values=["0", "1", "2", "3"],
        )
        self._pad_cb.grid(row=0, column=1, sticky=tk.EW, padx=2, pady=2)
        self._pad_frame.grid(row=1, column=0, columnspan=2, sticky=tk.EW)
        self._btn_step = ttk.Button(body, text="STEP発火", command=self._on_step_fire)
        self._btn_step.grid(row=1, column=2, sticky=tk.EW, padx=2, pady=2)

        # 再生ファイルの行。「記録の再生」を選んだときだけ出す。
        self._play_frame = ttk.Frame(body)
        self._play_frame.columnconfigure(1, weight=1)
        ttk.Label(self._play_frame, text="再生ファイル").grid(
            row=0, column=0, sticky=tk.W, padx=2, pady=2
        )
        self._play_path_var = tk.StringVar(value="")
        self._play_entry = ttk.Entry(
            self._play_frame, width=18, textvariable=self._play_path_var
        )
        self._play_entry.grid(row=0, column=1, sticky=tk.EW, padx=2, pady=2)
        self._play_browse = ttk.Button(
            self._play_frame, text="参照…", command=self._on_pick_play
        )
        self._play_browse.grid(row=0, column=2, sticky=tk.E, padx=2, pady=2)
        self._play_frame.grid(row=1, column=0, columnspan=2, sticky=tk.EW)
        self._play_frame.grid_remove()

        self._invert_label = ttk.Label(body, text="反転")
        self._invert_label.grid(row=3, column=0, sticky=tk.W, padx=2, pady=2)
        self._invert_var = tk.StringVar(value=CURRENT_INVERT_LABEL)
        self._invert_cb = ttk.Combobox(
            body,
            width=10,
            state="readonly",
            textvariable=self._invert_var,
            values=[CURRENT_INVERT_LABEL, *(label for _, label in INVERT_LABELS)],
        )
        self._invert_cb.grid(row=3, column=1, sticky=tk.EW, padx=2, pady=2)
        self._invert_cb.bind("<<ComboboxSelected>>", self._on_invert_changed, add="+")
        self._scale_label = ttk.Label(body, text="送出倍率")
        self._scale_label.grid(row=4, column=0, sticky=tk.W, padx=2, pady=2)
        self._scale_var = tk.StringVar(value="1.0")
        self._scale_cb = ttk.Combobox(
            body,
            width=10,
            state="readonly",
            textvariable=self._scale_var,
            values=list(SCALE_PRESETS),
        )
        self._scale_cb.grid(row=4, column=1, sticky=tk.EW, padx=2, pady=2)
        self._scale_cb.bind("<<ComboboxSelected>>", self._on_scale_changed, add="+")
        self._debug_var = tk.BooleanVar(value=False)
        self._debug_cb = ttk.Checkbutton(
            body,
            text="デバッグ",
            variable=self._debug_var,
            command=self._on_debug_toggled,
        )
        self._debug_cb.grid(row=0, column=2, sticky=tk.E, padx=2, pady=2)
        # 記録は任意。チェックするまで保存先を畳み、入力源の設定を先に見せる。
        # 記録の有無は開始時に読む（送出中の切替は次回の開始から効く）。
        self._record_frame = ttk.Frame(body)
        self._record_frame.columnconfigure(0, weight=1)
        self._record_var = tk.BooleanVar(value=False)
        self._record_cb = ttk.Checkbutton(
            self._record_frame,
            text="送出値を記録する",
            variable=self._record_var,
            command=self._on_record_toggled,
        )
        self._record_cb.grid(row=0, column=0, sticky=tk.W, padx=2)
        self._record_path_frame = ttk.Frame(self._record_frame)
        self._record_path_frame.columnconfigure(1, weight=1)
        ttk.Label(self._record_path_frame, text="ファイル").grid(
            row=0, column=0, sticky=tk.W, padx=2, pady=2
        )
        self._record_path_var = tk.StringVar(value="records/motion.pgyro")
        self._record_entry = ttk.Entry(
            self._record_path_frame, width=18, textvariable=self._record_path_var
        )
        self._record_entry.grid(row=0, column=1, sticky=tk.EW, padx=2, pady=2)
        self._record_browse = ttk.Button(
            self._record_path_frame, text="参照…", command=self._on_pick_record
        )
        self._record_browse.grid(row=0, column=2, sticky=tk.E, padx=2, pady=2)
        self._record_path_frame.grid(row=1, column=0, sticky=tk.EW, pady=(2, 0))
        self._record_path_frame.grid_remove()
        self._record_frame.grid(
            row=5, column=0, columnspan=3, sticky=tk.EW, pady=(8, 0)
        )
        # 手動入力: 6軸の生値を直接指定して送る。入力源が「手動入力」の
        # ときだけ表示する。値はtraceでメインスレッドから取り込み、
        # 送出スレッドはスナップショット(int6)だけを読む。
        self._manual_values = [0, 0, 4096, 0, 0, 0]
        self._manual_vars: list[tk.StringVar] = []
        # 入れ子 LabelFrame を廃し、枠線なしの SectionFrame で見出しと余白だけでまとめる。
        self._manual_frame = SectionFrame(body, "手動入力（生値 LSB）")
        manual_body = self._manual_frame.body
        for i, (name, dflt) in enumerate(
            zip(("AX", "AY", "AZ", "GX", "GY", "GZ"), self._manual_values)
        ):
            r, c = divmod(i, 3)
            ttk.Label(manual_body, text=name).grid(
                row=r, column=c * 2, sticky=tk.W, padx=(8 if c else 0, 4), pady=4
            )
            var = tk.StringVar(value=str(dflt))
            spin = ttk.Spinbox(
                manual_body,
                from_=-32768,
                to=32767,
                width=6,
                textvariable=var,
            )
            spin.grid(row=r, column=c * 2 + 1, sticky=tk.W, padx=(0, 4), pady=4)
            normalize = self._make_manual_normalizer(i, var)
            spin.bind("<FocusOut>", normalize, add="+")
            spin.bind("<Return>", normalize, add="+")
            var.trace_add("write", self._make_manual_trace(i, var))
            self._manual_vars.append(var)
        # 反転・感度の行（4）と重ねない。
        self._manual_frame.grid(
            row=6,
            column=0,
            columnspan=3,
            sticky=tk.EW,
            pady=(px(frame, GAP_GROUP), 0),
        )
        self._manual_frame.grid_remove()
        # マウス入力: 日常使う操作を先に、感度計算と詳細調整は必要時だけ開く。
        # ホットキーはキャプチャ解除用（修飾キー必須）。
        self._mouse_frame = ttk.Frame(body)
        self._mouse_frame.columnconfigure(0, weight=1)
        top_row = ttk.Frame(self._mouse_frame)
        top_row.grid(row=0, column=0, sticky=tk.EW)
        ttk.Label(top_row, text="感度").pack(side=tk.LEFT, padx=2, pady=2)
        self._mouse_sens_var = tk.StringVar(value="0.05")
        self._mouse_sens_spin = ttk.Spinbox(
            top_row,
            from_=0.0001,
            to=10.0,
            increment=0.005,
            width=9,
            format="%.4f",
            textvariable=self._mouse_sens_var,
        )
        self._mouse_sens_spin.pack(side=tk.LEFT, padx=(0, 2), pady=2)
        ttk.Label(top_row, text="上下比").pack(side=tk.LEFT, padx=(12, 2), pady=2)
        self._mouse_ratio_var = tk.StringVar(value="1.0")
        self._mouse_ratio_spin = ttk.Spinbox(
            top_row,
            from_=0.05,
            to=10.0,
            increment=0.05,
            width=6,
            format="%.2f",
            textvariable=self._mouse_ratio_var,
        )
        self._mouse_ratio_spin.pack(side=tk.LEFT, padx=(0, 2), pady=2)
        invert_row = ttk.Frame(self._mouse_frame)
        invert_row.grid(row=1, column=0, sticky=tk.W, pady=2)
        self._mouse_invert_x_var = tk.BooleanVar(value=False)
        self._mouse_invert_x_cb = ttk.Checkbutton(
            invert_row,
            text="左右反転",
            variable=self._mouse_invert_x_var,
            command=self._on_mouse_setting_changed,
        )
        self._mouse_invert_x_cb.pack(side=tk.LEFT, padx=2)
        self._mouse_invert_y_var = tk.BooleanVar(value=False)
        self._mouse_invert_y_cb = ttk.Checkbutton(
            invert_row,
            text="上下反転",
            variable=self._mouse_invert_y_var,
            command=self._on_mouse_setting_changed,
        )
        self._mouse_invert_y_cb.pack(side=tk.LEFT, padx=2)
        hotkey_row = ttk.Frame(self._mouse_frame)
        hotkey_row.grid(row=3, column=0, sticky=tk.EW)
        hotkey_row.columnconfigure(1, weight=1)
        ttk.Label(hotkey_row, text="解除キー").grid(
            row=0, column=0, sticky=tk.W, padx=2, pady=2
        )
        self._mouse_hotkey_var = tk.StringVar(value="ctrl+alt+m")
        self._mouse_hotkey_entry = ttk.Entry(
            hotkey_row, width=16, textvariable=self._mouse_hotkey_var
        )
        self._mouse_hotkey_entry.grid(row=0, column=1, sticky=tk.EW, padx=2, pady=2)
        # 送出中でも感度・上下比・反転は次のサンプルから効かせる。
        # 打鍵ごとではなく、確定（FocusOut/Return）で読み取る。
        for spin in (self._mouse_sens_spin, self._mouse_ratio_spin):
            spin.bind("<FocusOut>", self._on_mouse_setting_changed, add="+")
            spin.bind("<Return>", self._on_mouse_setting_changed, add="+")
        self._mouse_hotkey_entry.bind(
            "<FocusOut>", self._on_mouse_setting_changed, add="+"
        )
        self._mouse_hotkey_entry.bind(
            "<Return>", self._on_mouse_setting_changed, add="+"
        )
        # DPIと360°距離は感度を決めるときだけ使う。結果は計算欄の中で
        # 改行して示し、狭い別窓でも右端が切れないようにする。
        self._mouse_cal_section = CollapsibleSection(
            self._mouse_frame, "感度を距離から計算"
        )
        self._mouse_cal_section.grid(
            row=4, column=0, sticky=tk.EW, pady=(px(frame, GAP_GROUP), 0)
        )
        cal_row = ttk.Frame(self._mouse_cal_section.body)
        cal_row.pack(fill=tk.X)
        ttk.Label(cal_row, text="DPI").pack(side=tk.LEFT, padx=2)
        self._mouse_dpi_var = tk.StringVar(value="800")
        self._mouse_dpi_spin = ttk.Spinbox(
            cal_row,
            from_=100,
            to=32000,
            increment=100,
            width=7,
            textvariable=self._mouse_dpi_var,
        )
        self._mouse_dpi_spin.pack(side=tk.LEFT, padx=(0, 2))
        ttk.Label(cal_row, text="360°距離").pack(side=tk.LEFT, padx=(10, 2))
        self._mouse_target_var = tk.StringVar(value="20")
        self._mouse_target_spin = ttk.Spinbox(
            cal_row,
            from_=1,
            to=100,
            increment=1,
            width=5,
            format="%.0f",
            textvariable=self._mouse_target_var,
        )
        self._mouse_target_spin.pack(side=tk.LEFT, padx=(0, 2))
        self._mouse_calc_button = ttk.Button(
            self._mouse_cal_section.body, text="計算", command=self._on_mouse_calculate
        )
        self._mouse_calc_button.pack(side=tk.LEFT, padx=2, pady=2)
        self._mouse_cal_label_var = tk.StringVar(value="")
        ttk.Label(
            self._mouse_cal_section.body, textvariable=self._mouse_cal_label_var
        ).pack(side=tk.LEFT, padx=(8, 0))
        for spin in (self._mouse_dpi_spin, self._mouse_target_spin):
            spin.bind("<FocusOut>", self._on_mouse_setting_changed, add="+")
            spin.bind("<Return>", self._on_mouse_setting_changed, add="+")
        roll_row = ttk.Frame(self._mouse_frame)
        roll_row.grid(row=2, column=0, sticky=tk.EW, pady=(2, 0))
        ttk.Label(roll_row, text="ロール").pack(side=tk.LEFT, padx=2)
        self._mouse_roll_var = tk.StringVar(value="なし")
        self._mouse_roll_combo = ttk.Combobox(
            roll_row,
            width=9,
            state="readonly",
            textvariable=self._mouse_roll_var,
            values=[label for _name, label in ROLL_LABELS],
        )
        self._mouse_roll_combo.pack(side=tk.LEFT, padx=(0, 2))
        self._mouse_roll_combo.bind(
            "<<ComboboxSelected>>", self._on_mouse_roll_changed, add=""
        )
        self._mouse_reset_button = ttk.Button(
            roll_row, text="姿勢リセット", command=self._on_mouse_reset
        )
        self._mouse_reset_button.pack(side=tk.LEFT, padx=(12, 2))
        self._mouse_advanced = CollapsibleSection(self._mouse_frame, "詳細調整")
        self._mouse_advanced.grid(row=5, column=0, sticky=tk.EW)
        advanced = self._mouse_advanced.body
        ttk.Label(advanced, text="平滑化").pack(side=tk.LEFT, padx=2, pady=2)
        self._mouse_smooth_var = tk.StringVar(value="1.0")
        self._mouse_smooth_spin = ttk.Spinbox(
            advanced,
            from_=0.05,
            to=1.0,
            increment=0.05,
            width=6,
            format="%.2f",
            textvariable=self._mouse_smooth_var,
        )
        self._mouse_smooth_spin.pack(side=tk.LEFT, padx=(0, 12), pady=2)
        self._mouse_smooth_spin.bind(
            "<FocusOut>", self._on_mouse_setting_changed, add="+"
        )
        self._mouse_smooth_spin.bind(
            "<Return>", self._on_mouse_setting_changed, add="+"
        )
        self._mouse_gravity_var = tk.BooleanVar(value=True)
        self._mouse_gravity_cb = ttk.Checkbutton(
            advanced,
            text="重力追従",
            variable=self._mouse_gravity_var,
            command=self._on_mouse_setting_changed,
        )
        self._mouse_gravity_cb.pack(side=tk.LEFT, padx=2, pady=2)
        # 入力源の直下に表示し、共通の記録欄より先に操作できるようにする。
        self._mouse_frame.grid(row=2, column=0, columnspan=3, sticky=tk.EW, pady=(4, 0))
        self._mouse_frame.grid_remove()
        self._source_cb.bind("<<ComboboxSelected>>", self._on_source_changed, add="")
        self._apply_visibility()
        self._load_settings_into_vars()
        # この部品は Window.loadSettings() より先に作られる。設定がまだ
        # 無い状態の既定値で送出開始→保存すると保存値を潰すため、
        # イベントループ開始後に読み直す。
        try:
            self.frame.after_idle(self._load_settings_into_vars)
        except Exception:
            pass
        self._poll()

    def pack(self, **kwargs: Any) -> None:
        """外枠を配置する。"""
        self.frame.pack(**kwargs)

    def _load_settings_into_vars(self) -> None:
        """設定値を画面へ反映する。読めなければ既定のまま。"""
        try:
            settings = self._settings_provider()
        except Exception:
            return
        try:
            source = str(settings.get("source", "hid"))
            if source == "hid":
                try:
                    from pokecon.domain.hidhide import hidhide_ready

                    if not hidhide_ready(force=True):
                        # HidHide未設定でHIDを開くとSteam/SDLと競合する。
                        # 起動時は安全なパッドAPIへ寄せる。
                        source = "sensor"
                except Exception:
                    source = "sensor"
            if source == "mouse":
                try:
                    from pokecon.domain.mouse_capture import mouse_capture_available

                    if not mouse_capture_available():
                        # Windows以外ではマウス入力を出さない（静かに失敗）。
                        source = "sensor"
                except Exception:
                    source = "sensor"
            for name, label in SOURCE_LABELS:
                if name == source:
                    self._source_var.set(label)
                    break
            pad = int(settings.get("pad_index", "0"))
            if 0 <= pad <= 3:
                self._pad_var.set(str(pad))
            record_path = str(settings.get("record_path", "") or "")
            if record_path:
                self._record_path_var.set(record_path)
            self._play_path_var.set(str(settings.get("play_path", "") or ""))
            self._load_invert_from_settings(settings)
            try:
                from pokecon.domain.mouse_gyro import (
                    calibration_from_settings,
                    config_from_settings,
                )

                mouse = config_from_settings(settings)
                self._mouse_sens_var.set(f"{mouse.sensitivity:g}")
                self._mouse_ratio_var.set(f"{mouse.ratio_y:g}")
                self._mouse_smooth_var.set(f"{mouse.smoothing:g}")
                self._mouse_invert_x_var.set(mouse.invert_x)
                self._mouse_invert_y_var.set(mouse.invert_y)
                self._mouse_gravity_var.set(mouse.gravity_follow)
                self._mouse_hotkey_var.set(mouse.hotkey)
                dpi, target_cm = calibration_from_settings(settings)
                self._mouse_dpi_var.set(str(dpi))
                self._mouse_target_var.set(f"{target_cm:g}")
                for name, label in ROLL_LABELS:
                    if name == mouse.roll_modifier:
                        self._mouse_roll_var.set(label)
                        break
                self._update_mouse_calibration_label()
            except Exception:
                pass
            scale = float(settings.get("gyro_scale", "1.0") or 1.0)
            self._scale_var.set(f"{scale:g}")
            for i, key in enumerate(
                ("acc_x", "acc_y", "acc_z", "manual_gx", "manual_gy", "manual_gz")
            ):
                try:
                    value = int(settings.get(key, "0"))
                except (TypeError, ValueError):
                    continue
                self._manual_vars[i].set(str(value))
                self._normalize_manual_value(i, self._manual_vars[i])
            if source in DEBUG_SOURCES:
                self._debug_var.set(True)
            self._refresh_source_choices()
            self._apply_visibility()
        except (TypeError, ValueError):
            pass

    def _make_manual_trace(
        self, index: int, var: tk.StringVar
    ) -> Callable[[str, str, str], None]:
        """手動入力欄の変更をintへ取り込むハンドラを作る（メインスレッド）。"""

        def _apply(*_args: str) -> None:
            try:
                value = int(var.get())
            except (TypeError, ValueError):
                return
            self._manual_values[index] = max(-32768, min(32767, value))

        return _apply

    def _normalize_manual_value(self, index: int, var: tk.StringVar) -> None:
        """表示中の値をint16へ丸め、送信値と画面表示を一致させる。"""
        try:
            value = int(var.get())
        except (TypeError, ValueError):
            # 数値として読めない文字列は直前の採用値へ戻す。
            value = self._manual_values[index]
        value = max(-32768, min(32767, value))
        self._manual_values[index] = value
        text = str(value)
        if var.get() != text:
            var.set(text)

    def _make_manual_normalizer(
        self, index: int, var: tk.StringVar
    ) -> Callable[[Any], None]:
        """SpinboxのFocusOut/Return用ハンドラを作る（Tkは引数1つだけ渡す）。"""

        def _normalize(_event: Any) -> None:
            self._normalize_manual_value(index, var)

        return _normalize

    def _normalize_manual_values(self) -> None:
        """手動入力の6軸すべてを丸める。送出開始の直前に呼ぶ。"""
        for i, var in enumerate(self._manual_vars):
            self._normalize_manual_value(i, var)

    def _sign_keys(self) -> tuple[str, str]:
        """選択中の入力源が読む符号キー。HID直結だけ hid_* を使う。"""
        prefix = "hid_" if self._source_name() == "hid" else ""
        return f"{prefix}accel_signs", f"{prefix}gyro_signs"

    def _signs_from_settings(
        self, settings: dict[str, str], source: str
    ) -> tuple[tuple[int, int, int], tuple[int, int, int]]:
        """その入力源が使う符号を設定から読む。読めなければ源の既定へ倒す。"""
        return imu_service.signs_from_settings(settings, source)

    def _live_trim_from_settings(
        self, settings: dict[str, str], source: str
    ) -> _LiveTrim:
        """送出中に毎サンプル読む加工値を設定から組み立てる。"""
        return imu_service.live_trim_from_settings(settings, source)

    def _load_invert_from_settings(
        self, settings: dict[str, str] | None = None
    ) -> None:
        """選択中の入力源の符号を画面へ反映する。

        プリセットに一致しなければ「設定値のまま」を選び、送出開始時に
        符号を書き換えない（既定の軸写像を黙って壊さない）。
        """
        try:
            from pokecon.domain.config import parse_imu_axis_map

            if settings is None:
                settings = self._settings_provider()
        except Exception:
            return
        acc_key, gyr_key = self._sign_keys()
        acc = parse_imu_axis_map(str(settings.get(acc_key, "")), (-1, 1))
        gyr = parse_imu_axis_map(str(settings.get(gyr_key, "")), (-1, 1))
        if acc is not None and gyr is not None:
            for name, label in INVERT_LABELS:
                if INVERT_SIGNS[name] == (acc, gyr):
                    self._invert_var.set(label)
                    return
        self._invert_var.set(CURRENT_INVERT_LABEL)

    def _on_source_changed(self, _event: Any = None) -> None:
        """入力源に応じて行を出し分ける（ラベルで説明しない）。"""
        self._apply_visibility()

    def _apply_visibility(self) -> None:
        """通常は実機の2源だけ。診断用の行はデバッグ時だけ出す。"""
        source = self._source_name()
        debug = bool(self._debug_var.get())
        if source == "sensor":
            self._pad_frame.grid()
        else:
            self._pad_frame.grid_remove()
        if source == "file":
            self._play_frame.grid()
        else:
            self._play_frame.grid_remove()
        if source == "step":
            self._btn_step.grid()
        else:
            self._btn_step.grid_remove()
        if source == "manual":
            self._manual_frame.grid()
        else:
            self._manual_frame.grid_remove()
        if source == "mouse":
            self._mouse_frame.grid()
        else:
            self._mouse_frame.grid_remove()
        # マウスの符号はマウス欄の反転で決める。デバッグの反転プリセットを
        # 重ねると二重に掛かるため、マウス入力時は出さない。
        for widget in (self._invert_label, self._invert_cb):
            if debug and source != "mouse":
                widget.grid()
            else:
                widget.grid_remove()
        for widget in (self._scale_label, self._scale_cb):
            if debug:
                widget.grid()
            else:
                widget.grid_remove()
        if not debug and source in DEBUG_SOURCES:
            # デバッグ源を選んだまま通常へ戻したら実機源へ寄せる。
            for name, label in SOURCE_LABELS:
                if name == "hid":
                    self._source_var.set(label)
                    break
            self._refresh_source_choices()
        # 入力源が変わったら、その源が読む符号を画面へ反映する。
        self._load_invert_from_settings()

    def _refresh_source_choices(self) -> None:
        """デバッグ時だけ診断用の入力源を候補に出す。"""
        debug = bool(self._debug_var.get())
        hid_ok = True
        try:
            from pokecon.domain.hidhide import hidhide_ready

            hid_ok = bool(hidhide_ready(force=True))
        except Exception:
            hid_ok = False
        mouse_ok = True
        try:
            from pokecon.domain.mouse_capture import mouse_capture_available

            mouse_ok = bool(mouse_capture_available())
        except Exception:
            mouse_ok = False
        values = [
            label
            for name, label in SOURCE_LABELS
            if (debug or name not in DEBUG_SOURCES)
            and (name != "hid" or hid_ok)
            and (name != "mouse" or mouse_ok)
        ]
        try:
            self._source_cb.config(values=values)
        except Exception:
            pass

    def _on_debug_toggled(self) -> None:
        self._refresh_source_choices()
        self._apply_visibility()

    def _manual_reader(self) -> Any:
        """手動入力の読み取り関数。送出スレッドから呼ばれる。"""
        snapshot = self._manual_values

        def _read() -> tuple[int, int, int, int, int, int]:
            return (
                snapshot[0],
                snapshot[1],
                snapshot[2],
                snapshot[3],
                snapshot[4],
                snapshot[5],
            )

        return _read

    def _invert_mode(self) -> str | None:
        """画面の反転ラベル→設定名。設定値のままならNone。"""
        label = self._invert_var.get()
        for name, text in INVERT_LABELS:
            if text == label:
                return name
        return None

    def _on_invert_changed(self, _event: Any = None) -> None:
        """反転の組み合わせを設定へ書き、送出中なら次のサンプルから効かせる。"""
        mode = self._invert_mode()
        if mode is None:
            self._queue.put(("log", "IMU反転: 設定値のまま（符号は書き換えません）"))
            return
        acc, gyr = INVERT_SIGNS[mode]
        acc_key, gyr_key = self._sign_keys()
        try:
            self._settings_writer(
                {
                    acc_key: ",".join(str(v) for v in acc),
                    gyr_key: ",".join(str(v) for v in gyr),
                }
            )
        except Exception:
            pass
        suffix = "（次回から）"
        trim = self._live_trim
        if self._streaming and trim is not None:
            self._live_trim = (trim[0], trim[1], acc, gyr, trim[4], trim[5])
            suffix = "（即時）"
        self._queue.put(("log", f"反転: {self._invert_var.get()}{suffix}"))

    def _on_scale_changed(self, _event: Any = None) -> None:
        """ジャイロ倍率を設定へ書き、送出中なら次のサンプルから効かせる。"""
        try:
            value = float(self._scale_var.get())
        except (TypeError, ValueError):
            return
        try:
            self._settings_writer({"gyro_scale": f"{value:g}"})
        except Exception:
            pass
        suffix = "（次回から）"
        trim = self._live_trim
        if self._streaming and trim is not None:
            self._live_trim = (value, trim[1], trim[2], trim[3], trim[4], trim[5])
            suffix = "（即時）"
        self._queue.put(("log", f"倍率: {value:g}{suffix}"))

    def _source_name(self) -> str:
        """画面の入力源ラベル→設定名。読めなければsensor。"""
        label = self._source_var.get()
        for name, text in SOURCE_LABELS:
            if text == label:
                return name
        return "sensor"

    def _save_manual_values(self) -> None:
        """手動入力の6軸を設定へ書く（次回起動でも同じ値を使う）。"""
        try:
            self._settings_writer(
                {
                    "acc_x": str(self._manual_values[0]),
                    "acc_y": str(self._manual_values[1]),
                    "acc_z": str(self._manual_values[2]),
                    "manual_gx": str(self._manual_values[3]),
                    "manual_gy": str(self._manual_values[4]),
                    "manual_gz": str(self._manual_values[5]),
                }
            )
        except Exception:
            pass

    def _float_var_text(
        self, var: tk.StringVar, default: float, low: float, high: float
    ) -> str:
        """画面の実数を範囲内の文字列へ直し、表示も揃える。"""
        try:
            value = float(str(var.get()).strip())
        except (TypeError, ValueError):
            value = default
        if not math.isfinite(value):
            value = default
        value = max(low, min(high, value))
        text = f"{value:g}"
        try:
            if str(var.get()) != text:
                var.set(text)
        except Exception:
            pass
        return text

    def _int_var_text(
        self, var: tk.StringVar, default: int, low: int, high: int
    ) -> str:
        """画面の整数を範囲内の文字列へ直し、表示も揃える。"""
        try:
            value = int(float(str(var.get()).strip()))
        except (TypeError, ValueError):
            value = default
        value = max(low, min(high, value))
        text = str(value)
        try:
            if str(var.get()) != text:
                var.set(text)
        except Exception:
            pass
        return text

    def _update_mouse_calibration_label(self) -> None:
        """現在の感度・DPIで何cmが360°かを表示する。"""
        try:
            sensitivity = float(
                self._float_var_text(self._mouse_sens_var, 0.05, 0.0001, 10.0)
            )
            dpi = int(self._int_var_text(self._mouse_dpi_var, 800, 100, 32000))
        except Exception:
            try:
                self._mouse_cal_label_var.set("")
            except Exception:
                pass
            return
        try:
            from pokecon.domain.mouse_gyro import cm_per_360_for

            cm = cm_per_360_for(sensitivity, dpi)
        except Exception:
            cm = None
        try:
            self._mouse_cal_label_var.set(
                "—" if cm is None else f"現在 {cm:.1f}cm/360°"
            )
        except Exception:
            pass

    def _on_mouse_calculate(self) -> None:
        """DPIと目標距離から感度を逆算して適用する。"""
        dpi = int(self._int_var_text(self._mouse_dpi_var, 800, 100, 32000))
        target_cm = float(
            self._float_var_text(self._mouse_target_var, 20.0, 1.0, 100.0)
        )
        try:
            from pokecon.domain.mouse_gyro import sensitivity_for_dpi

            sensitivity = sensitivity_for_dpi(dpi, target_cm)
        except Exception:
            return
        self._mouse_sens_var.set(f"{sensitivity:g}")
        self._on_mouse_setting_changed()
        self._queue.put(
            (
                "log",
                f"感度: {sensitivity:g} deg/count（{dpi}DPI・{target_cm:g}cm/360°）",
            )
        )

    def _mouse_roll_value(self) -> str:
        """画面のロール選択→設定名。読めなければnone。"""
        label = self._mouse_roll_var.get()
        for name, text in ROLL_LABELS:
            if text == label:
                return name
        return "none"

    def _on_mouse_roll_changed(self, _event: Any = None) -> None:
        """ロール修飾を保存し、実行中のキャプチャへも反映する。"""
        value = self._mouse_roll_value()
        try:
            self._settings_writer({"mouse_roll_modifier": value})
        except Exception:
            pass
        capture = self._mouse_capture
        if capture is not None:
            setter = getattr(capture, "set_roll_modifier", None)
            if callable(setter):
                try:
                    setter(value)
                except Exception:
                    pass
        source = self._mouse_source
        if source is not None:
            try:
                settings = dict(self._settings_provider())
                settings["mouse_roll_modifier"] = value
                from pokecon.domain.mouse_gyro import config_from_settings

                source.update_config(config_from_settings(settings))
            except Exception:
                pass

    def _on_mouse_reset(self) -> None:
        """仮想姿勢を水平・正面へ戻す（ボタン用。ホットキーも同じ）。"""
        source = self._mouse_source
        if source is None:
            self._queue.put(("log", "マウス送出中ではありません。"))
            return
        try:
            source.reset_orientation()
        except Exception:
            pass
        self._queue.put(("log", "姿勢を水平・正面にリセットしました。"))

    def _mouse_settings_payload(self) -> dict[str, str]:
        """マウス欄の値を設定へ書く形にする。軸・符号はini側で保持する。"""
        hotkey = str(self._mouse_hotkey_var.get() or "").strip() or "ctrl+alt+m"
        return {
            "mouse_sensitivity": self._float_var_text(
                self._mouse_sens_var, 0.05, 0.0001, 10.0
            ),
            "mouse_ratio_y": self._float_var_text(
                self._mouse_ratio_var, 1.0, 0.05, 10.0
            ),
            "mouse_smoothing": self._float_var_text(
                self._mouse_smooth_var, 1.0, 0.05, 1.0
            ),
            "mouse_invert_x": "1" if bool(self._mouse_invert_x_var.get()) else "0",
            "mouse_invert_y": "1" if bool(self._mouse_invert_y_var.get()) else "0",
            "mouse_gravity_follow": "1" if bool(self._mouse_gravity_var.get()) else "0",
            "mouse_dpi": self._int_var_text(self._mouse_dpi_var, 800, 100, 32000),
            "mouse_cm_per_360": self._float_var_text(
                self._mouse_target_var, 20.0, 1.0, 100.0
            ),
            "mouse_roll_modifier": self._mouse_roll_value(),
            "mouse_hotkey": hotkey,
        }

    def _on_mouse_setting_changed(self, _event: Any = None) -> None:
        """マウス欄の確定値を保存し、送出中なら次のサンプルから反映する。"""
        payload = self._mouse_settings_payload()
        try:
            self._settings_writer(payload)
        except Exception:
            pass
        self._update_mouse_calibration_label()
        if self._stream_source != "mouse":
            return
        source = self._mouse_source
        if source is None:
            return
        try:
            settings = dict(self._settings_provider())
            settings.update(payload)
            from pokecon.domain.mouse_gyro import config_from_settings

            source.update_config(config_from_settings(settings))
        except Exception:
            pass

    def _on_mouse_capture_state(self, enabled: bool, reason: str) -> None:
        """キャプチャの開始/解除を送出源と画面へ伝える（入力スレッドから）。"""
        source = self._mouse_source
        if source is not None:
            try:
                source.set_enabled(bool(enabled))
            except Exception:
                pass
        # 古いキャプチャの解除が、次の開始後に届いても無視できるよう
        # どのインスタンスの通知かを一緒に渡す。
        self._queue.put(
            ("mouse_capture", (self._mouse_capture, bool(enabled), str(reason)))
        )

    def _close_mouse_capture(self) -> None:
        """Raw Inputを止め、カーソルの閉じ込めを解除する。例外は投げない。"""
        capture, self._mouse_capture = self._mouse_capture, None
        if capture is not None:
            try:
                capture.stop()
            except Exception:
                pass
        source, self._mouse_source = self._mouse_source, None
        if source is not None:
            try:
                source.set_enabled(False)
            except Exception:
                pass
        self._mouse_interval_s = 0.0
        self._mouse_capture_on = False
        self._mouse_last_events = None
        self._mouse_last_events_t = None

    @staticmethod
    def _block_mouse_event(_event: Any) -> str:
        """キャプチャ中にTkのマウス処理へ渡さない。"""
        return "break"

    def _on_escape(self, _event: Any = None) -> str | None:
        """Escでジャイロ送出を停止する（停止ボタンと同じ）。"""
        if self._stop:
            return None
        if self._starting:
            self._stop_after_start = True
            self._queue.put(("log", "開始完了後に停止します。"))
            return "break"
        if self._stopping:
            return "break"
        if not self._streaming:
            return None
        self._on_toggle()
        return "break"

    def _release_pad_pointer_state(self) -> None:
        """キャプチャ切替時に、マウスで掴んだスティック/ボタンを解放する。

        キャプチャ中にクリックが通ると、仮想パッドのスティック掴みが
        残ってゲームパッド入力を上書きし続ける。開始・停止のどちらでも
        明示的に離す。カメラ面（旧CaptureArea）のマウススティックも対象。
        """
        try:
            pad = self._pad_provider() if self._pad_provider is not None else None
        except Exception:
            pad = None
        if pad is not None:
            release = getattr(pad, "releaseAllHeld", None)
            if callable(release):
                try:
                    release()
                except Exception:
                    pass
        try:
            preview = (
                self._preview_provider() if self._preview_provider is not None else None
            )
        except Exception:
            preview = None
        if preview is None:
            return
        for attr, method_name in (
            ("_stick_left", "mouseLeftRelease"),
            ("_stick_right", "mouseRightRelease"),
        ):
            state = getattr(preview, attr, None)
            if not bool(getattr(state, "active", False)):
                continue
            release = getattr(preview, method_name, None)
            if callable(release):
                try:
                    release()
                except Exception:
                    pass

    def _set_mouse_cursor_hidden(self, hidden: bool) -> None:
        """キャプチャ中だけカーソルを隠し、GUIクリックを止める。"""
        if hidden:
            if self._saved_cursors or self._saved_bindtags:
                return
            widgets: list[Any] = []
            try:
                widgets.append(self.frame.winfo_toplevel())
            except Exception:
                return
            index = 0
            while index < len(widgets):
                widget = widgets[index]
                index += 1
                try:
                    previous = str(widget.cget("cursor"))
                except Exception:
                    previous = ""
                try:
                    widget.configure(cursor="none")
                except Exception:
                    pass
                else:
                    self._saved_cursors.append((widget, previous))
                try:
                    original = widget.bindtags()
                    widget.bindtags((_CAPTURE_BLOCK_TAG, *original))
                except Exception:
                    pass
                else:
                    self._saved_bindtags.append((widget, original))
                try:
                    widgets.extend(widget.winfo_children())
                except Exception:
                    pass
            return
        saved_tags, self._saved_bindtags = self._saved_bindtags, []
        for widget, tags in saved_tags:
            try:
                widget.bindtags(tags)
            except Exception:
                pass
        saved, self._saved_cursors = self._saved_cursors, []
        for widget, cursor in saved:
            try:
                widget.configure(cursor=cursor)
            except Exception:
                pass

    def _on_toggle(self) -> None:
        """送出の開始/停止。線操作は作業スレッドで行う。"""
        if self._starting or self._stopping:
            self._queue.put(
                ("log", "開始/停止の処理中です。完了後にもう一度押してください。")
            )
            return
        if self._streaming:
            self._stopping = True
            try:
                self._btn_toggle.configure(text="停止中…")
            except Exception:
                pass
            # 停止ワーカーがクリップを外すまで待たず、カーソルを先に戻す。
            self._release_pad_pointer_state()
            self._set_mouse_cursor_hidden(False)
            thread = threading.Thread(target=self._stop_worker, daemon=True)
            thread.start()
            return
        source = self._source_name()
        if source == "manual":
            self._normalize_manual_values()
            self._save_manual_values()
        if source == "mouse":
            # Tkの窓ハンドルはGUIスレッドでだけ読む。クライアント領域の
            # 画面座標はRaw Input側がWin32で取る（DPIのずれを避ける）。
            try:
                top = self.frame.winfo_toplevel()
                self._owner_hwnd = int(top.winfo_id())
            except Exception:
                self._owner_hwnd = 0
            self._clip_rect = None
            # 掴んだままのスティック/ボタンを離してからロックする
            # （開始前に押していたクリックが残らないように）。
            self._release_pad_pointer_state()
            # 開始が失敗したら _poll の "stopped" で戻す。
            self._set_mouse_cursor_hidden(True)
        try:
            pad = int(self._pad_var.get())
        except (TypeError, ValueError):
            pad = 0
        try:
            scale_text = f"{float(self._scale_var.get()):g}"
        except (TypeError, ValueError):
            scale_text = "1"
        values = {
            "source": source,
            "pad_index": str(pad),
            "gyro_scale": scale_text,
            "record_path": self._record_path_var.get().strip(),
            "play_path": self._play_path_var.get().strip(),
        }
        if source == "mouse":
            values.update(self._mouse_settings_payload())
        # 作業スレッドはTk変数を読めない。ここで文字列へ写しておく。
        self._start_play_path = values["play_path"]
        self._start_record_path = values["record_path"]
        try:
            self._start_record_on = bool(self._record_var.get())
        except Exception:
            self._start_record_on = False
        mode = self._invert_mode()
        if mode is not None and source != "mouse":
            # プリセットを明示的に選んだときだけ書く。未変更のまま開始して
            # 設定ファイルの既定符号（SDLの逆写像など）を壊さない。
            acc, gyr = INVERT_SIGNS[mode]
            acc_key, gyr_key = self._sign_keys()
            values[acc_key] = ",".join(str(v) for v in acc)
            values[gyr_key] = ",".join(str(v) for v in gyr)
        try:
            self._settings_writer(values)
        except Exception:
            pass
        self._starting = True
        self._stop_after_start = False
        try:
            self._btn_toggle.configure(text="開始中…")
        except Exception:
            pass
        thread = threading.Thread(
            target=self._start_worker, args=(source, pad), daemon=True
        )
        thread.start()

    def _start_worker(self, source: str, pad: int) -> None:
        """開始ワーカー。終了時は必ずフラグを戻し、失敗なら表示も戻す。"""
        try:
            self._do_start(source, pad)
        finally:
            self._starting = False
            stop_now = self._stop_after_start
            self._stop_after_start = False
            if not self._streaming:
                self._queue.put(("state", "stopped"))
            elif stop_now and not self._stopping:
                self._stopping = True
                try:
                    self._btn_toggle.configure(text="停止中…")
                except Exception:
                    pass
                threading.Thread(target=self._stop_worker, daemon=True).start()

    def _stop_worker(self) -> None:
        """停止ワーカー。終了時は必ずフラグを戻す。"""
        try:
            self._do_stop()
        finally:
            self._stopping = False

    def _on_record_toggled(self) -> None:
        """記録の有無は次回の開始から効く（送出中の開閉はしない）。"""
        if self._streaming:
            try:
                self._record_var.set(self._recorder is not None)
            except Exception:
                pass
            self._queue.put(("log", "記録の有無は次回の送出開始から効きます。"))
        if self._record_var.get():
            self._record_path_frame.grid()
        else:
            self._record_path_frame.grid_remove()

    def _ask_file(self, save: bool) -> str:
        """記録ファイルの選択。取り消し・失敗は空文字。"""
        try:
            from tkinter import filedialog

            types = [("ジャイロ記録", "*.pgyro"), ("すべてのファイル", "*.*")]
            if save:
                return str(
                    filedialog.asksaveasfilename(
                        parent=self.frame,
                        defaultextension=".pgyro",
                        initialfile="motion.pgyro",
                        filetypes=types,
                    )
                    or ""
                )
            return str(
                filedialog.askopenfilename(parent=self.frame, filetypes=types) or ""
            )
        except Exception:
            return ""

    def _on_pick_play(self) -> None:
        """再生ファイルを選ぶ。"""
        path = self._ask_file(save=False)
        if path:
            self._play_path_var.set(path)

    def _on_pick_record(self) -> None:
        """記録先を選ぶ。拡張子が無ければ .pgyro を補う。"""
        path = self._ask_file(save=True)
        if not path:
            return
        if not os.path.splitext(path)[1]:
            path += ".pgyro"
        self._record_path_var.set(path)

    def _on_step_fire(self) -> None:
        """STEP波形の再発火。送出中でSTEP源のときだけ効く。"""
        gen = self._step_gen
        if gen is None or not self._streaming:
            self._queue.put(("log", "STEP送出中に押してください。"))
            return
        try:
            gen.trigger()
            self._queue.put(("log", "STEPを発火しました。"))
        except Exception as e:
            self._queue.put(("log", f"STEP発火に失敗: {e!r}"))

    def _on_motion_preview(self) -> None:
        """送出とは独立した3D表示を開く。"""
        preview = self._motion_preview
        if preview is not None and preview.is_alive():
            return
        if preview is not None:
            preview.close()
        preview = MotionPreview()
        try:
            preview.start()
        except Exception as exc:
            preview.close()
            self._queue.put(("log", f"3Dプレビューを開けません: {exc!r}"))
            return
        self._motion_preview = preview

    def _build_source(self, source: str, pad: int) -> Any:
        """入力源の生成器を作る。失敗は None。"""
        self._source_error = ""
        try:
            settings = self._settings_provider()
        except Exception:
            settings = {}

        def _on_mouse_created(src_obj: Any, capture: Any, interval_s: float) -> None:
            self._mouse_source = src_obj
            self._mouse_capture = capture
            self._mouse_interval_s = interval_s
            self._mouse_last_events = None
            self._mouse_last_events_t = None

        res = imu_service.build_imu_source(
            source,
            pad,
            settings,
            play_path=self._start_play_path,
            owner_hwnd=self._owner_hwnd,
            clip_rect=self._clip_rect,
            manual_reader=self._manual_reader,
            on_playback_end=lambda total: self._queue.put(("playback_end", total)),
            on_mouse_state=self._on_mouse_capture_state,
            on_log=lambda text: self._queue.put(("log", text)),
            on_mouse_created=_on_mouse_created,
        )
        if res.error:
            self._source_error = res.error
        if res.sensor_reader is not None:
            self._sensor_reader = res.sensor_reader
        if res.step_gen is not None:
            self._step_gen = res.step_gen
        if res.player is not None:
            self._player = res.player
            self._playing = res.playing
        if res.gen is None and source == "mouse" and res.mouse_capture is not None:
            self._close_mouse_capture()
        return res.gen

    def _open_recorder(self, source: str) -> bool:
        """記録先を開く。記録しない設定なら何もせず True。"""
        ok, recorder = imu_service.open_recorder_sink(
            source,
            self._start_record_on,
            self._start_record_path,
            self._start_play_path,
            lambda text: self._queue.put(("log", text)),
        )
        if recorder is not None:
            self._recorder = recorder
        return ok

    def _record_sample(self, sample: tuple[int, int, int, int, int, int]) -> None:
        """送出する1件を記録する。記録中でなければ何もしない。"""
        recorder = self._recorder
        if recorder is None:
            return
        try:
            recorder.write(sample)
        except Exception:
            pass

    def _read_preview_controls(self) -> tuple[PreviewControls, float]:
        """同じ入力源の受信済み状態を読む。ハンドルを開き直さない。"""
        reader = self._sensor_reader
        now = time.monotonic()
        if reader is not self._preview_control_reader:
            self._preview_control_reader = reader
            self._preview_controls = PreviewControls()
            self._preview_controls_at = 0.0
        if reader is None:
            return self._preview_controls, self._preview_controls_at
        if now - self._preview_controls_at >= 0.020:
            read_pad = getattr(reader, "read_pad", None)
            try:
                self._preview_controls = (
                    controls_from_pad(read_pad())
                    if callable(read_pad)
                    else PreviewControls()
                )
            except Exception:
                # ボタンの読み取り失敗は姿勢表示・ジャイロ送出を止めない。
                self._preview_controls = PreviewControls()
            self._preview_controls_at = now
        return self._preview_controls, self._preview_controls_at

    def _push_view(self, sample: tuple[int, int, int, int, int, int]) -> None:
        """可視化へ1件渡す。表示の失敗で送出は止めない。"""
        preview = getattr(self, "_motion_preview", None)
        if preview is not None:
            try:
                controls, controls_at = self._read_preview_controls()
                preview.push(sample, controls, controls_at)
            except Exception as exc:
                self._motion_preview = None
                self._queue.put(("preview_error", (preview, exc)))

    @staticmethod
    def _sample_transform(
        settings: dict[str, str],
        hid: bool = False,
        apply_signs: bool = True,
    ) -> Any | None:
        """IMU 軸・符号の変換関数を作る。恒等なら None。"""
        return imu_service.sample_transform(settings, hid=hid, apply_signs=apply_signs)

    @staticmethod
    def _apply_axis_settings(
        reader: Any,
        settings: dict[str, str],
        apply_signs: bool = True,
    ) -> None:
        """軸変換・バイアス設定を reader へ適用する。読めない項目は飛ばす。"""
        imu_service.apply_axis_settings(reader, settings, apply_signs=apply_signs)

    @staticmethod
    def _hid_failure_message() -> str:
        """HID 直結を開始できなかった理由。IMU 未有効と未接続を区別する。"""
        return imu_service.hid_failure_message()

    def _do_start(self, source: str, pad: int) -> None:
        """送出開始の実体（作業スレッド）。"""
        try:
            transport = self._transport_provider()
        except Exception:
            transport = None
        if transport is None:
            self._queue.put(("log", "シリアルが開いていません。先に接続してください。"))
            return
        try:
            opened = bool(transport.is_open())
        except Exception:
            opened = False
        if not opened:
            self._queue.put(("log", "シリアルが開いていません。先に接続してください。"))
            return
        gen = self._build_source(source, pad)
        if gen is None:
            if source == "sensor":
                self._queue.put(
                    (
                        "log",
                        f"センサーが開けません: pad={pad}。対応パッドを繋いでください。",
                    )
                )
            elif source == "hid":
                self._queue.put(("log", self._hid_failure_message()))
            elif source == "file":
                self._queue.put(
                    ("log", self._source_error or "記録を再生できませんでした。")
                )
            elif source == "mouse":
                self._queue.put(
                    ("log", self._source_error or "マウス入力を開始できませんでした。")
                )
            else:
                self._queue.put(
                    ("log", f"波形の生成に失敗: {source}。設定を確認してください。")
                )
            return
        # 再生は記録済みの送出値をそのまま返す（符号・倍率を二重に掛けない）。
        if source != "file":
            self._playing = False
        if not self._open_recorder(source):
            self._close_source()
            return
        # 可視化へ送出値を流す。窓の開閉に追従するため都度引き直す。
        inner = gen
        panel = self
        try:
            settings_now = self._settings_provider()
        except Exception:
            settings_now = {}
        # 加工値は開始時の設定で固定せず、不変タプルを毎サンプル読む。
        # GUIスレッドの反転・感度変更は次のサンプルから効く。
        if source == "file":
            # 再生は記録済みの送出値をそのまま返す（下のwatchedを参照）。
            self._live_trim = None
        else:
            self._live_trim = self._live_trim_from_settings(settings_now, source)

        def watched() -> Any:
            sample = inner()
            if sample is None:
                return None
            try:
                ax, ay, az, gx, gy, gz = (int(v) for v in sample)
            except (TypeError, ValueError):
                return None
            if panel._playing:
                # 記録は送出値をそのまま持つ。符号・倍率・不感帯を再適用
                # すると二重に加工され、記録した動きからずれる。
                out = (ax, ay, az, gx, gy, gz)
                panel._record_sample(out)
                panel._push_view(out)
                return out
            ax, ay, az, gx, gy, gz = imu_service.trim_and_scale_sample(
                (ax, ay, az, gx, gy, gz), panel._live_trim
            )

            peak_g = max(abs(gx), abs(gy), abs(gz)) * 0.06103
            peak_a = max(abs(ax), abs(ay), abs(az)) * 0.000244
            if peak_g > panel._peak_g:
                panel._peak_g = peak_g
            if peak_a > panel._peak_a:
                panel._peak_a = peak_a
            # 軸ごとの最小/最大。定値源なら0、FWのtimeout復帰や
            # 読みの取りこぼしで跳べば幅として見える。
            for i, v in enumerate((ax, ay, az)):
                lo = panel._acc_min[i]
                hi = panel._acc_max[i]
                panel._acc_min[i] = v if lo is None or v < lo else lo
                panel._acc_max[i] = v if hi is None or v > hi else hi
            view_sample = (ax, ay, az, gx, gy, gz)
            panel._push_view(view_sample)
            # 実際に送出する値を記録する（再生で同じ値が戻る）。
            panel._record_sample(view_sample)
            # 加工後の値を送出する（不感帯を実際に効かせる）。
            return view_sample

        if self._stop:
            # close() が先に走った場合は線を掴まない。
            self._close_source()
            return
        gen = watched
        interval_s: float | None = None
        if source == "file" and self._player is not None:
            # 記録の周期で送る（v1は200Hz固定。ヘッダー値を尊重する）。
            try:
                interval_s = float(self._player.period_us) / 1_000_000.0
            except Exception:
                interval_s = None
        elif source == "mouse" and self._mouse_interval_s > 0:
            # FWの0x30と同じ5ms。角度の積分はこの周期で行う。
            interval_s = self._mouse_interval_s
        # 手動の開始は、マクロのジャイロ再生より利用者の意思を優先する。
        # replace=True で引き継ぎ、停止は owner="panel" のときだけ行う
        # （マクロの後始末がパネルの送出を巻き込まない）。
        previous_owner = ""
        get_owner = getattr(transport, "imu_loop_owner", None)
        if callable(get_owner):
            try:
                previous_owner = str(get_owner())
            except Exception:
                previous_owner = ""
        try:
            start = getattr(transport, "start_imu_loop", None)
            if not callable(start):
                ok = False
            elif interval_s is not None:
                ok = bool(start(gen, interval_s, replace=True, owner="panel"))
            else:
                ok = bool(start(gen, replace=True, owner="panel"))
        except Exception:
            ok = False
        if ok and previous_owner and previous_owner != "panel":
            self._queue.put(("log", "他のジャイロ送出を引き継ぎました。"))
        if not ok:
            self._queue.put(("log", "ジャイロ送信を開始できませんでした。"))
            self._close_source()
            return
        self._streaming = True
        self._stream_source = source
        if source == "mouse" and self._mouse_capture is not None:
            capture = self._mouse_capture
            try:
                capture.set_enabled(True, "start")
            except Exception:
                pass
            source_obj = self._mouse_source
            hotkey = "ctrl+alt+m"
            if source_obj is not None:
                try:
                    hotkey = source_obj.config.hotkey
                except Exception:
                    pass
            if capture.enabled():
                self._queue.put(
                    (
                        "log",
                        f"マウス入力を開始しました（{hotkey} で解除 / Escで停止）。",
                    )
                )
                reset_hotkey = ""
                if source_obj is not None:
                    try:
                        reset_hotkey = source_obj.config.reset_hotkey
                    except Exception:
                        reset_hotkey = ""
                if reset_hotkey:
                    self._queue.put(("log", f"姿勢リセット: {reset_hotkey}"))
            else:
                self._queue.put(
                    (
                        "log",
                        "マウス入力は待機中です。PokeConを前面にして"
                        f"{hotkey} を押すと再開します。",
                    )
                )
        self._queue.put(("state", "streaming"))
        self._queue.put(("log", "ジャイロ送信を開始しました。"))

    def _do_stop(self) -> None:
        """送出停止の実体（作業スレッド）。"""
        try:
            transport = self._transport_provider()
        except Exception:
            transport = None
        if transport is not None:
            try:
                stop = getattr(transport, "stop_imu_loop", None)
                if callable(stop):
                    stopped = stop(owner="panel")
                    if stopped is False:
                        self._queue.put(
                            (
                                "log",
                                "他のジャイロ送出が動いているため、"
                                "パネルからは停止していません。",
                            )
                        )
            except Exception:
                pass
        self._close_source()
        self._streaming = False
        self._stream_source = ""
        self._queue.put(("state", "stopped"))
        self._queue.put(("log", "ジャイロ送信を停止しました。"))

    def _close_source(self) -> None:
        """入力源と記録器を閉じる。例外は投げない。

        HID直結はパッド読みと共有しているため、ここでは閉じない
        （閉じるとボタン入力が止まる）。プロセス終了時に任せる。
        """
        self._close_mouse_capture()
        self._close_recorder()
        self._playing = False
        self._player = None
        reader, self._sensor_reader = self._sensor_reader, None
        self._preview_control_reader = None
        self._preview_controls = PreviewControls()
        self._preview_controls_at = 0.0
        self._step_gen = None
        imu_service.close_sensor_reader(reader)

    def _close_recorder(self) -> None:
        """記録を閉じ、件数を1行出す。例外は投げない。"""
        recorder, self._recorder = self._recorder, None
        if recorder is None:
            return
        try:
            recorder.close()
        except Exception:
            pass
        try:
            count = int(recorder.count)
            path = self._start_record_path
            self._queue.put(("log", f"記録しました: {path} {count}件"))
        except Exception:
            pass

    def _poll(self) -> None:
        """作業スレッドからの届けを画面へ出す。"""
        if self._stop:
            return
        try:
            preview = self._motion_preview
            if preview is not None and not preview.is_alive():
                self._motion_preview = None
                error = preview.last_error
                preview.close()
                if error:
                    self._log(f"3Dプレビューを停止しました: {error}")
            while True:
                try:
                    kind, payload = self._queue.get_nowait()
                except queue.Empty:
                    break
                if kind == "log":
                    try:
                        self._log(str(payload))
                    except Exception:
                        pass
                elif kind == "preview_error":
                    preview, exc = payload
                    preview.close()
                    self._log(f"3Dプレビューを停止しました: {exc!r}")
                elif kind == "mouse_capture":
                    capture, enabled, reason = payload
                    if capture is not self._mouse_capture:
                        # 停止済み・作り直し前のインスタンスの通知。
                        continue
                    was_on = self._mouse_capture_on
                    self._mouse_capture_on = bool(enabled)
                    self._release_pad_pointer_state()
                    self._set_mouse_cursor_hidden(bool(enabled))
                    if not enabled and was_on and reason == "focus":
                        self._log(
                            "マウス入力を解除しました（PokeConが前面にありません）。"
                        )
                    elif reason == "hotkey":
                        if enabled:
                            self._log("マウス入力を再開しました。")
                        else:
                            self._log("マウス入力を解除しました（ホットキー）。")
                elif kind == "state":
                    try:
                        if payload == "streaming":
                            self._btn_toggle.configure(text="停止")
                        else:
                            self._btn_toggle.configure(text="開始")
                            # 開始失敗でカーソルを隠したままにしない。
                            # 古い停止通知が新しい開始後に届いても、
                            # 送出中なら触らない。
                            if not self._streaming and not self._starting:
                                self._set_mouse_cursor_hidden(False)
                    except Exception:
                        pass
                elif kind == "playback_end":
                    # 記録を送り切ったら自分で止める。最後の値を送り続けると
                    # コンソール側で静止姿勢のまま入力が残る。
                    try:
                        self._log(f"再生が終了しました（{payload}件）。")
                    except Exception:
                        pass
                    if self._streaming and not self._stopping:
                        self._stopping = True
                        try:
                            self._btn_toggle.configure(text="停止中…")
                        except Exception:
                            pass
                        threading.Thread(target=self._stop_worker, daemon=True).start()
        except Exception:
            pass
        try:
            self._refresh_stats()
        except Exception:
            pass
        try:
            # マウスキャプチャ中は解除の反映を待たせない。通常は500ms。
            delay = 100 if self._stream_source == "mouse" or self._starting else 500
            self.frame.after(delay, self._poll)
        except Exception:
            pass

    def _refresh_stats(self) -> None:
        """統計表示を更新する。送出中のみ読む。"""
        if not self._streaming:
            self._peak_g = 0.0
            self._peak_a = 0.0
            try:
                self._stats_var.set("停止中")
            except Exception:
                pass
            return
        peak_g = self._peak_g
        peak_a = self._peak_a
        self._peak_g = 0.0
        self._peak_a = 0.0
        spans = []
        for i in range(3):
            lo = self._acc_min[i]
            hi = self._acc_max[i]
            spans.append(0.0 if lo is None or hi is None else (hi - lo) * 0.000244)
        self._acc_min = [None, None, None]
        self._acc_max = [None, None, None]
        span_txt = f"ΔA=({spans[0]:.2f},{spans[1]:.2f},{spans[2]:.2f})G"
        rec_txt = ""
        recorder = self._recorder
        if recorder is not None:
            try:
                rec_txt = f" 記録={recorder.count}件"
            except Exception:
                rec_txt = ""
        if self._playing and self._player is not None:
            try:
                rec_txt += f" 再生 全{len(self._player)}件"
            except Exception:
                pass
        owner_note = ""
        try:
            transport = self._transport_provider()
            stats = getattr(transport, "imu_stats", None)
            info = stats() if callable(stats) else None
            get_owner = getattr(transport, "imu_loop_owner", None)
            if callable(get_owner):
                owner = str(get_owner())
                if owner and owner != "panel":
                    owner_note = f"（{owner} が送出中）"
        except Exception:
            info = None
        mouse_txt = ""
        if self._stream_source == "mouse":
            mouse_txt = f" マウス={'ON' if self._mouse_capture_on else 'OFF'}"
            if self._mouse_capture_on:
                mouse_txt += "(Esc停止)"
            source_obj = self._mouse_source
            if source_obj is not None:
                try:
                    mouse_stats = source_obj.stats()
                    dropped = float(mouse_stats.get("dropped_deg", 0.0))
                    if dropped >= 0.05:
                        mouse_txt += f" 捨て={dropped:.1f}°"
                    events = float(mouse_stats.get("events", 0.0))
                    now = time.monotonic()
                    if (
                        self._mouse_last_events is not None
                        and self._mouse_last_events_t is not None
                    ):
                        span = now - self._mouse_last_events_t
                        if span > 0:
                            mouse_txt += (
                                f" 入力="
                                f"{(events - self._mouse_last_events) / span:.0f}/s"
                            )
                    self._mouse_last_events = events
                    self._mouse_last_events_t = now
                except Exception:
                    pass
        try:
            if isinstance(info, dict):
                sent_now = int(info.get("sent", 0))
                now = time.monotonic()
                rate_txt = ""
                if self._last_sent is not None and self._last_sent_t is not None:
                    span = now - self._last_sent_t
                    if span > 0:
                        rate_txt = f" rate={(sent_now - self._last_sent) / span:.0f}/s"
                self._last_sent = sent_now
                self._last_sent_t = now
                self._stats_var.set(
                    f"送出中 sent={info.get('sent', 0)}"
                    f" dropped={info.get('dropped', 0)}{rate_txt}"
                    f" peak|g|={peak_g:.1f}dps peak|a|={peak_a:.2f}G {span_txt}"
                    f"{mouse_txt}{rec_txt}{owner_note}"
                )
            else:
                self._stats_var.set(
                    f"送出中 peak|g|={peak_g:.1f}dps peak|a|={peak_a:.2f}G "
                    f"{span_txt}{mouse_txt}{rec_txt}{owner_note}"
                )
        except Exception:
            pass

    def close(self) -> None:
        """送出を止めて予約を消す。例外は投げない。"""
        self._stop = True
        preview, self._motion_preview = self._motion_preview, None
        if preview is not None:
            preview.close()
        # 開始/停止ワーカーが走っている間に別の停止を重ねない。
        deadline = time.monotonic() + 1.0
        while (self._starting or self._stopping) and time.monotonic() < deadline:
            time.sleep(0.01)
        if self._streaming and not self._stopping:
            self._stopping = True
            thread = threading.Thread(target=self._stop_worker, daemon=True)
            thread.start()
            try:
                thread.join(1.0)
            except Exception:
                pass
        elif not self._streaming:
            self._close_source()
        # キャプチャが残っていても、ここで必ず止めてカーソルを戻す。
        try:
            self._close_mouse_capture()
        except Exception:
            pass
        self._release_pad_pointer_state()
        self._set_mouse_cursor_hidden(False)
        for target, funcid in self._esc_bindings:
            try:
                target.unbind("<Escape>", funcid)
            except Exception:
                pass
        self._esc_bindings = []
