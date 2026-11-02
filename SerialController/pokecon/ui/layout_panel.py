#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""layout_panel.py - ウィンドウレイアウトの適用と状態バッジの配信。

PokeControllerApp に混ぜて使う（多重継承）。複数台を 1 画面に並べるため、
「どの部品を出すか」を設定 1 つで切り替える。どれを出すかはここで考えず、
core.display_mode.layout_plan が返した表（LayoutPlan）だけを見る。状態を
見分ける分岐が増えるたびに Window 側が太くなるため、判断は core 側 1 箇所に
閉じる。

プロファイルの色（帯・色チップ）と実行状態の 1 行（コンパクトバーと
プレビューのバッジ）もここから配る。状態が変わる経路は Window._update_title
が全部通るので、そこで 1 回だけ配る。

プレビューの要求サイズと伸び方は camera_panel 側の
_apply_preview_layout が持つ（実体は CaptureArea を知る必要があるため）。
このファイルは「いつ・どのレイアウトで呼ぶか」だけを決める。

Window 本体は import しない（循環になる）。
"""

from __future__ import annotations

import time
import tkinter as tk
import tkinter.ttk as ttk
from typing import Any

from pokecon.domain.display_mode import LAYOUTS, layout_plan, normalize_profile_color
from pokecon.domain.pane_arrangement import arrangement_tree, normalize_arrangement
from pokecon.domain.status_view import BADGE_COLORS, status_view
from pokecon.ui.transport_names import display_name as transport_display_name

# 下端の状態バーに常時表示できる項目（キー, メニュー名）。
# 表示(V) メニューで選び、settings.ini の Window.status_items に保存する。
STATUS_ITEMS: tuple[tuple[str, str], ...] = (
    ("state", "実行状態"),
    ("target", "送信先"),
    ("input", "手動入力"),
    ("bcon", "Pico↔Switch"),
    ("camera", "カメラ"),
    ("fps", "表示 fps"),
)
_DEFAULT_STATUS_ITEMS = "state,target,input,bcon,camera"
# STATUS flags の bit5=有線（Switch-bcon の spec/protocol_v3.md）。
_STATUS_FLAG_WIRED = 0x20

# コンパクトバーの色チップの幅（px）。色つきなら一目で分かる細帯。
_COMPACT_CHIP_WIDTH = 12
# 色チップの左右の余白。
_CHIP_PAD_X = (4, 2)


class LayoutPanelMixin:
    """レイアウト適用Mixin。単体では使わない。"""

    root: Any
    frame_1: Any
    camera_lf: Any
    preview: Any
    profile: str
    serial: Any
    runner: Any
    # Window 側が _init_state / _apply_settings_to_widgets で用意する
    layout_mode: Any
    arrangement: Any
    show_tabs_pane: Any
    show_log_pane: Any
    _pane_arranger: Any
    profile_color: Any
    startButton: Any
    pauseButton: Any
    stopPlay: Any
    openCommandPalette: Any
    show_mode: Any
    show_size: Any
    _running_command: str
    _paused: bool
    _on_setting_changed: Any
    _apply_content_minsize: Any
    _apply_preview_layout: Any

    # 組み立て後に出来上がる部品。まだ無い間に _publish_status が来ても
    # 落ちないよう、宣言だけして参照は getattr で避ける。
    profile_band: Any
    compact_bar: Any
    # コンパクト/プレビューのみで隠したカメラ欄の操作部（標準へ戻すときに使う）
    _hidden_camera_controls: list[Any]
    compact_chip: Any
    compact_profile_label: Any
    compact_status: Any
    _compact_status_label: Any
    compact_start: Any
    transport_name: Any
    status_bar: Any
    status_state: Any
    status_device: Any
    status_input: Any
    status_bcon: Any
    status_item_vars: dict[str, Any]
    settings: Any
    status_fps: Any
    status_camera: Any
    compact_pause: Any
    compact_stop: Any
    tabs_scroll: Any
    log_area: Any
    Command_nb: Any
    py_cb: Any
    mcu_cb: Any

    # ------------------------------------------------------------------
    # 組み立て
    # ------------------------------------------------------------------

    def _build_layout_widgets(self) -> None:
        """レイアウト切替の対象になる部品を 2 つ作る。

        _build_ui の最後で呼ぶ。作時点では preview も startButton も揃って
        いるが、適用（_apply_layout）は preview 確定後に Window 側が呼ぶ。
        ここではどちらも配置しない（標準レイアウトのまま見せる）。
        """
        # プロファイル色の帯。色がある時だけ root の上端へ乗せる。
        self.profile_band = tk.Frame(self.root, height=6)
        self.profile_band.pack_propagate(False)

        # コンパクトバー。1 行で「誰の・今どこ・操作」だけを示す。
        self.compact_bar = ttk.Frame(getattr(self, "preview_area", self.camera_lf))

        # 左から: 色チップ / プロファイル名 / 状態 / 操作。
        self.compact_chip = tk.Frame(self.compact_bar, width=_COMPACT_CHIP_WIDTH)
        self.compact_chip.pack_propagate(False)
        self.compact_chip.pack(side="left", fill="y", padx=_CHIP_PAD_X)

        self.compact_profile_label = ttk.Label(self.compact_bar, text="")
        self.compact_profile_label.pack(side="left")

        self.compact_status = tk.StringVar(value="")
        self._compact_status_label = ttk.Label(
            self.compact_bar, textvariable=self.compact_status
        )
        self._compact_status_label.pack(side="left", padx=(6, 0))

        # Start / Pause は本体と同じ関数を invoke する。状態を二重に持たず、
        # 入口だけ並べて 1 画面で完結させる。
        # 右から詰めるので、左→右の見た目は
        # [選択...] [開始] [停止] [一時停止] になる。
        self.compact_pause = ttk.Button(self.compact_bar, text="一時停止")
        self.compact_pause.config(command=lambda: self.pauseButton.invoke())
        self.compact_pause.pack(side="right", padx=(2, 0))
        self.compact_stop = ttk.Button(self.compact_bar, text="停止")
        self.compact_stop.config(command=lambda: self.stopPlay())
        self.compact_stop.pack(side="right", padx=(2, 0))
        self.compact_start = ttk.Button(self.compact_bar, text="開始")
        self.compact_start.config(command=lambda: self.startButton.invoke())
        self.compact_start.pack(side="right", padx=(2, 0))
        ttk.Button(
            self.compact_bar, text="選択...", command=self.openCommandPalette
        ).pack(side="right", padx=2)

        # 標準レイアウトの下端の 1 行。状態・送信先・手動入力・表示 fps を
        # どのタブを開いていても見せる（タブの中だけの状態にしない）。
        # 出し入れは _apply_layout（root 直下に pack する）。
        self.status_bar = ttk.Frame(self.root, padding=(6, 1))
        self.status_state = tk.StringVar(value="")
        self.status_device = tk.StringVar(value="送信先: 未接続")
        self.status_input = tk.StringVar(value="手動入力: 停止（入力なし）")
        self.status_bcon = tk.StringVar(value="Pico↔Switch: 不明")
        if getattr(self, "status_camera", None) is None:
            self.status_camera = tk.StringVar(value="")
        self.status_fps = tk.StringVar(value="")
        self._status_vars = {
            "state": self.status_state,
            "target": self.status_device,
            "input": self.status_input,
            "bcon": self.status_bcon,
            "camera": self.status_camera,
            "fps": self.status_fps,
        }
        default_items = set(_DEFAULT_STATUS_ITEMS.split(","))
        self.status_item_vars = {
            key: tk.BooleanVar(value=key in default_items) for key, _ in STATUS_ITEMS
        }
        self._status_labels: dict[str, Any] = {}
        self._status_seps: dict[str, Any] = {}
        self._status_hidden: set[str] = set()
        for key, _label in STATUS_ITEMS:
            self._status_labels[key] = ttk.Label(
                self.status_bar, textvariable=self._status_vars[key]
            )
            self._status_seps[key] = ttk.Separator(self.status_bar, orient="vertical")
        self._status_items_loaded = False
        self._repack_status_items()

        try:
            self.root.bind("<Configure>", self._on_root_configure, add="+")
        except tk.TclError:
            pass

    def _dpi_factor(self) -> float:
        """DPI倍率。端数で境界が揺れないよう、整数DPIから求める。"""
        try:
            return max(1.0, round(float(self.root.winfo_fpixels("1i"))) / 96.0)
        except (tk.TclError, ValueError):
            return 1.0

    def _logical_width(self) -> float:
        width = float(self.root.winfo_width())
        factor = self._dpi_factor()
        return width / factor if factor > 0 else 0.0

    def _tabs_right_fallback_needed(self) -> bool:
        """狭いときの tabs_right フォールバックを、幅の往復で暴れないよう決める。

        800 論理px 未満で畳み、840 論理px 以上で戻す（40px のヒステリシス）。
        """
        if str(self.arrangement.get()) != "tabs_right":
            self._tabs_right_fallback = False
            return False
        width = self._logical_width()
        if width <= 0:
            return bool(getattr(self, "_tabs_right_fallback", False))
        state = getattr(self, "_tabs_right_fallback", None)
        if state is None:
            state = width < 800
        elif state and width >= 840:
            state = False
        elif not state and width < 800:
            state = True
        self._tabs_right_fallback = state
        return bool(state)

    def _on_root_configure(self, event: Any = None) -> None:
        if event is not None and getattr(event, "widget", None) is not self.root:
            return
        if str(self.arrangement.get()) != "tabs_right":
            return
        state = self._tabs_right_fallback_needed()
        if state != getattr(self, "_auto_layout_state", None):
            self._auto_layout_state = state
            self.root.after_idle(self._apply_layout)

    def _repack_status_items(self) -> None:
        """選ばれている項目だけを、状態バーの左から順に並べ直す。"""
        for widget in self._status_labels.values():
            widget.pack_forget()
        for sep in self._status_seps.values():
            sep.pack_forget()
        first = True
        for key, _label in STATUS_ITEMS:
            try:
                if not self.status_item_vars[key].get():
                    continue
            except Exception:
                continue
            if key in self._status_hidden:
                continue
            if not first:
                try:
                    self._status_seps[key].pack(side="left", fill="y", padx=8)
                except Exception:
                    pass
            try:
                self._status_labels[key].pack(side="left")
            except Exception:
                pass
            first = False

    def _sync_status_items(self) -> None:
        """設定（Window.status_items）から表示項目を一度だけ反映する。"""
        if getattr(self, "_status_items_loaded", False):
            return
        settings = getattr(self, "settings", None)
        text = ""
        try:
            text = str(settings.status_items.get()) if settings is not None else ""
        except Exception:
            text = ""
        if not text:
            text = _DEFAULT_STATUS_ITEMS
        enabled = {part.strip() for part in text.split(",") if part.strip()}
        for key, _label in STATUS_ITEMS:
            try:
                self.status_item_vars[key].set(key in enabled)
            except Exception:
                pass
        self._status_items_loaded = True
        self._repack_status_items()

    def applyStatusItem(self, key: str) -> None:
        """状態バーの表示項目を切り替えて保存する（表示(V) メニュー）。"""
        if key not in self.status_item_vars:
            return
        enabled = [
            name for name, _label in STATUS_ITEMS if self.status_item_vars[name].get()
        ]
        try:
            self.settings.status_items.set(",".join(enabled))
        except Exception:
            pass
        self._repack_status_items()
        self._on_setting_changed()

    # ------------------------------------------------------------------
    # レイアウト適用
    # ------------------------------------------------------------------

    def applyLayout(self, layout: str) -> None:
        """レイアウトを選び、画面へ適用して保存する。

        値は検証してから変数に入れる。未知の値がそのまま入ると、
        次回起動時に画面と設定が食い違うため。
        """
        self.layout_mode.set(layout if layout in LAYOUTS else LAYOUTS[0])
        self._apply_layout()
        # 要求サイズが変わるので最小サイズの制限も測り直す（タブの見切れ防止）。
        self._apply_content_minsize()
        self._on_setting_changed()

    def _apply_layout(self) -> None:
        """現在のレイアウトに合わせて、部品の出し入れとプレビューを直す。"""
        self._sync_status_items()
        plan = self._layout_plan()

        # カメラ欄の操作部（見出し・入力・コンボ・ボタン）。
        # プレビューとコンパクトバーは残すので、grid_slaves から取り除く。
        # grid_remove は配置オプションを覚えているため、戻す時は grid() だけで
        # 元の位置と sticky へ戻る。再配置のコードを書き足さずに済む。
        if plan.show_camera_controls:
            for widget in getattr(self, "_hidden_camera_controls", []):
                widget.grid()
            self._hidden_camera_controls = []
        else:
            # grid_remove した部品は grid_slaves に出てこなくなるので、戻す
            # ときのために隠した物を覚えておく（覚えないと標準へ戻せない）。
            hidden = list(getattr(self, "_hidden_camera_controls", []))
            for widget in self._camera_control_widgets():
                widget.grid_remove()
                hidden.append(widget)
            self._hidden_camera_controls = hidden
        # 隠すときは枠の見出しも落とす（プレビューだけの枠にする）。
        self.camera_lf.config(text="カメラ" if plan.show_camera_controls else "")

        # 下端の状態の 1 行。本体より先に pack して、窓が小さいときに本体より
        # 先に詰められないようにする。
        if plan.show_status_bar:
            self.status_bar.pack(side="bottom", fill="x", before=self.frame_1)
        else:
            self.status_bar.pack_forget()

        # コンパクトバー。tabs_right では標準でも実行バーとして出す。
        show_exec_bar = bool(plan.show_compact_bar) or (
            plan.show_tabs and str(self.arrangement.get()) == "tabs_right"
        )
        self._exec_bar_minimal = show_exec_bar and not plan.show_compact_bar
        if show_exec_bar:
            self.compact_bar.grid(column=0, row=1, sticky="ew")
        else:
            self.compact_bar.grid_remove()

        # プレビューの要求サイズと伸び方。実体は camera_panel 側。
        self._apply_preview_layout()

        # 3 つの欄（プレビュー・設定タブ・ログ）を仕切りに並べ直す。出すかどうかは
        # レイアウト（コンパクト等は隠す）と利用者の折りたたみの両方で決まる。
        # 伸びるプレビューにだけ余白を多めに配る（core.pane_arrangement）。
        requested = str(self.arrangement.get())
        effective = requested
        auto_hide_log = False
        if requested == "tabs_right" and self._tabs_right_fallback_needed():
            # 右列の最小幅を割る。設定は変えず、この場だけ
            # log_right + ログ非表示へ落とす。
            effective = "log_right"
            auto_hide_log = True
        self._effective_arrangement = effective
        set_pin_tab = getattr(self, "setLogPinTabMode", None)
        if callable(set_pin_tab):
            set_pin_tab(effective == "tabs_right")
        # 右列の下限は論理 415px（125% で 519px）。パッドの縮小率 0.9 を
        # 割らない幅。中身の要求がこれを超えるなら、そちらを優先する。
        if effective == "tabs_right":
            factor = self._dpi_factor()
            self.tabs_scroll.configure(width=round(415 * max(factor, 1.0)))
        else:
            self.tabs_scroll.configure(width=0)
        tree = arrangement_tree(
            effective,
            show_tabs=plan.show_tabs and bool(self.show_tabs_pane.get()),
            show_log=plan.show_log
            and bool(self.show_log_pane.get())
            and not auto_hide_log,
            stretch=bool(plan.preview.stretch),
        )
        self._pane_arranger.apply(tree)
        if effective == "tabs_right":
            # 右列は論理 415px の固定幅。仕切りは割合で持つため、今の幅から
            # 目標幅を引いた位置を入れる。左列の上下は、映像の 16:9 と
            # ログ最小高から _apply_tabs_right_split が決める。
            try:
                total_w = int(self.frame_1.winfo_width())
            except tk.TclError:
                total_w = 0
            factor = self._dpi_factor()
            target = round(415 * max(factor, 1.0)) + 10
            if total_w > target * 2:
                ratio = (total_w - target) / total_w
                self._pane_arranger.sash_ratios["H[V[preview,log],tabs]"] = [ratio]
            self._pane_arranger._restore_sashes()
            self.root.after_idle(self._apply_tabs_right_split)
            self.root.after(120, self._apply_tabs_right_split)

        # 色も設定値なので、ここでまとめて反映する。起動時の 1 回目も
        # _apply_layout を通るため、色反映の経路が 1 つで済む。
        self._apply_profile_color_widgets(str(self.profile_color.get()))

        # 実行状態の 1 行（コンパクトバー）とプレビューのバッジ
        self._publish_status()

    def applyArrangement(self, arrangement: str) -> None:
        """欄の並べ方を選び、画面へ適用して保存する。"""
        self.arrangement.set(normalize_arrangement(arrangement))
        self._relayout_and_save()

    def setPaneVisible(self, name: str, visible: bool) -> None:
        """設定タブ（tabs）かログ（log）を出す/畳む。保存もする。"""
        var = {"tabs": self.show_tabs_pane, "log": self.show_log_pane}.get(name)
        if var is None:
            return
        var.set(bool(visible))
        self._relayout_and_save()

    def resetPaneSashes(self) -> None:
        """仕切りを既定の位置へ戻して保存する。"""
        self._pane_arranger.reset_sashes()
        self._on_setting_changed()

    def _relayout_and_save(self) -> None:
        self._apply_layout()
        self._apply_content_minsize()
        self._on_setting_changed()

    def _apply_tabs_right_split(self) -> None:
        """左列の上下を、映像の 16:9 とログ最小高から決める。

        映像が高さで頭打ちになるときは、余りをログへ回さず、ログを最小高に
        する（上段を映像に必要な高さで止める）。手で仕切りを動かした後は
        その位置を優先する。
        """
        if str(getattr(self, "_effective_arrangement", "")) != "tabs_right":
            return
        if getattr(self, "_tabs_right_log_locked", False):
            return
        pane = self._pane_arranger.pane_for("V[preview,log]")
        if pane is None or len(pane.panes()) < 2:
            return
        if not self.log_area.winfo_ismapped():
            return
        total = int(pane.winfo_height())
        if total <= 1:
            return
        if not getattr(self, "_tabs_right_sash_bound", False):
            try:
                pane.bind("<ButtonRelease-1>", self._on_tabs_right_sash_drag, add="+")
                self._tabs_right_sash_bound = True
            except tk.TclError:
                pass
        chrome = max(0, self.camera_lf.winfo_height() - self.preview.winfo_height())
        bar = (
            self.compact_bar.winfo_height() if self.compact_bar.winfo_ismapped() else 0
        )
        factor = self._dpi_factor()
        log_min = round(
            (250 if getattr(self, "_tabs_right_pin_open", False) else 148)
            * max(factor, 1.0)
        )
        width = max(1, self.preview.winfo_width())
        video_h = min(width * 9 / 16, total - chrome - bar - log_min)
        video_h = max(80, video_h)
        desired_log = max(log_min, total - (chrome + video_h + bar))
        # 上段＝左列の残り。sashpos は上段の高さ。
        pane.sashpos(0, max(1, total - desired_log))

    def _on_tabs_right_sash_drag(self, _event: Any = None) -> None:
        self._tabs_right_log_locked = True

    def _on_log_pin_visibility(self, opened: bool) -> None:
        if str(getattr(self, "_effective_arrangement", "")) != "tabs_right":
            return
        self._tabs_right_pin_open = bool(opened)
        self.root.after_idle(self._apply_tabs_right_split)
        self.root.after(100, self._apply_tabs_right_split)

    def _layout_plan(self) -> Any:
        """今のレイアウト・表示モード・固定サイズから LayoutPlan を取る。"""
        return layout_plan(
            str(self.layout_mode.get()),
            str(self.show_mode.get()),
            str(self.show_size.get()),
        )

    def _camera_control_widgets(self) -> list[Any]:
        """カメラ欄の grid の子のうち、操作部だけを取り出す。

        プレビューとコンパクトバーは「操作部」ではないので残す。
        """
        keep = {id(getattr(self, "preview", None)), id(self.compact_bar)}
        return [w for w in self.camera_lf.grid_slaves() if id(w) not in keep]

    # ------------------------------------------------------------------
    # 状態表示の配信
    # ------------------------------------------------------------------

    def _publish_status(self) -> None:
        """実行状態の 1 行を、コンパクトバーとプレビューのバッジへ配る。

        Window._update_title の最後から呼ばれる（状態が変わる経路は全部そこを
        通るため）。UI 構築前に来ても落ちないよう、未作成の部品は getattr で
        避ける。
        """
        connection = self.serial.connection_snapshot()
        device = connection.port_name
        state = {
            "running_command": str(getattr(self, "_running_command", "")),
            "paused": bool(getattr(self, "_paused", False)),
            "stop_waited_ms": int(getattr(self.runner, "stop_waited", 0)),
            "connection": connection,
        }
        profile = str(getattr(self, "profile", ""))
        # バッジは単独で見えるので [名前] を付ける。コンパクトバーは隣の
        # ラベルが名前を出しているため、付けると二重になる。
        view = status_view(profile=profile, **state)
        bar_view = status_view(profile="", **state)

        # 下端の 1 行。名前はウィンドウタイトルにあるので付けない。
        if getattr(self, "status_state", None) is not None:
            self.status_state.set(bar_view.text)
            transport = connection.transport_name
            if connection.phase == "failed":
                self.status_device.set("送信先: 接続失敗")
            elif connection.phase == "opening":
                self.status_device.set("送信先: 接続処理中")
            elif device and connection.opened:
                name = transport_display_name(transport) if transport else "不明"
                self.status_device.set(f"送信先: {device} / {name}")
            else:
                self.status_device.set("送信先: 未接続")
            if getattr(self, "status_bcon", None) is not None:
                if not connection.opened or connection.status is None:
                    self.status_bcon.set("Pico↔Switch: 不明")
                else:
                    flags = int(connection.status[0])
                    mode = "有線" if flags & _STATUS_FLAG_WIRED else "無線"
                    stale = (
                        connection.status_received_at is not None
                        and time.monotonic() - connection.status_received_at > 3.0
                    )
                    self.status_bcon.set(
                        f"Pico↔Switch: {mode}" + ("（更新停止）" if stale else "")
                    )

            # 関係のない項目は幅を取らない。設定のチェックは変えず、
            # このセッションの表示だけを切り替える。
            hidden: set[str] = set()
            transport = connection.transport_name
            if not transport:
                transport_var = getattr(self, "transport_name", None)
                getter = getattr(transport_var, "get", None)
                try:
                    transport = str(getter()) if callable(getter) else ""
                except tk.TclError:
                    transport = ""
            uses_bcon = getattr(self.serial, "uses_bcon", None)
            if transport and callable(uses_bcon):
                if not uses_bcon(transport):
                    hidden.add("bcon")
            if not connection.opened or connection.phase != "ready":
                hidden.add("input")
            fps_var = getattr(self, "status_fps", None)
            if fps_var is not None and not str(fps_var.get()).strip():
                hidden.add("fps")
            if hidden != getattr(self, "_status_hidden", None):
                self._status_hidden = hidden
                repack = getattr(self, "_repack_status_items", None)
                if callable(repack):
                    repack()

        # コンパクトバーがあれば 1 行と操作ボタンの中身を写す。
        if getattr(self, "compact_status", None) is not None:
            # 実行バーは実行の状態だけを出す。接続は下端の状態バーが持つ。
            if bar_view.kind in {"running", "paused", "stopping"}:
                exec_text = bar_view.text
            else:
                name = ""
                try:
                    nb = self.Command_nb
                    idx = int(nb.index(nb.select()))
                    combo = self.py_cb if idx == 0 else self.mcu_cb
                    getter = getattr(self, "_selectedName", None)
                    if callable(getter):
                        name = str(getter(combo) or "")
                except (AttributeError, tk.TclError, TypeError, ValueError):
                    name = ""
                exec_text = "待機中" + (f" · {name}" if name else "")
            self.compact_status.set(exec_text)
            self._mirror_button(self.compact_pause, self.pauseButton)
            running = str(self.startButton["text"]) == "停止"
            self.compact_start.configure(
                text="開始",
                state="disabled" if running else str(self.startButton["state"]),
            )
            self.compact_stop.configure(state="normal" if running else "disabled")

        # プロファイル名のラベル。無名なら隠す（空欄のラベルが幅を取る）。
        profile_label = getattr(self, "compact_profile_label", None)
        if profile_label is not None:
            profile_label.config(text=profile)
            if profile and not getattr(self, "_exec_bar_minimal", False):
                # 隠した後に pack し直すと左詰めの最後へ回るので、状態文より
                # 前（色チップの直後）へ明示して戻す。
                profile_label.pack(side="left", before=self._compact_status_label)
            else:
                profile_label.pack_forget()

        # プレビューのバッジ。出すのはバッジのあるレイアウトだけ。
        preview = getattr(self, "preview", None)
        if preview is None:
            return
        if self._layout_plan().show_badge:
            preview.setBadge(view.text, BADGE_COLORS[view.kind])
        else:
            preview.clearBadge()

    def publishVideoStats(self, shown_fps: float) -> None:
        """表示 fps の実測を下端の 1 行へ出す（log_panel の集計から呼ぶ）。"""
        if getattr(self, "status_fps", None) is not None:
            self.status_fps.set(f"表示 {shown_fps:.1f} fps")
            hidden = getattr(self, "_status_hidden", None)
            if hidden is not None and "fps" in hidden:
                hidden.discard("fps")
                self._repack_status_items()

    @staticmethod
    def _mirror_button(target: Any, source: Any) -> None:
        """コンパクトバーのボタンへ、本体側の文字と有効/無効を写す。

        状態を二重に持たないため、読むのは本体側だけ。写すのは text と state
        の 2 つ（command は本体を invoke するので写さない）。
        """
        if target is None or source is None:
            return
        for option in ("text", "state"):
            try:
                target[option] = source[option]
            except Exception:
                # 破棄済みなら黙る。状態表示の失敗で本体を止めない。
                pass

    # ------------------------------------------------------------------
    # プロファイルの色
    # ------------------------------------------------------------------

    def applyProfileColor(self, color: str) -> None:
        """プロファイルの色を検証して帯と色チップへ反映し、保存する。

        色は複数台を並べたときの識別子。窓が重なっても「どの台か」が、
        ウィンドウタイトルを見られない環境でも分かる。
        """
        value = normalize_profile_color(color)
        self.profile_color.set(value)
        self._apply_profile_color_widgets(value)
        self._on_setting_changed()

    def _apply_profile_color_widgets(self, color: str) -> None:
        """色つきの帯と色チップを更新する（空なら出さない）。

        applyProfileColor と _apply_layout の両方から呼ぶ。起動時の 1 回目も
        _apply_layout を通るため、色反映の経路が 1 つで済む。
        """
        band = getattr(self, "profile_band", None)
        if band is not None:
            if color:
                band.config(background=color)
                # 本体は pack 済みなので、順序を指定しないと帯が下に来る。
                band.pack(side="top", fill="x", before=self.frame_1)
            else:
                band.pack_forget()

        chip = getattr(self, "compact_chip", None)
        if chip is not None:
            if color and not getattr(self, "_exec_bar_minimal", False):
                chip.config(background=color)
                # pack し直すと左詰めの最後（状態文の後ろ）へ回るため、
                # バーの先頭へ明示して戻す。無名プロファイルでは名前ラベルが
                # pack されておらず基準にできないので、状態文を基準にする。
                label = self.compact_profile_label
                anchor = label if label.winfo_manager() else self._compact_status_label
                chip.pack(side="left", fill="y", padx=_CHIP_PAD_X, before=anchor)
            else:
                chip.pack_forget()
