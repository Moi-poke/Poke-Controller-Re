#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""controller_panel.py - 操作タブ（手動入力・操作盤・パッド追従）の UI Mixin."""

from __future__ import annotations

import threading
import time
import tkinter as tk
import tkinter.ttk as ttk
from typing import TYPE_CHECKING, Any

from loguru import logger
from pokecon.domain.gamepad_map import DEADZONE_MAX
from pokecon.domain.pad_layout import AUTO, LAYOUTS, LAYOUT_LABELS, resolve_layout
from pokecon.ui.collapsible import SectionFrame
from pokecon.ui.controller_dock import ControllerDock
from pokecon.ui.spacing import GAP_GROUP, GAP_ITEM, TAB_PAD_X, TAB_PAD_Y, px
from pokecon.ui.tooltip import Tooltip

#: 設定タブの最低の高さ（px）。
_TAB_MIN_HEIGHT = 280

#: 文字を打つ部品の Tk クラス名。ここにフォーカスがある間は Switch へ送らない。
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


class ControllerPanelMixin:
    """SerialPanelMixin から分離したコントローラ・手動入力枠の UI 制御。"""

    if TYPE_CHECKING:
        root: Any
        tab_controller: Any
        settings: Any
        serial: Any
        preview: Any
        camera_lf: Any
        status_input: Any
        runner: Any
        tabs_scroll: Any
        _closing: bool
        _kb_window_active: Any
        _gamepad_active: Any

        @staticmethod
        def _set_wrap(label: Any, wrap: int) -> None: ...
        def _on_setting_changed(self) -> None: ...
        def applyArbitration(self, event: Any = None) -> None: ...

    def _build_control_frame(self) -> None:
        self.control_lf = ttk.Frame(
            self.tab_controller,
            padding=(px(self.root, TAB_PAD_X), px(self.root, TAB_PAD_Y)),
        )
        self.control_lf.columnconfigure(0, weight=1)
        self.control_lf.place(x=5, y=5)
        self.control_lf.bind("<Configure>", self._fit_control_viewport, add="+")

        self.is_use_keyboard = tk.BooleanVar()
        self.cb_use_keyboard = ttk.Checkbutton(self.control_lf)
        self.cb_use_keyboard.config(
            text="キーボードで操作",
            variable=self.is_use_keyboard,
            command=self._on_keyboard_toggled,
        )
        self.cb_use_keyboard.grid(row=0, column=0, sticky="w")
        Tooltip(self.cb_use_keyboard, "キーボードのキーを Switch のボタンとして送る")
        self.key_config_button = ttk.Button(
            self.control_lf, text="キー割当", command=self._open_key_config
        )
        self.key_config_button.grid(row=0, column=1, sticky="e", padx=(6, 0))

        self.is_use_gamepad = tk.BooleanVar()
        self.cb_use_gamepad = ttk.Checkbutton(self.control_lf)
        self.cb_use_gamepad.config(
            text="ゲームパッドで操作",
            variable=self.is_use_gamepad,
            command=self._on_gamepad_toggled,
        )
        self.cb_use_gamepad.grid(row=2, column=0, sticky="w")
        Tooltip(self.cb_use_gamepad, "接続したゲームパッドの入力を Switch へ送る")
        self._hidhide_gamepad_ok = self._gamepad_enabled_by_hidhide()

        self.gamepad_row = ttk.Frame(self.control_lf)
        self.gamepad_row.grid(row=4, column=0, columnspan=2, sticky="ew")
        self.gamepad_row.columnconfigure(1, weight=1)
        self.gamepad_label = ttk.Label(self.gamepad_row, text="パッド")
        self.gamepad_label.grid(row=0, column=0, sticky="w", padx=(0, 4))
        self.gamepad_choice = tk.StringVar(value="")
        self.gamepad_cb = ttk.Combobox(
            self.gamepad_row, textvariable=self.gamepad_choice, width=24
        )
        self.gamepad_cb.grid(row=0, column=1, sticky="ew")
        self.gamepad_cb.bind("<<ComboboxSelected>>", self._on_gamepad_selected, add="")
        self.gamepad_reconnect_btn = ttk.Button(
            self.gamepad_row,
            text="再接続",
            command=self._on_gamepad_reconnect,
        )
        self.gamepad_status = tk.StringVar(value="")
        self.gamepad_status_label = ttk.Label(
            self.gamepad_row, textvariable=self.gamepad_status, width=8, anchor="e"
        )
        self.gamepad_status_label.grid(row=0, column=2, padx=(8, 0), sticky="e")
        self.gamepad_reconnect_btn.grid(row=0, column=3, padx=(4, 0), sticky="e")
        Tooltip(
            self.gamepad_reconnect_btn,
            "ゲームパッドを探し直す（自動で拾えないとき）",
        )
        Tooltip(
            self.gamepad_status_label,
            "ゲームパッドから入力が読めているかの状態",
        )

        self.hidhide_row = ttk.Frame(self.control_lf)
        self.hidhide_row.grid(row=2, column=1, sticky="e")
        self.hidhide_reason = ttk.Label(self.hidhide_row, text="HidHide 未設定")
        self.hidhide_reason.pack(side="left")
        self.hidhide_setup_btn = ttk.Button(
            self.hidhide_row,
            text="セットアップ",
            command=self._open_hidhide_setup,
        )
        self.hidhide_setup_btn.pack(side="left", padx=(4, 0))
        Tooltip(
            self.hidhide_reason,
            "HidHide が未設定のため、ゲームパッド入力を Switch へ送れません。",
        )
        self._apply_hidhide_gamepad_gate()

        self.mouse_section = SectionFrame(self.control_lf, "マウス操作")
        self.mouse_section.grid(
            row=8,
            column=0,
            columnspan=2,
            sticky="ew",
            pady=(px(self.control_lf, GAP_GROUP), 0),
        )
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

        self.pad_section = SectionFrame(self.control_lf, "ゲームパッドの調整")
        self.pad_section.grid(
            row=9,
            column=0,
            columnspan=2,
            sticky="ew",
            pady=(px(self.control_lf, GAP_GROUP), 0),
        )
        self.gamepad_dz_label = ttk.Label(
            self.pad_section.body, text="スティックの遊び"
        )
        self.gamepad_dz_label.grid(row=0, column=0, sticky="w")
        self.gamepad_dz = tk.StringVar(value="1638")
        self.gamepad_dz_spin = ttk.Spinbox(
            self.pad_section.body,
            textvariable=self.gamepad_dz,
            from_=0,
            to=DEADZONE_MAX,
            increment=100,
            width=8,
        )
        self.gamepad_dz_spin.grid(row=0, column=1, sticky="w", padx=(8, 0))
        self.gamepad_dz_pct = ttk.Label(self.pad_section.body, text="5.0%")
        self.gamepad_dz_pct.grid(row=0, column=2, sticky="w", padx=(8, 0))
        Tooltip(
            self.gamepad_dz_spin,
            "スティックの小さな傾きを無視する範囲。生の軸値 0〜16383、既定 1638 = 5%。勝手に動くときに上げる",
        )
        Tooltip(
            self.gamepad_dz_pct,
            "0〜32767 の軸に対する割合。100% にするとスティックが反応しないため 50% まで",
        )
        self.gamepad_dz_spin.bind("<FocusOut>", self._on_gamepad_dz_changed, add="")
        self.gamepad_dz_spin.bind("<Return>", self._on_gamepad_dz_changed, add="")

        # 単一コンボだけの折りたたみ（1項目折りたたみ）を廃し、常時見せる SectionFrame にする。
        self.arbitration_section = SectionFrame(
            self.control_lf,
            "コマンド実行中の手動入力",
        )
        self.arbitration_section.grid(
            row=10,
            column=0,
            columnspan=2,
            sticky="ew",
            pady=(px(self.control_lf, GAP_GROUP), 0),
        )
        self.arbitration_label = ttk.Label(
            self.arbitration_section.body, text="入力調停"
        )
        self.arbitration_label.grid(row=0, column=0, sticky="w")
        self.arbitration_cb = ttk.Combobox(self.arbitration_section.body)
        self.arbitration_cb.config(state="readonly", width=24)
        self.arbitration_cb.grid(
            row=1, column=0, sticky="ew", pady=(px(self.control_lf, GAP_ITEM), 0)
        )
        self.arbitration_cb.bind("<<ComboboxSelected>>", self.applyArbitration, add="")
        Tooltip(
            self.arbitration_cb,
            "コマンド実行中に手動操作を受け付けるかどうか",
        )
        self.arbitration_hint = ttk.Label(
            self.arbitration_section.body, wraplength=300, justify="left"
        )
        self.arbitration_hint.grid(
            row=2, column=0, sticky="ew", pady=(px(self.control_lf, GAP_ITEM), 0)
        )

        self.gyro_row = ttk.Frame(self.control_lf)
        self.gyro_row.columnconfigure(1, weight=1)
        ttk.Label(self.gyro_row, text="ジャイロ").grid(row=0, column=0, sticky="w")
        self.gyro_state = tk.StringVar(value="停止中")
        ttk.Label(self.gyro_row, textvariable=self.gyro_state).grid(
            row=0, column=1, sticky="w", padx=(8, 0)
        )
        self.gyro_button = ttk.Button(
            self.gyro_row, text="開始", command=self._toggle_gyro
        )
        self.gyro_button.grid(row=0, column=2, sticky="e")
        Tooltip(self.gyro_button, "ジャイロ送出の開始/停止（操作盤の下と同じ）")

        self._gamepad_after: Any = None
        self._gamepad_ids: dict[int, str] = {}
        self._gamepad_names: dict[int, str] = {}
        self._gamepad_identity = ""
        self._gamepad_name = ""
        self._gamepad_refresh_busy = False
        self._gamepad_refresh_thread: threading.Thread | None = None
        self._gamepad_restart_busy = False
        self._gamepad_restart_thread: threading.Thread | None = None
        self._gamepad_restart_pending: int | None = None
        self._gamepad_reconnect_busy = False
        self._gamepad_reconnect_thread: threading.Thread | None = None
        self._gamepad_skip_auto_restart = False
        self._update_controller_status()
        self.refreshGamepads()
        self._poll_gamepads()

        def _make_imu_panel(master: Any) -> Any:
            from pokecon.ui.ImuPanel import ImuPanel

            imu = ImuPanel(
                master,
                lambda: getattr(self.serial.sender, "transport", None),
                self._read_imu_settings,
                self._write_imu_settings,
                lambda text: print(text),
                pad_provider=self._controller_pad_for_imu,
                preview_provider=lambda: getattr(self, "preview", None),
            )
            self._imu_panel = imu
            try:
                imu._stats_var.trace_add("write", self._sync_gyro_shortcut)
                self._sync_gyro_shortcut()
            except Exception:
                pass
            return imu

        self.layout_label = ttk.Label(self.control_lf, text="操作盤")
        self.layout_label.grid(row=6, column=0, sticky="w", pady=(6, 0))
        self.controller_layout_choice = tk.StringVar(value=LAYOUT_LABELS[AUTO])
        self._controller_layout_labels = {LAYOUT_LABELS[key]: key for key in LAYOUTS}
        self.layout_cb = ttk.Combobox(
            self.control_lf,
            textvariable=self.controller_layout_choice,
            state="readonly",
            width=18,
        )
        self.layout_cb["values"] = [LAYOUT_LABELS[key] for key in LAYOUTS]
        self.layout_cb.grid(row=6, column=1, sticky="e", pady=(6, 0))
        self.layout_cb.bind(
            "<<ComboboxSelected>>", self._on_controller_layout_selected, add=""
        )
        Tooltip(
            self.layout_cb,
            "接続したパッドに合わせる（自動）、または操作盤をプロコン / "
            "Joy-Con (L) / Joy-Con (R) に固定する",
        )

        dock_area = ttk.Frame(self.control_lf)
        self._dock_area = dock_area
        dock_area.grid(row=7, column=0, columnspan=2, sticky="ew", pady=(6, 0))
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
        self._apply_controller_layout()

    # -- 操作盤の種類 --------------------------------------------------------

    def _selected_controller_layout(self) -> str:
        """画面で選んでいる種類（内部キー）。読めなければ auto。"""
        try:
            raw = str(self.controller_layout_choice.get()).strip()
        except Exception:
            return AUTO
        return self._controller_layout_labels.get(raw, AUTO)

    def _on_controller_layout_selected(self, event: Any = None) -> None:
        """操作盤の種類を選び直した。設定へ保存し、すぐ描き直す。"""
        self._set_controller_layout_choice(self._selected_controller_layout())

    def _set_controller_layout_choice(self, mode: str) -> None:
        """種類を選び直す（画面・設定・操作盤を一度に合わせる）。"""
        if mode not in LAYOUT_LABELS:
            mode = AUTO
        try:
            self.controller_layout_choice.set(LAYOUT_LABELS[mode])
        except Exception:
            pass
        try:
            self.settings.controller_layout.set(mode)
        except Exception as e:
            logger.debug(f"操作盤の種類を設定へ書けませんでした: {e}")
        self._apply_controller_layout()
        try:
            self._on_setting_changed()
        except Exception:
            pass

    def _load_controller_layout_setting(self) -> None:
        """設定ファイルの値を選択欄へ流し、操作盤へ反映する。"""
        try:
            mode = str(self.settings.controller_layout.get())
        except Exception:
            mode = AUTO
        if mode not in LAYOUT_LABELS:
            mode = AUTO
        try:
            self.controller_layout_choice.set(LAYOUT_LABELS[mode])
        except Exception:
            pass
        self._apply_controller_layout()

    def _save_controller_layout_setting(self) -> None:
        """選択欄の値を設定オブジェクトへ戻す（Window._save_settings から）。"""
        try:
            self.settings.controller_layout.set(self._selected_controller_layout())
        except Exception as e:
            logger.debug(f"操作盤の種類を保存できませんでした: {e}")

    def _apply_controller_layout(self) -> None:
        """選択値と接続中のパッド名から、実際に描く種類を決めて渡す。"""
        dock = getattr(self, "controller_dock", None)
        if dock is None:
            return
        names = getattr(self, "_gamepad_names", {})
        try:
            index = self._gamepad_index()
        except Exception:
            index = 0
        pad_name = ""
        try:
            pad_name = str(names.get(index, "") or "")
        except Exception:
            pad_name = ""
        resolved = resolve_layout(self._selected_controller_layout(), pad_name)
        try:
            dock.set_layout(resolved)
        except Exception as e:
            logger.warning(f"操作盤の種類を切り替えられませんでした: {e}")

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
        """見えている幅・高さへ、操作欄と操作盤を収める。"""
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
            tab_height = max(frame.winfo_reqheight() + 10, _TAB_MIN_HEIGHT)
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

    def _controller_pad_for_imu(self) -> Any:
        """いま表示している仮想パッド（別窓が優先）。"""
        dock = getattr(self, "controller_dock", None)
        if dock is None:
            return None
        return getattr(dock, "floating", None) or getattr(dock, "embedded", None)

    def _toggle_gyro(self) -> None:
        """常時見える入口から、操作盤の下のジャイロ欄を開始/停止する。"""
        imu = getattr(self, "_imu_panel", None)
        if imu is None:
            return
        try:
            imu._on_toggle()
        except Exception as e:
            logger.warning(f"ジャイロの開始/停止に失敗しました: {e}")

    def _sync_gyro_shortcut(self, *_args: Any) -> None:
        """ジャイロ欄の状態を、常時見える入口の表示へ写す。"""
        imu = getattr(self, "_imu_panel", None)
        if imu is None:
            return
        try:
            state = str(imu._stats_var.get())
            self.gyro_state.set(state)
            self.gyro_button.config(text="停止" if "送出中" in state else "開始")
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

    @classmethod
    def _walk_widgets(cls, widget: Any) -> list[Any]:
        found: list[Any] = []
        try:
            children = widget.winfo_children()
        except Exception:
            return found
        for child in children:
            found.append(child)
            found.extend(cls._walk_widgets(child))
        return found

    def _release_focus_later(self, target: Any) -> None:
        """クリックした操作部品からフォーカスを外し、タブへ戻す。"""

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
                widget.bind(
                    "<<ComboboxSelected>>",
                    lambda _e, t=target: self._release_focus_later(t),
                    add="+",
                )

    def _update_controller_status(self) -> None:
        """手動入力の受付状態を、取得できる情報だけで更新する。"""
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
        """「ゲームパッドで操作」を選べる状態か。"""
        try:
            from pokecon.domain.hidhide import hidhide_ready

            return bool(hidhide_ready())
        except Exception:
            return False

    def _apply_hidhide_gamepad_gate(self) -> None:
        """HidHide 未設定ならゲームパッド操作のチェックを無効にする。"""
        ok = bool(getattr(self, "_hidhide_gamepad_ok", False))
        self.cb_use_gamepad.config(state="normal" if ok else "disabled")
        gamepad_cb = getattr(self, "gamepad_cb", None)
        if gamepad_cb is not None:
            gamepad_cb.config(state="readonly" if ok else "disabled")
        reconnect = getattr(self, "gamepad_reconnect_btn", None)
        if reconnect is not None:
            reconnect.config(state="normal" if ok else "disabled")
        self._sync_hidhide_setup_button()
        if ok:
            return
        if not self.is_use_gamepad.get():
            return
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
        status = getattr(self, "gamepad_status_label", None)
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
                    reason.pack_forget()
                else:
                    anchor = button if button is not None else None
                    reason.pack(side="left", before=anchor)
            if status is not None:
                status.grid()
                if not ok:
                    self.gamepad_status.set("")
                elif not str(self.gamepad_status.get()):
                    self.gamepad_status.set("未接続")
        except tk.TclError:
            pass

    def _after_hidhide_setup(self) -> bool:
        """HidHide 設定の直後に、起動時の判定をやり直して使い始める。"""
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
            from pokecon.ui.hidhide_setup import open_hidhide_setup

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

    # -- キーボード操作 --------------------------------------------------------

    def activateKeyboard(self) -> None:
        """キーボード操作の有効・無効を切り替える。"""
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
        """フォーカス追従を結線し、現在の状態へ即時合わせる。"""
        self.root.bind("<FocusIn>", self.onFocusInController)
        self.root.bind("<FocusOut>", self.onFocusOutController)
        self._syncKeyboardFocus(stop_hook=False)

    def _unbindKeyboardFocus(self) -> None:
        """フォーカス追従を外し、門を下ろす。"""
        self.root.unbind("<FocusIn>")
        self.root.unbind("<FocusOut>")
        self._kb_window_active.clear()

    def onFocusInController(self, event: Any) -> None:
        """窓内へフォーカスが戻ったら打鍵を受け付ける。"""
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
        """窓外へ出たら打鍵を止める。"""
        try:
            self.root.after(100, self._syncKeyboardFocus)
        except Exception:
            self._kb_window_active.clear()
            self._stopKeyboardHook()

    def _syncKeyboardFocus(self, *, stop_hook: bool = True) -> None:
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
            if stop_hook:
                self._stopKeyboardHook()

    def _stopKeyboardHook(self) -> None:
        """窓外なので OS 全体のキーフックを外す（設定は変えない）。"""
        try:
            enabled = bool(self.is_use_keyboard.get())
        except Exception:
            enabled = False
        if not enabled:
            return
        if self.serial.keyboard is None:
            return
        self.serial.stop_keyboard()

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
        """Use Keyboard の切り替え。有効化に失敗した場合も保存する。"""
        self.activateKeyboard()
        self._update_controller_status()
        self._on_setting_changed()

    # -- ゲームパッド操作 ------------------------------------------------------

    def activateGamepad(self) -> None:
        """ゲームパッド操作の有効・無効を切り替える。"""
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
        """ゲームパッド入力の鏡を結線する。Tk への受け渡しまで持つ。"""
        try:
            self.serial.set_gamepad_display(self._onGamepadDisplay)
        except Exception as e:
            logger.warning(f"ゲームパッド表示の結線で例外: {e}")

    def _onGamepadDisplay(self, snapshot: Any) -> None:
        """読取スレッドからの通知。Tk スレッドへ予約して描く。"""
        try:
            from pokecon.domain.ui_inbox import post_latest_to_gui

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

    #: 状態行を書き換える最小間隔。
    GAMEPAD_STATUS_INTERVAL_S = 0.1

    def _setGamepadStatus(self, data: dict[str, Any]) -> None:
        """状態行に名前・経路・生軸値を出す（Tk スレッド専用）。"""
        try:
            status = getattr(self, "gamepad_status", None)
            if status is None:
                return
            if not bool(getattr(self, "_hidhide_gamepad_ok", False)):
                if str(status.get()) != "":
                    status.set("")
                return
            if not data.get("connected"):
                if getattr(self, "_gamepad_status_text", None) != "none":
                    self._gamepad_status_text = "none"
                    status.set("")
                return
            now = time.monotonic()
            last = getattr(self, "_gamepad_status_at", 0.0)
            if now - last < self.GAMEPAD_STATUS_INTERVAL_S:
                return
            self._gamepad_status_at = now
            text = "接続中"
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
        return value if 0 <= value <= DEADZONE_MAX else 1638

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
        value = max(0, min(DEADZONE_MAX, value))
        try:
            self.gamepad_dz.set(str(value))
            self._set_gamepad_dz_pct(value)
            self.settings.gamepad_deadzone.set(value)
        except Exception:
            pass
        if self.is_use_gamepad.get():
            try:
                gamepad = self.serial.gamepad
                if gamepad is not None:
                    gamepad.deadzone = max(0, min(DEADZONE_MAX, int(value)))
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
        """繋がっているパッド一覧を取り直して選択肢へ入れる。"""
        if getattr(self, "_gamepad_refresh_busy", False):
            return
        if getattr(self, "_gamepad_reconnect_busy", False):
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
            from pokecon.domain.hidhide import hidhide_ready
            from pokecon.domain.pad_source import PadSource, sdl_debug_enabled

            source = PadSource(allow_sdl=sdl_debug_enabled(), allow_hid=hidhide_ready)
            names = source.names()
            try:
                identities = source.identities()
            except Exception:
                identities = {}
        except Exception as e:
            logger.warning(f"パッド一覧の列挙で例外: {e}")
            names = {}
            identities = {}
        try:
            from pokecon.domain.ui_inbox import post_latest_to_gui

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
        """列挙結果を一覧へ反映する（GUIスレッド専用）。"""
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
            found = [i for i in range(4) if names.get(i)]
            if not found:
                labels = ["パッドなし"]
                self._gamepad_map = {"パッドなし": current}
                self.gamepad_cb["values"] = labels
                self.gamepad_choice.set("パッドなし")
                self.gamepad_status.set("")
                try:
                    self.gamepad_reconnect_btn.config(text="再検索")
                except Exception:
                    pass
            else:
                labels = [f"{i}: {names.get(i, '(未接続)')}" for i in range(4)]
                self._gamepad_map = {label: i for i, label in enumerate(labels)}
                self.gamepad_cb["values"] = labels
                try:
                    self.gamepad_reconnect_btn.config(text="再接続")
                except Exception:
                    pass
                for label, index in self._gamepad_map.items():
                    if index == current:
                        self.gamepad_choice.set(label)
                        break
            self._refresh_gamepad_deadzone_field()
            apply_layout = getattr(self, "_apply_controller_layout", None)
            if callable(apply_layout):
                apply_layout()
            return current
        except Exception as e:
            logger.warning(f"パッド一覧の更新で例外: {e}")
            return self._gamepad_index()

    def _follow_gamepad(
        self, current: int, names: dict[int, str] | None = None
    ) -> int | None:
        """同じ個体が別の番号へ移っていれば、その番号を返す。"""
        table = {} if names is None else names
        identities = getattr(self, "_gamepad_ids", {})
        ident = getattr(self, "_gamepad_identity", "")
        if ident:
            for index, value in identities.items():
                if value == ident:
                    key = int(index)
                    self._gamepad_name = table.get(key, self._gamepad_name)
                    return key
        name = getattr(self, "_gamepad_name", "")
        if name:
            same = [int(i) for i, value in table.items() if value == name]
            if len(same) == 1:
                key = same[0]
                self._gamepad_identity = identities.get(key, "")
                return key
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
        """手動の再接続。SDL/HID のキャッシュを捨てて列挙し直す。"""
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
            from pokecon.domain.hidhide import hidhide_ready
            from pokecon.domain.pad_source import PadSource, sdl_debug_enabled

            invalidated_live = False
            live = getattr(self.serial, "invalidate_gamepad", None)
            if callable(live):
                try:
                    invalidated_live = bool(live())
                except Exception as e:
                    logger.debug(f"読取器のキャッシュ破棄で例外: {e}")
            source = PadSource(allow_sdl=sdl_debug_enabled(), allow_hid=hidhide_ready)
            if not invalidated_live:
                source.invalidate()
            names = source.names()
            identities = source.identities()
        except Exception as e:
            logger.warning(f"パッドの再接続で例外: {e}")
            names, identities = {}, {}
            err = str(e) or "パッドを列挙できません"
        try:
            from pokecon.domain.ui_inbox import post_to_gui

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
            self._gamepad_skip_auto_restart = True
            try:
                current = self._apply_gamepad_listing(names, identities)
            finally:
                self._gamepad_skip_auto_restart = False
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
        if getattr(self, "_hidhide_gamepad_ok", True) is False:
            return
        try:
            self.gamepad_reconnect_btn.state(["!disabled"])
        except Exception:
            pass

    def _restart_gamepad_at(self, index: int) -> None:
        """番号が移った個体を掴み直す。有効化中だけ実体を作り直す。"""
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
            from pokecon.domain.ui_inbox import post_to_gui

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
        """遊び欄を設定値へ戻す。ただし入力中は上書きしない。"""
        spin = getattr(self, "gamepad_dz_spin", None)
        try:
            if spin is not None and spin.focus_get() is spin:
                return
        except Exception:
            pass
        value = self._gamepad_deadzone()
        try:
            if str(self.gamepad_dz.get()).strip() != str(value):
                self.gamepad_dz.set(str(value))
            self._set_gamepad_dz_pct(value)
        except Exception:
            pass

    def _set_gamepad_dz_pct(self, value: int) -> None:
        """遊びの割合を表示する（生の軸 32767 に対する %）。"""
        label = getattr(self, "gamepad_dz_pct", None)
        if label is None:
            return
        try:
            label.config(text=f"{value / 32767 * 100:.1f}%")
        except (tk.TclError, AttributeError):
            pass

    def _poll_gamepads(self) -> None:
        """抜き差しで一覧と状態行を追従させる画面側巡回（2秒毎）。"""
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
        """選択肢で選んだパッドへ切り替える。途中でも切り替える。"""
        try:
            raw = str(self.gamepad_choice.get()).strip()
            index = self._gamepad_map.get(raw, None)
            if index is None:
                index = int(raw.split(":")[0].strip())
        except Exception:
            index = 0
        try:
            index = max(0, min(3, int(index)))
            self.settings.gamepad_index.set(index)
        except Exception:
            index = 0
        try:
            self._gamepad_identity = getattr(self, "_gamepad_ids", {}).get(index, "")
            self._gamepad_name = getattr(self, "_gamepad_names", {}).get(index, "")
        except Exception:
            self._gamepad_identity = ""
            self._gamepad_name = ""
        apply_layout = getattr(self, "_apply_controller_layout", None)
        if callable(apply_layout):
            apply_layout()
        if self.is_use_gamepad.get():
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
