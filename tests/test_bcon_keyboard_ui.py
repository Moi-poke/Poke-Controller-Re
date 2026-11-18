"""実 Tk の入力欄・サービス境界・閉鎖時の離鍵。外部機器は使わない。"""

import threading
import tkinter as tk
from collections.abc import Iterator
from functools import partial
from types import SimpleNamespace
from typing import Any

import pytest
from bcon_ui_support import Service
from fakes import BconKeyboardSerial
from pokecon.domain.serial import bcon_keyboard
from pokecon.domain.transport.bcon import BconTransport
from pokecon.domain.transport.bcon_protocol import T_KEYBOARD, BconParser
from pokecon.ui.BconSetup import BconSetup
from pokecon.ui.serial_panel import SerialPanelMixin
from test_bcon_layout_contract import _group, _title


@pytest.fixture
def keyboard_ui(
    tk_root: tk.Tk, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[BconSetup, Service, BconTransport, BconKeyboardSerial]]:
    monkeypatch.setattr(
        bcon_keyboard,
        "send_text",
        partial(bcon_keyboard.send_text, sleep=lambda _: None),
    )
    monkeypatch.setattr(
        bcon_keyboard,
        "release_keys",
        partial(bcon_keyboard.release_keys, sleep=lambda _: None),
    )
    wire = BconKeyboardSerial()
    transport = BconTransport()
    transport.ser = wire
    transport._hello_established = True
    service = Service(transport)
    setup = BconSetup(tk_root, service, keyboard_enabled=lambda: True)  # type: ignore[arg-type]
    setup._color_loaded = True
    service.publish(
        generation=7,
        opened=True,
        phase="ready",
        transport_name="switch-bcon",
        hello_ack=(4, 0, 15, 0),
    )
    setup._set_busy()
    setup._grp_keyboard.set_open(True)
    try:
        yield setup, service, transport, wire
    finally:
        setup.close()


def reports(wire: BconKeyboardSerial) -> list[bytes]:
    return [
        body
        for kind, body, _ in BconParser().feed(b"".join(wire.written))
        if kind == T_KEYBOARD
    ]


def run_job(service: Service, transport: BconTransport) -> bool:
    result: list[bool] = []
    work = service.operations[-1][1]
    worker = threading.Thread(target=lambda: result.append(work(transport)))
    worker.start()
    worker.join(5)
    assert not worker.is_alive()
    assert len(result) == 1
    return result[0]


def test_keyboard_group_and_busy_controls(keyboard_ui: Any) -> None:
    setup, service, _, _ = keyboard_ui
    group = _group(setup._kb_text)
    assert _title(group) == "キーボード入力（検証）"
    assert _group(setup._btn_kb_send) is group
    assert _group(setup._btn_kb_release) is group
    assert setup._btn_kb_send.cget("text") == "入力して送信"
    assert setup._btn_kb_release.cget("text") == "キー解放"
    for operation, state in (("キーボード送信", "disabled"), (None, "normal")):
        service.publish(operation=operation)
        setup._set_busy()
        for widget in (
            setup._btn_kb_send,
            setup._btn_kb_release,
            setup._kb_text,
            setup._kb_interval_input,
        ):
            assert str(widget.cget("state")) == state


def test_text_is_captured_on_gui_thread_before_worker_runs(
    keyboard_ui: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    setup, service, transport, wire = keyboard_ui
    setup._kb_text.insert(0, "aA")
    setup._kb_interval.set(80)
    intervals: list[float] = []
    original_send = bcon_keyboard.send_text
    original_interval_get = setup._kb_interval.get

    def send_text(*args: Any, **kwargs: Any) -> Any:
        intervals.append(kwargs["interval_s"])
        return original_send(*args, **kwargs)

    def get_interval() -> int:
        assert threading.get_ident() == gui_thread
        return int(original_interval_get())

    monkeypatch.setattr(bcon_keyboard, "send_text", send_text)
    monkeypatch.setattr(setup._kb_interval, "get", get_interval)
    gui_thread = threading.get_ident()
    original_get = setup._kb_text.get

    def get_text() -> str:
        assert threading.get_ident() == gui_thread
        return str(original_get())

    monkeypatch.setattr(setup._kb_text, "get", get_text)
    setup._on_keyboard_send()
    assert wire.written == []
    setup._kb_text.delete(0, tk.END)
    setup._kb_text.insert(0, "b")
    setup._kb_interval.set(120)
    assert run_job(service, transport)
    assert intervals == [0.08]
    assert reports(wire) == [
        bcon_keyboard.report(0, 4),
        bytes(8),
        bcon_keyboard.report(2, 4),
        bytes(8),
        bytes(8),
        bytes(8),
    ]
    setup._poll()
    assert "2/2文字" in setup._log.get("1.0", tk.END)


@pytest.mark.parametrize("text", ["abc日本語", "abc\x7f"])
def test_invalid_text_has_zero_writes_and_names_character(
    keyboard_ui: Any, text: str
) -> None:
    setup, service, transport, wire = keyboard_ui
    setup._kb_text.insert(0, text)
    setup._on_keyboard_send()
    assert not run_job(service, transport)
    assert wire.written == []
    setup._poll()
    assert repr(text[3]) in setup._log.get("1.0", tk.END)


def test_unestablished_hello_is_explained_without_writes(keyboard_ui: Any) -> None:
    setup, service, transport, wire = keyboard_ui
    transport._hello_established = False
    setup._kb_text.insert(0, "abc")
    setup._on_keyboard_send()
    assert not run_job(service, transport)
    assert wire.written == []
    setup._poll()
    assert "先に接続（HELLO→開始）を実行してください" in setup._log.get("1.0", tk.END)


def test_release_button_and_idle_close_send_neutral_twice(keyboard_ui: Any) -> None:
    setup, service, transport, wire = keyboard_ui
    setup._on_keyboard_release()
    assert run_job(service, transport)
    assert reports(wire) == [bytes(8), bytes(8)]
    wire.written.clear()
    setup.close()
    assert run_job(service, transport)
    assert reports(wire) == [bytes(8), bytes(8)]


@pytest.mark.parametrize("stop", ["close", "hide", "generation"])
def test_close_or_cancel_during_press_releases_without_next_press(
    keyboard_ui: Any,
    monkeypatch: pytest.MonkeyPatch,
    stop: str,
) -> None:
    setup, service, transport, wire = keyboard_ui
    pressed, resume = threading.Event(), threading.Event()
    original_write = wire.write

    def write(data: bytes) -> int:
        count = original_write(data)
        if data[3:11] != bytes(8):
            pressed.set()
            assert resume.wait(5)
        return count

    monkeypatch.setattr(wire, "write", write)
    setup._kb_text.insert(0, "ab")
    setup._on_keyboard_send()
    work = service.operations[-1][1]
    results: list[bool] = []
    worker = threading.Thread(target=lambda: results.append(work(transport)))
    worker.start()
    try:
        assert pressed.wait(5)
        if stop == "close":
            setup.close()
        elif stop == "hide":
            setup.set_visible(False)
        else:
            service.publish(generation=8)
    finally:
        resume.set()
        worker.join(5)
    assert not worker.is_alive()
    assert results == [False]
    assert reports(wire) == [bcon_keyboard.report(0, 4), bytes(8), bytes(8), bytes(8)]


def test_keyboard_focus_gate_and_warnings(keyboard_ui: Any) -> None:
    setup, service, _, _ = keyboard_ui
    host = SimpleNamespace(
        root=setup.window,
        _kb_window_active=threading.Event(),
        serial=SimpleNamespace(
            keyboard=object(), connection_snapshot=service.connection_snapshot
        ),
    )
    SerialPanelMixin.onFocusInController(
        host, SimpleNamespace(widget=setup._btn_kb_send)
    )  # type: ignore[arg-type]
    assert host._kb_window_active.is_set()
    SerialPanelMixin.onFocusInController(host, SimpleNamespace(widget=setup._kb_text))  # type: ignore[arg-type]
    assert not host._kb_window_active.is_set()
    assert "入力欄の外" in setup._kb_focus_note.cget("text")
    service.publish(status=(0, 0, 0, 0, 0))
    setup._set_busy()
    assert "無線モードでは Switch に届きません" in setup._kb_mode_note.cget("text")
    service.publish(status=(0x20, 0, 0, 0, 0))
    setup._keyboard_enabled = lambda: False
    setup._set_busy()
    assert setup._kb_mode_note.cget("text") == ""
    assert setup._kb_focus_note.cget("text") == ""


@pytest.mark.parametrize(
    ("ack", "allowed", "message"),
    [
        (None, False, "先に接続"),
        ((4, 0, 12, 0), False, "PicoのFWが古い（0.13未満）。書き込んでください"),
        ((4, 0, 13, 0), True, "FW15未満はキーボード取りこぼし対策前"),
        ((4, 0, 14, 0), True, "FW15未満はキーボード取りこぼし対策前"),
        ((4, 0, 15, 0), True, "通信へ書き込みました"),
    ],
)
def test_firmware_guard(
    keyboard_ui: Any, ack: Any, allowed: bool, message: str
) -> None:
    setup, service, transport, wire = keyboard_ui
    service.publish(hello_ack=ack)
    setup._kb_text.insert(0, "a")
    setup._on_keyboard_send()
    assert run_job(service, transport) is allowed
    assert bool(reports(wire)) is allowed
    setup._poll()
    log = setup._log.get("1.0", tk.END)
    assert message in log
    if ack == (4, 0, 15, 0):
        assert "FW15未満" not in log


@pytest.mark.parametrize("value", ["", "abc", "19", "201"])
def test_invalid_interval_never_starts_worker(keyboard_ui: Any, value: str) -> None:
    setup, service, _, wire = keyboard_ui
    setup._kb_interval_input.delete(0, tk.END)
    setup._kb_interval_input.insert(0, value)
    setup._on_keyboard_send()
    assert service.operations == []
    assert wire.written == []


def test_release_captures_selected_interval(
    keyboard_ui: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    setup, service, transport, wire = keyboard_ui
    intervals: list[float] = []
    original_release = bcon_keyboard.release_keys

    def release(*args: Any, **kwargs: Any) -> Any:
        intervals.append(kwargs["interval_s"])
        return original_release(*args, **kwargs)

    monkeypatch.setattr(bcon_keyboard, "release_keys", release)
    setup._kb_interval.set(100)
    setup._on_keyboard_release()
    setup._kb_interval.set(50)
    assert run_job(service, transport)
    assert intervals == [0.1]
    assert reports(wire) == [bytes(8), bytes(8)]
