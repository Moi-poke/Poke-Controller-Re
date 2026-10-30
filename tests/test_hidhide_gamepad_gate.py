"""HidHide 未設定時にゲームパッド操作の入口を閉じる契約。実 Tk。

通常の読み取り経路は HID 直結だけで、SDL はデバッグ用の環境変数が
あるときしか使わない。HidHide が未設定ならチェック自体を無効にし、
「押せるのに動かない」状態を見せない。HidHide 設定の直後は判定を
やり直し、使えるようになったらそのまま操作を始める。
"""

from __future__ import annotations

import tkinter as tk
import tkinter.ttk as ttk
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest
from core.hidhide import HidHideStatus
from ui.serial_panel import SerialPanelMixin


class _Serial:
    """set_gamepad_enabled の呼び出しだけ覚える代役。"""

    def __init__(self) -> None:
        self.events: list[bool] = []
        self.phase = "ready"

    def set_gamepad_enabled(self, enabled: bool) -> None:
        self.events.append(enabled)

    def connection_snapshot(self) -> Any:
        return SimpleNamespace(phase=self.phase)

    def stop_gamepad(self) -> None:
        pass


class _Panel(SerialPanelMixin):
    """ゲームパッド欄だけを持つ最小のパネル。"""

    def __init__(self, root: tk.Tk, *, enabled: bool) -> None:
        self.is_use_gamepad = tk.BooleanVar(master=root, value=enabled)
        self.cb_use_gamepad = ttk.Checkbutton(
            root, text="ゲームパッドで操作", variable=self.is_use_gamepad
        )
        self.serial = _Serial()
        self.started = 0

    def activateGamepad(self) -> None:
        self.started += 1

    def _on_setting_changed(self) -> None:
        pass


@pytest.fixture(autouse=True)
def _clear_hidhide_cache() -> Iterator[None]:
    """detect_hidhide の5秒キャッシュをテストごとに捨てる。"""
    from core import hidhide

    hidhide.clear_cache()
    yield
    hidhide.clear_cache()


def _install_status(monkeypatch: pytest.MonkeyPatch, *, ready: bool) -> None:
    from core import hidhide

    status = HidHideStatus(
        installed=True,
        cloak_on=ready,
        app_whitelisted=True,
        hidden_nintendo=True,
    )
    monkeypatch.setattr(hidhide, "detect_hidhide", lambda **kwargs: status)


def _gate(panel: _Panel) -> None:
    """_build_control_frame と同じ順序で判定して門を適用する。"""
    panel._hidhide_gamepad_ok = panel._gamepad_enabled_by_hidhide()
    panel._apply_hidhide_gamepad_gate()


def test_not_ready_disables_the_checkbox_and_clears_the_setting(
    tk_root: tk.Tk, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_status(monkeypatch, ready=False)
    panel = _Panel(tk_root, enabled=True)

    _gate(panel)

    assert panel._gamepad_enabled_by_hidhide() is False
    assert panel.cb_use_gamepad.instate(["disabled"])
    assert panel.is_use_gamepad.get() is False
    # 設定に True が残っていたので、開始済みの実体も止める。
    assert panel.serial.events == [False]


def test_not_ready_keeps_an_off_setting_off(
    tk_root: tk.Tk, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_status(monkeypatch, ready=False)
    panel = _Panel(tk_root, enabled=False)

    _gate(panel)

    assert panel.cb_use_gamepad.instate(["disabled"])
    assert panel.is_use_gamepad.get() is False
    assert panel.serial.events == []


def test_ready_leaves_the_checkbox_usable(
    tk_root: tk.Tk, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_status(monkeypatch, ready=True)
    panel = _Panel(tk_root, enabled=False)

    _gate(panel)

    assert panel._gamepad_enabled_by_hidhide() is True
    assert not panel.cb_use_gamepad.instate(["disabled"])
    assert panel.is_use_gamepad.get() is False
    assert panel.serial.events == []


def test_ready_keeps_the_restored_setting(
    tk_root: tk.Tk, monkeypatch: pytest.MonkeyPatch
) -> None:
    """設定の流し込みで True に戻った値を、ready のときは消さない。"""
    _install_status(monkeypatch, ready=True)
    panel = _Panel(tk_root, enabled=False)
    _gate(panel)

    panel.is_use_gamepad.set(True)
    panel._apply_hidhide_gamepad_gate()

    assert panel.is_use_gamepad.get() is True
    assert not panel.cb_use_gamepad.instate(["disabled"])
    assert panel.serial.events == []


def test_not_ready_closes_the_gate_again_after_settings_restore(
    tk_root: tk.Tk, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_apply_settings_to_widgets が True を書き戻しても選べないまま保つ。"""
    _install_status(monkeypatch, ready=False)
    panel = _Panel(tk_root, enabled=False)
    _gate(panel)

    # Window._apply_settings_to_widgets の流し込みを再現する。
    panel.is_use_gamepad.set(True)
    panel._apply_hidhide_gamepad_gate()

    assert panel.is_use_gamepad.get() is False
    assert panel.cb_use_gamepad.instate(["disabled"])
    assert panel.serial.events == [False]


def test_after_setup_enables_the_gamepad_once_hidhide_is_ready(
    tk_root: tk.Tk, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ワンクリック設定の直後、再起動なしでパッド操作を始める。"""
    _install_status(monkeypatch, ready=True)
    panel = _Panel(tk_root, enabled=False)

    assert panel._after_hidhide_setup() is True

    assert panel._hidhide_gamepad_ok is True
    assert panel.is_use_gamepad.get() is True
    assert panel.started == 1
    assert not panel.cb_use_gamepad.instate(["disabled"])


def test_after_setup_does_not_enable_without_a_serial_connection(
    tk_root: tk.Tk, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_status(monkeypatch, ready=True)
    panel = _Panel(tk_root, enabled=False)
    panel.serial.phase = "closed"

    assert panel._after_hidhide_setup() is False

    assert panel._hidhide_gamepad_ok is True
    assert panel.is_use_gamepad.get() is False
    assert panel.started == 0
