#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""hidhide_setup.py - HidHide 設定の画面（tkinter）。

状態を行で見せ、押せる項目だけにボタンを置く。説明文ではなく
「何が済んでいて、何が残っているか」を配置で伝える。
判断と CLI の実行は core.hidhide / core.hidhide_setup が持ち、
ここでは見せる・押されたら渡すだけにする（Window 本体は import しない）。

CLI の往復で画面を固めないよう、検出・適用はワーカースレッドで行い、
結果は ``after(0)`` で Tk スレッドへ戻す。窓は 1 つだけ開く。
"""

from __future__ import annotations

import threading
import tkinter as tk
import tkinter.messagebox as tkmsg
import tkinter.ttk as ttk
import webbrowser
from collections.abc import Callable
from typing import Any

from core.hidhide import HidHideStatus, detect_hidhide
from core.hidhide_setup import (
    CLIENT_HINT,
    INSTALL_URL,
    CliRun,
    GamingDevice,
    apply_recommended,
    failure_message,
    read_game_devices,
    release_setup,
    set_cloak,
    switch_devices,
    switch_hidden_in_status,
    switch_hide_paths,
)
from loguru import logger

_OK_COLOR = "#1a6b2a"
_WARN_COLOR = "#9a5b00"
_BAD_COLOR = "#a12a2a"
_MUTED_COLOR = "#666666"

_MARKERS = {"ok": "✓", "warn": "!", "none": "·"}

# (status, devices, inventory error)
_DetectResult = tuple[HidHideStatus, tuple[GamingDevice, ...], str]
# (run, status, devices, inventory error)
_ChangeResult = tuple[CliRun, HidHideStatus, tuple[GamingDevice, ...], str]

#: 開いている窓。破棄済みは掴み直す。
_open_win: Any | None = None


def _alive(window: Any) -> bool:
    try:
        return window is not None and bool(window.winfo_exists())
    except Exception:
        return False


def open_hidhide_setup(
    master: tk.Misc, on_applied: Callable[[], Any] | None = None
) -> Any:
    """HidHide 設定の窓を開く。既に開いていれば手前へ出す。

    ``on_applied`` は設定が正常に書かれた後に Tk スレッドで呼ぶ。
    呼び出し側は、起動時に固定した判定をやり直すために使う。
    """
    global _open_win
    window = _open_win
    if window is not None and _alive(window):
        if on_applied is not None:
            window._on_applied = on_applied
        try:
            window.lift()
            window.focus_force()
        except tk.TclError:
            pass
        return window
    _open_win = HidHideSetupDialog(master, on_applied=on_applied)
    return _open_win


def close_hidhide_setup() -> None:
    """終了処理から呼ぶ。開いていなければ何もしない。"""
    global _open_win
    window, _open_win = _open_win, None
    if window is not None and _alive(window):
        try:
            window.destroy()
        except Exception:
            pass


class HidHideSetupDialog(tk.Toplevel):
    """HidHide の状態表示と設定変更。"""

    def __init__(
        self, master: tk.Misc, on_applied: Callable[[], Any] | None = None
    ) -> None:
        super().__init__(master)
        self.title("HidHide")
        self.transient(master)  # type: ignore[call-overload]
        self.resizable(False, False)
        self.minsize(430, 0)

        self._on_applied = on_applied
        self._busy = False
        self._status: HidHideStatus = HidHideStatus()
        self._devices: tuple[GamingDevice, ...] = ()
        self._markers: dict[str, ttk.Label] = {}
        self._values: dict[str, ttk.Label] = {}
        self._actions: dict[str, ttk.Button] = {}

        body = ttk.Frame(self, padding=(18, 12))
        body.grid(row=0, column=0, sticky="nsew")
        body.columnconfigure(0, weight=1)

        rows = ttk.Frame(body)
        rows.grid(row=0, column=0, sticky="ew")
        rows.columnconfigure(1, weight=1)
        specs = (
            ("install", "HidHide"),
            ("app", "アプリの許可"),
            ("device", "Pro Controller"),
            ("cloak", "cloak"),
        )
        for row, (key, text) in enumerate(specs):
            marker = ttk.Label(rows, text=_MARKERS["none"], width=2, anchor="center")
            marker.grid(row=row, column=0, sticky="w", padx=(0, 6), pady=3)
            ttk.Label(rows, text=text).grid(row=row, column=1, sticky="w", pady=3)
            value = ttk.Label(rows, text="-", anchor="e")
            value.grid(row=row, column=2, sticky="e", padx=(12, 0), pady=3)
            self._markers[key] = marker
            self._values[key] = value
            action = ttk.Button(rows, width=10)
            action.grid(row=row, column=3, sticky="e", padx=(14, 0), pady=3)
            action.grid_remove()
            self._actions[key] = action

        self.install_button = self._actions["install"]
        self.install_button.config(text="入手", command=self._open_install_page)
        self.cloak_button = self._actions["cloak"]
        self.cloak_button.config(command=self._on_toggle_cloak)

        ttk.Separator(body).grid(row=1, column=0, sticky="ew", pady=(10, 8))

        self.apply_button = ttk.Button(
            body, text="セットアップ", command=self._on_apply
        )
        self.apply_button.grid(row=2, column=0, sticky="ew")

        secondary = ttk.Frame(body)
        secondary.grid(row=3, column=0, sticky="ew", pady=(6, 0))
        secondary.columnconfigure(0, weight=1)
        self.refresh_button = ttk.Button(
            secondary, text="再検出", width=8, command=self._on_refresh
        )
        self.refresh_button.grid(row=0, column=0, sticky="w")
        self.release_button = ttk.Button(
            secondary, text="解除", width=8, command=self._on_release
        )
        self.release_button.grid(row=0, column=1, sticky="e")

        self.message_label = ttk.Label(body, wraplength=360, justify="left")
        self.message_label.grid(row=4, column=0, sticky="w", pady=(8, 0))
        self.message_label.grid_remove()

        self.protocol("WM_DELETE_WINDOW", self._close)
        self.bind("<Destroy>", self._on_destroyed, add="+")
        self.bind("<Escape>", lambda _event: self._close(), add="+")

        # 親ウィンドウが画面より大きい／別モニタにある場合、transient の
        # 既定位置だと画面外へ出ることがある。見える位置へ寄せる。
        self.update_idletasks()
        self._place_near_master(master)

        self._sync_buttons()
        self.after(50, self._on_refresh)

    def _place_near_master(self, master: tk.Misc) -> None:
        """親の中央へ置き、画面外へはみ出さないよう寄せる。"""
        try:
            width = max(self.winfo_width(), self.winfo_reqwidth())
            height = max(self.winfo_height(), self.winfo_reqheight())
            x = master.winfo_rootx() + max(0, (master.winfo_width() - width) // 2)
            y = master.winfo_rooty() + max(0, (master.winfo_height() - height) // 2)
            x = max(0, min(x, self.winfo_screenwidth() - width))
            y = max(0, min(y, self.winfo_screenheight() - height))
            self.geometry(f"+{x}+{y}")
        except Exception:
            pass

    # -- 非同期の往復 --------------------------------------------------------

    def _run_task(
        self,
        work: Callable[[], Any],
        done: Callable[[Any], None],
    ) -> None:
        """CLI の往復を別スレッドで行い、結果を Tk スレッドへ戻す。"""
        if self._busy:
            return
        self._busy = True
        self._sync_buttons()

        def worker() -> None:
            try:
                result: Any = work()
            except Exception as e:
                result = e
            self._post(lambda: self._finish_task(result, done))

        threading.Thread(target=worker, name="HidHideSetup", daemon=True).start()

    def _post(self, callback: Callable[[], None]) -> None:
        try:
            self.after(0, callback)
        except Exception:
            pass

    def _finish_task(self, result: Any, done: Callable[[Any], None]) -> None:
        self._busy = False
        if not _alive(self):
            return
        try:
            if isinstance(result, Exception):
                self._set_message(f"{result}", "error")
                return
            done(result)
        finally:
            self._sync_buttons()

    # -- 状態の読み込み ------------------------------------------------------

    def _on_refresh(self) -> None:
        self._run_task(self._detect, self._show_detected)

    @staticmethod
    def _detect() -> _DetectResult:
        status = detect_hidhide(force=True)
        if not status.installed or not status.cli_path:
            return (status, (), "")
        devices, run = read_game_devices(status.cli_path)
        return (status, devices, "" if run.ok else run.error)

    def _show_detected(self, result: _DetectResult) -> None:
        status, devices, inventory_error = result
        self._update_status(status, devices, inventory_error)
        if status.error:
            self._set_message(f"{status.error}\n{CLIENT_HINT}", "error")
        else:
            self._clear_message()

    # -- 設定の変更 ----------------------------------------------------------

    def _on_apply(self) -> None:
        if not self._status.cli_path:
            return
        self._run_task(self._apply_worker, self._show_applied)

    @staticmethod
    def _apply_worker() -> _ChangeResult:
        status = detect_hidhide(force=True)
        cli = status.cli_path
        devices: tuple[GamingDevice, ...] = ()
        inventory_error = ""
        if cli:
            devices, inventory = read_game_devices(cli)
            if not inventory.ok:
                inventory_error = inventory.error
        if not cli:
            return (
                CliRun(code=-1, error="HidHideCLI.exe が見つかりません"),
                status,
                devices,
                inventory_error,
            )
        run = apply_recommended(cli, hide_paths=switch_hide_paths(devices))
        return (run, detect_hidhide(cli, force=True), devices, inventory_error)

    def _show_applied(self, result: _ChangeResult) -> None:
        run, status, devices, inventory_error = result
        self._update_status(status, devices, inventory_error)
        if not run.ok:
            self._set_message(failure_message(run), "error")
            return
        if inventory_error:
            self._set_message(f"{inventory_error}\n{CLIENT_HINT}", "error")
            return
        if not switch_devices(devices):
            self._set_message("コントローラを接続してください", "warning")
            return
        enabled = self._notify_applied()
        self._set_message(
            "Pro Controller を有効にしました" if enabled else "設定しました",
            "success",
        )

    def _notify_applied(self) -> bool:
        """設定後に呼び出し側の判定をやり直す。使える状態なら True。"""
        callback = self._on_applied
        if callback is None:
            return False
        try:
            return bool(callback())
        except Exception as e:
            logger.warning(f"HidHide設定後の反映に失敗しました: {e}")
            return False

    def _on_toggle_cloak(self) -> None:
        if not self._status.cli_path:
            return
        self._run_task(self._cloak_worker, self._show_toggled)

    @staticmethod
    def _cloak_worker() -> _ChangeResult:
        status = detect_hidhide(force=True)
        cli = status.cli_path
        if not cli:
            return (
                CliRun(code=-1, error="HidHideCLI.exe が見つかりません"),
                status,
                (),
                "",
            )
        run = set_cloak(cli, not status.cloak_on)
        devices, inventory = read_game_devices(cli)
        return (
            run,
            detect_hidhide(cli, force=True),
            devices,
            "" if inventory.ok else inventory.error,
        )

    def _show_toggled(self, result: _ChangeResult) -> None:
        run, status, devices, inventory_error = result
        self._update_status(status, devices, inventory_error)
        if not run.ok:
            self._set_message(failure_message(run), "error")
            return
        self._set_message("cloak ON" if status.cloak_on else "cloak OFF", "success")

    def _on_release(self) -> None:
        if not self._status.cli_path:
            return
        if not tkmsg.askyesno(
            "HidHide",
            "HidHide の設定を解除します。よろしいですか？",
            parent=self,
        ):
            return
        self._run_task(self._release_worker, self._show_released)

    @staticmethod
    def _release_worker() -> _ChangeResult:
        status = detect_hidhide(force=True)
        cli = status.cli_path
        if not cli:
            return (
                CliRun(code=-1, error="HidHideCLI.exe が見つかりません"),
                status,
                (),
                "",
            )
        run = release_setup(cli, hide_paths=status.hidden_devices)
        devices, inventory = read_game_devices(cli)
        return (
            run,
            detect_hidhide(cli, force=True),
            devices,
            "" if inventory.ok else inventory.error,
        )

    def _show_released(self, result: _ChangeResult) -> None:
        run, status, devices, inventory_error = result
        self._update_status(status, devices, inventory_error)
        if not run.ok:
            self._set_message(failure_message(run), "error")
            return
        self._set_message("解除しました", "success")

    # -- 表示の反映 ----------------------------------------------------------

    def _update_status(
        self,
        status: HidHideStatus,
        devices: tuple[GamingDevice, ...],
        inventory_error: str,
    ) -> None:
        self._status = status
        self._devices = devices

        if status.installed:
            self._set_row("install", "済み", "ok")
        else:
            self._set_row("install", "未導入", "warn")

        if not status.installed:
            self._set_row("app", "—", "none")
            self._set_row("device", "—", "none")
            self._set_row("cloak", "—", "none")
        else:
            self._set_row(
                "app",
                "許可済み" if status.app_whitelisted else "未許可",
                "ok" if status.app_whitelisted else "warn",
            )
            present = switch_devices(devices)
            hidden = switch_hidden_in_status(status, devices)
            if inventory_error:
                self._set_row("device", "確認できません", "warn")
            elif not present:
                self._set_row("device", "未接続", "none")
            elif hidden:
                text = "hide済み" if len(present) == 1 else f"{len(present)}台 hide済み"
                self._set_row("device", text, "ok")
            else:
                text = "未hide" if len(present) == 1 else f"{len(present)}台 未hide"
                self._set_row("device", text, "warn")
            self._set_row(
                "cloak",
                "ON" if status.cloak_on else "OFF",
                "ok" if status.cloak_on else "warn",
            )
        self._sync_buttons()

    def _set_row(self, key: str, text: str, state: str) -> None:
        marker = self._markers.get(key)
        value = self._values.get(key)
        color = {
            "ok": _OK_COLOR,
            "warn": _WARN_COLOR,
            "none": _MUTED_COLOR,
        }.get(state, _MUTED_COLOR)
        if marker is not None:
            marker.config(text=_MARKERS[state], foreground=color)
        if value is not None:
            value.config(text=text, foreground=color)

    def _sync_buttons(self) -> None:
        if not _alive(self):
            return
        usable = (
            not self._busy and self._status.installed and bool(self._status.cli_path)
        )
        self.apply_button.config(state="normal" if usable else "disabled")
        self.release_button.config(state="normal" if usable else "disabled")
        self.refresh_button.config(state="disabled" if self._busy else "normal")
        self.cloak_button.config(
            text="OFFにする" if self._status.cloak_on else "ONにする",
            state="normal" if usable else "disabled",
        )
        install = self._actions.get("install")
        if install is not None:
            if self._status.installed:
                install.grid_remove()
            else:
                install.grid()
        cloak = self._actions.get("cloak")
        if cloak is not None:
            if self._status.installed:
                cloak.grid()
            else:
                cloak.grid_remove()

    def _set_message(self, text: str, kind: str) -> None:
        color = {
            "success": _OK_COLOR,
            "warning": _WARN_COLOR,
            "error": _BAD_COLOR,
        }.get(kind, _MUTED_COLOR)
        try:
            self.message_label.config(text=text, foreground=color)
            self.message_label.grid()
        except tk.TclError:
            pass

    def _clear_message(self) -> None:
        try:
            self.message_label.config(text="")
            self.message_label.grid_remove()
        except tk.TclError:
            pass

    # -- 外部を開く ----------------------------------------------------------

    def _open_install_page(self) -> None:
        webbrowser.open(INSTALL_URL)

    # -- 終了 ----------------------------------------------------------------

    def _close(self) -> None:
        self.destroy()

    def _on_destroyed(self, event: Any) -> None:
        if event.widget is not self:
            return
        global _open_win
        if _open_win is self:
            _open_win = None
        logger.debug("Close HidHide setup window")
