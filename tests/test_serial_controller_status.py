"""手動入力の受付状態の契約。実 Tk（枠だけ）。T01。

「設定が有効」「機器から読取中」「Switch へ送信中」を混ぜない。
送信先・手動入力はタブ内だけに置かず、アプリ下端の状態バー
（layout_panel）が全タブ共通で出す。ここではその入力状態の文言を見る。
"""

from __future__ import annotations

import tkinter as tk
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest
from core.connection_state import ConnectionSnapshot
from ui.serial_panel import SerialPanelMixin


class _Var:
    def __init__(self, value: Any) -> None:
        self.value = value

    def get(self) -> Any:
        return self.value

    def set(self, value: Any) -> None:
        self.value = value


@pytest.fixture
def host(tk_root: tk.Tk) -> Iterator[Any]:
    window = tk.Toplevel(tk_root)
    window.withdraw()
    obj: Any = SerialPanelMixin.__new__(SerialPanelMixin)
    obj.serial = SimpleNamespace(
        connection_snapshot=lambda: obj.snapshot,
    )
    obj.snapshot = ConnectionSnapshot()
    obj.runner = SimpleNamespace(is_busy=lambda: getattr(obj, "_busy", False))
    obj._busy = False
    obj.status_input = tk.StringVar(window, value="")
    obj.arbitration_hint = tk.Label(window)
    obj.is_use_keyboard = _Var(False)
    obj.is_use_gamepad = _Var(False)
    obj.camera_lf = SimpleNamespace(
        is_use_left_stick_mouse=_Var(False),
        is_use_right_stick_mouse=_Var(False),
    )
    obj.cb_left_stick_mouse = tk.Checkbutton(window)
    obj.cb_right_stick_mouse = tk.Checkbutton(window)
    try:
        yield obj
    finally:
        window.destroy()


def _state(host: Any) -> str:
    return host.status_input.get()


def test_disconnected_says_it_waits_for_a_connection(host: Any) -> None:
    host.snapshot = ConnectionSnapshot()
    host._update_controller_status()

    assert _state(host) == "手動入力: 接続待ち"


def test_ready_with_keyboard_says_which_inputs_are_accepted(host: Any) -> None:
    host.snapshot = ConnectionSnapshot(phase="ready", opened=True, port_name="COM3")
    host.is_use_keyboard.set(True)
    host._update_controller_status()

    assert _state(host) == "手動入力: 受付中（キーボード）"


def test_mouse_and_gamepad_are_listed_without_becoming_exclusive_choices(
    host: Any,
) -> None:
    host.snapshot = ConnectionSnapshot(phase="ready", opened=True, port_name="COM3")
    host.is_use_keyboard.set(True)
    host.is_use_gamepad.set(True)
    host.camera_lf.is_use_right_stick_mouse.set(True)
    host._update_controller_status()

    state = _state(host)
    for name in ("キーボード", "パッド", "マウス"):
        assert name in state


def test_running_command_is_named_as_the_reason_for_stopping_input(host: Any) -> None:
    host.snapshot = ConnectionSnapshot(phase="ready", opened=True, port_name="COM3")
    host.is_use_keyboard.set(True)
    host._busy = True
    host._update_controller_status()

    assert _state(host) == "手動入力: 停止（コマンド実行中）"


def test_ready_without_any_enabled_input_says_so(host: Any) -> None:
    host.snapshot = ConnectionSnapshot(phase="ready", opened=True, port_name="COM3")
    host._update_controller_status()

    assert _state(host) == "手動入力: 停止（入力なし）"


def test_failed_connection_is_not_shown_as_ready(host: Any) -> None:
    host.snapshot = ConnectionSnapshot(
        phase="failed", opened=True, port_name="COM3", message="応答なし"
    )
    host._update_controller_status()

    assert _state(host) == "手動入力: 接続待ち"


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("off", "どちらも断りません"),
        ("script", "コマンド実行中は手動入力を断ります"),
        ("human", "手で触った直後"),
    ],
)
def test_arbitration_choices_explain_their_effect(
    host: Any, mode: str, expected: str
) -> None:
    host._render_arbitration_hint(mode)

    assert expected in host.arbitration_hint.cget("text")


def test_unknown_arbitration_choice_is_not_guessed(host: Any) -> None:
    host._render_arbitration_hint("future-mode")

    text = host.arbitration_hint.cget("text")
    assert "future-mode" in text
    assert "実装に従います" in text
