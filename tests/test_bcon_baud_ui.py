"""bcon baud切替UIの試験（実機不要・headless）。

- 表示・可否・候補の組み立ては core.transport.bcon_baud（純粋関数）が持つ。
- メイン画面の baud 欄は bcon 系のときだけ readonly で選べる。
- レガシーは disabled（9600 前提のマイコンを誤らないため）。
- 永続化は settings.ini の bcon_baud_index（0-4）。
"""

from __future__ import annotations

import pytest


def test_baud_index_to_bps_uses_protocol_table():
    """各indexはBAUD_TABLEの値に一致する（期待値は表から読む）。"""
    from core.transport import bcon_baud as baud_ui
    from core.transport.bcon_protocol import BAUD_TABLE

    # 表の5件すべてが引けること。ハードコードせず表に当てる。
    for index in sorted(BAUD_TABLE.keys()):
        assert baud_ui.baud_bps_for_index(index) == BAUD_TABLE[index]


def test_baud_default_index_is_table_default():
    """既定indexはDEFAULT_BAUD_INDEX（=3）で1Mbpsを指す。"""
    from core.transport import bcon_baud as baud_ui
    from core.transport.bcon_protocol import BAUD_TABLE, DEFAULT_BAUD_INDEX

    assert DEFAULT_BAUD_INDEX == 3
    assert BAUD_TABLE[DEFAULT_BAUD_INDEX] == 1000000
    assert (
        baud_ui.baud_bps_for_index(baud_ui.BAUD_DEFAULT_INDEX)
        == BAUD_TABLE[DEFAULT_BAUD_INDEX]
    )


def test_baud_invalid_index_never_sent():
    """範囲外indexは送出せず拒否する（BAUD_SETを投げない）。"""
    from core.transport import bcon_baud as baud_ui
    from core.transport.bcon_protocol import BAUD_TABLE

    for bad in (-1, 5, 99):
        assert bad not in BAUD_TABLE
        with pytest.raises(ValueError):
            baud_ui.baud_bps_for_index(bad)
    # 送出ガード：不正indexは送信可否がFalse。
    assert baud_ui.baud_index_sendable(-1) is False
    assert baud_ui.baud_index_sendable(5) is False


def test_baud_adopt_confirm_message():
    """adopt成功時は確定文言（切替完了＋bps入り）を出す。"""
    from core.transport import bcon_baud as baud_ui
    from core.transport.bcon_protocol import BAUD_TABLE, DEFAULT_BAUD_INDEX

    bps = BAUD_TABLE[DEFAULT_BAUD_INDEX]
    msg = baud_ui.baud_adopt_message(bps)
    assert str(bps) in msg
    assert "切替" in msg


def test_baud_revert_display_message():
    """adopt失敗時は復帰文言（新旧bps入り・確定と文面が違う）を出す。"""
    from core.transport import bcon_baud as baud_ui
    from core.transport.bcon_protocol import BAUD_TABLE

    new_bps = BAUD_TABLE[1]
    old_bps = BAUD_TABLE[3]
    adopt = baud_ui.baud_adopt_message(new_bps)
    revert = baud_ui.baud_revert_message(new_bps, old_bps)
    assert str(new_bps) in revert
    assert str(old_bps) in revert
    assert revert != adopt
    assert "戻" in revert


def test_combo_state_is_editable_only_for_bcon():
    """レガシーは固定、bcon だけ選べる。"""
    from core.transport import bcon_baud as baud_ui

    assert baud_ui.baud_combo_state(True) == "readonly"
    assert baud_ui.baud_combo_state(False) == "disabled"


def test_combo_values_switch_between_table_and_legacy():
    """bcon は BAUD_TABLE の表示名、レガシーは渡した候補をそのまま使う。"""
    from core.transport import bcon_baud as baud_ui
    from core.transport.bcon_protocol import BAUD_TABLE

    labels = baud_ui.baud_combo_values(True, [9600, 4800])
    assert labels == [baud_ui.BAUD_DISPLAY_LABELS[i] for i in sorted(BAUD_TABLE)]
    assert baud_ui.baud_combo_values(False, [9600, 4800]) == ["9600", "4800"]


def test_label_and_index_roundtrip_covers_table():
    """コンボの表示名と index が全段で往復する（選んだ段を取り違えない）。"""
    from core.transport import bcon_baud as baud_ui
    from core.transport.bcon_protocol import BAUD_TABLE

    for index in sorted(BAUD_TABLE):
        label = baud_ui.baud_label_for_index(index)
        assert baud_ui.baud_index_for_label(label) == index


class _Var:
    def __init__(self, value):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


def _baud_host(*, opened=False, phase="disconnected", baud=None, index=3):
    from types import SimpleNamespace

    from core.connection_state import ConnectionSnapshot
    from ui.serial_panel import SerialPanelMixin

    snapshot = ConnectionSnapshot(
        generation=1, phase=phase, transport_name="bcon", opened=opened, baud=baud
    )
    service = SimpleNamespace(
        snapshot=snapshot,
        requests=[],
        connection_snapshot=lambda: service.snapshot,
        uses_bcon=lambda name: name in {"bcon", "switch-bcon", "switch-bcon-proc"},
        change_bcon_baud=lambda selected: service.requests.append(selected) or True,
    )
    host = SerialPanelMixin.__new__(SerialPanelMixin)
    host.serial = service
    host._closing = False
    host._pending_transport_switch = None
    host._connection_view = snapshot
    host.transport_name = _Var("switch-bcon")
    host.bcon_baud = _Var("2M")
    host.settings = SimpleNamespace(bcon_baud_index=_Var(index), baud_rate=_Var(4800))
    host.saved = []
    host._on_setting_changed = lambda: host.saved.append(
        host.settings.bcon_baud_index.get()
    )
    host._connection_change_allowed = lambda: True
    host._refresh_connection_controls = lambda: None
    host._refreshBaudUi = lambda: None
    host._refresh_bcon_tab = lambda: None
    host._refresh_bcon_rows = lambda: None
    host._render_connection_summary = lambda: None
    host._update_title = lambda: None
    host._unbindKeyboardFocus = lambda: None
    host._clearGamepadMirror = lambda: None
    host._gamepad_active = SimpleNamespace(clear=lambda: None)
    host.is_use_keyboard = _Var(False)
    host.is_use_gamepad = _Var(False)
    return host


def test_offline_baud_selection_saves_only_bcon_index():
    host = _baud_host()
    host.applyBaudRate()
    assert host.settings.bcon_baud_index.get() == 4
    assert host.settings.baud_rate.get() == 4800
    assert host.saved == [4]
    assert host.serial.requests == []


def test_connected_baud_saves_only_after_confirmed_snapshot():
    from dataclasses import replace

    host = _baud_host(opened=True, phase="ready", baud=1000000)
    host.applyBaudRate()
    assert host.serial.requests == [4]
    assert host.settings.bcon_baud_index.get() == 3
    assert not host.saved
    confirmed = replace(host.serial.snapshot, baud=2000000)
    host.serial.snapshot = confirmed
    host._apply_connection_snapshot(confirmed)
    assert host.settings.bcon_baud_index.get() == 4
    assert host.saved == [4]
    assert host.settings.baud_rate.get() == 4800


def test_unconfirmed_baud_failure_keeps_saved_index():
    from dataclasses import replace

    host = _baud_host(opened=True, phase="ready", baud=1000000)
    host.applyBaudRate()
    failed = replace(host.serial.snapshot, phase="failed", baud=None)
    host.serial.snapshot = failed
    host._apply_connection_snapshot(failed)
    assert host.settings.bcon_baud_index.get() == 3
    assert host.bcon_baud.get() == "1M"
    assert not host.saved


def test_periodic_status_does_not_recreate_input_listeners():
    from dataclasses import replace

    host = _baud_host(opened=True, phase="hello", baud=None)
    host.is_use_keyboard.set(True)
    host.is_use_gamepad.set(True)
    enabled = []
    host._bindKeyboardFocus = lambda: enabled.append("keyboard")
    host.activateGamepad = lambda: enabled.append("gamepad")
    ready = replace(host.serial.snapshot, phase="ready", baud=1000000)
    host.serial.snapshot = ready
    host._apply_connection_snapshot(ready)
    status = replace(ready, status=(0x20, 1, 0, 0, 0), status_received_at=1.0)
    host.serial.snapshot = status
    host._apply_connection_snapshot(status)
    assert enabled == ["keyboard", "gamepad"]


def test_refresh_reselects_only_the_saved_device_when_it_returns(tk_root, monkeypatch):
    import tkinter as tk
    from tkinter import ttk

    from ui.serial_panel import SerialPanelMixin

    host = _baud_host()
    host.settings.com_port_name = _Var("COM65001")
    host.com_port_name = tk.StringVar(master=tk_root, value="COM65001")
    host.com_port_text = tk.StringVar(master=tk_root)
    host.com_port = tk.IntVar(master=tk_root)
    host.com_port_cb = ttk.Combobox(tk_root, textvariable=host.com_port_text)
    for name in (
        "reloadComPort",
        "disconnectComPort",
        "refreshComPort",
        "bcon_hunt_button",
    ):
        setattr(host, name, ttk.Button(tk_root))
    host.transport_cb = ttk.Combobox(tk_root)
    host.baud_rate_cb = ttk.Combobox(tk_root)
    host.com_port_notice = ttk.Label(tk_root)
    host._refresh_connection_controls = (
        lambda: SerialPanelMixin._refresh_connection_controls(host)
    )
    ports = []
    monkeypatch.setattr("WindowUtils.listComPorts", lambda: ports)
    try:
        for available, expected in (
            ([("COM65001", "saved")], "COM65001"),
            ([], ""),
            ([("COM65002", "other")], ""),
            ([("COM65002", "other"), ("COM65001", "saved")], "COM65001"),
        ):
            ports[:] = available
            host.refreshComPorts()
            assert host.com_port_name.get() == expected
            assert host.reloadComPort.instate(["!disabled"]) == bool(expected)
            assert host.settings.com_port_name.get() == "COM65001"
            assert host.saved == []
            assert host.serial.requests == []
        host.refreshComPorts(keep_current=False)
        assert host.com_port_name.get() == ""
        assert host.reloadComPort.instate(["disabled"])
    finally:
        for widget in (
            host.com_port_cb,
            host.reloadComPort,
            host.disconnectComPort,
            host.refreshComPort,
            host.bcon_hunt_button,
            host.transport_cb,
            host.baud_rate_cb,
            host.com_port_notice,
        ):
            widget.destroy()
