"""接続の写しから作る表示と操作可否を、Tk・実ポートなしで確認する。"""

from collections.abc import Callable
from dataclasses import replace
from queue import Queue
from threading import Event, Thread
from time import monotonic
from types import SimpleNamespace
from typing import Any

import pytest
from BconSetup import BconSetup
from core.connection_state import ConnectionSnapshot
from core.transport.bcon_baud import baud_label_for_index
from core.ui_inbox import UiInbox
from ui.serial_panel import SerialPanelMixin


class Value:
    def __init__(self, value: Any) -> None:
        self.value = value

    def get(self) -> Any:
        return self.value

    def set(self, value: Any) -> None:
        self.value = value


class Widget(dict[str, Any]):
    def configure(self, **kwargs: Any) -> None:
        self.update(kwargs)

    config = configure


class Lamps:
    def __init__(self) -> None:
        self.colors: dict[int, str] = {}

    def itemconfigure(self, item: int, **kwargs: Any) -> None:
        self.colors[item] = kwargs["fill"]


class Runner:
    """接続操作の可否を、テストから変更できる実行状態で答える。"""

    def __init__(self) -> None:
        self.state = "idle"

    def is_busy(self) -> bool:
        return self.state != "idle"


class SnapshotService:
    """画面へ渡すのは写しだけ。存在しない RPC を呼べばその場で失敗する。"""

    def __init__(self, snapshot: ConnectionSnapshot) -> None:
        self.snapshot = snapshot
        self.requests: list[tuple[str, Any]] = []

    def connection_snapshot(self) -> ConnectionSnapshot:
        return self.snapshot

    @staticmethod
    def uses_bcon(name: str) -> bool:
        return name in {"bcon", "switch-bcon", "switch-bcon-proc"}

    def change_bcon_baud(self, index: int) -> bool:
        self.requests.append(("baud", index))
        return True

    def hunt_bcon_baud(self) -> bool:
        self.requests.append(("hunt", None))
        return True

    def request_connect(self, **kwargs: Any) -> bool:
        self.requests.append(("connect", kwargs))
        return True

    def request_switch_transport(self, *args: Any, **kwargs: Any) -> bool:
        self.requests.append(("transport", args))
        return True

    def run_bcon_operation(self, *args: Any, **kwargs: Any) -> bool:
        self.requests.append(("maintenance", args))
        return True


def serial_view(service: Any) -> Any:
    host: Any = SerialPanelMixin.__new__(SerialPanelMixin)
    host.serial = service
    host._connection_view = replace(service.connection_snapshot(), phase="disconnected")
    host._closing = False
    host._pending_transport_switch = None
    host.runner = Runner()
    host.inbox = UiInbox()
    host.settings = SimpleNamespace(
        bcon_baud_index=Value(3),
        setting_path="unused",
        baud_rate=Value("9600"),
        com_port_name=Value("test-port"),
    )
    host.transport_name = Value("switch-bcon")
    host.bcon_baud = Value(baud_label_for_index(3))
    host.baud_rate = Value("9600")
    host.bcon_wired = Value("")
    host.com_port_name = Value("test-port")
    host.com_port = Value(0)
    host.com_port_text = Value("test device")
    host._com_port_map = {"test device": "test-port"}
    host.is_use_keyboard = Value(False)
    host.is_use_gamepad = Value(False)
    host._gamepad_active = Event()
    host._player_lamp_items = [0, 1, 2, 3]
    host.player_lamp_canvas = Lamps()
    for name in (
        "com_port_cb",
        "transport_cb",
        "reloadComPort",
        "disconnectComPort",
        "refreshComPort",
        "baud_rate_cb",
        "bcon_hunt_button",
        "connection_status",
        "connection_details",
        "rumble_label",
        "com_port_notice",
        "cb_use_keyboard",
        "cb_use_gamepad",
        "transport_description",
    ):
        setattr(host, name, Widget())
    host.saved_indices = []
    host._on_setting_changed = lambda: host.saved_indices.append(
        host.settings.bcon_baud_index.get()
    )
    host._update_title = lambda: None
    host._unbindKeyboardFocus = lambda: None
    host._clearGamepadMirror = lambda: None
    host._refresh_bcon_tab = lambda: None
    host._refresh_bcon_rows = lambda: None
    return host


DETAIL_BUTTONS = (
    "_btn_cap",
    "_btn_beacon",
    "_btn_reconnect",
    "_btn_status",
    "_btn_ping",
    "_btn_winfo",
    "_btn_w0",
    "_btn_w1",
    "_btn_e0",
    "_btn_e1",
    "_btn_e2",
    "_btn_bootsel",
    "_btn_clear",
    "_btn_keys",
    "_btn_color",
    "_btn_connect",
    "_btn_hunt",
)


def detail_view(service: SnapshotService) -> Any:
    view: Any = BconSetup.__new__(BconSetup)
    view._service = service
    view._snapshot = service.snapshot
    view._unsupported = set()
    # 初回の本体色取得が済んだ画面で、状態通知による表示更新を確認する。
    view._color_loaded = True
    view._command_busy = lambda: False
    view._visible = True
    view._visibility_revision = 0
    view._closed = False
    view._capture_deadline_id = None
    view._capture_saved_seen = False
    view.inbox = UiInbox()
    view._queue = Queue()
    view._lamp_items = [0, 1, 2, 3]
    view._lamp_canvas = Lamps()
    for name in (
        *DETAIL_BUTTONS,
        "_summary",
        "_actual_baud",
        "_busy_label",
        "_imu_label",
        "_vib_label",
        "_seconds_input",
    ):
        setattr(view, name, Widget())
    view._color_entries = [Widget() for _ in range(4)]
    view.lines = []
    view._append = view.lines.append
    return view


def ready_snapshot(**changes: Any) -> ConnectionSnapshot:
    return replace(
        ConnectionSnapshot(
            generation=1,
            phase="ready",
            transport_name="switch-bcon",
            port_name="test-port",
            baud=1000000,
            opened=True,
            hello_ack=(4, 1, 2, 0),
        ),
        **changes,
    )


def test_baud_settings_commit_only_after_confirmed_snapshot() -> None:
    service = SnapshotService(ready_snapshot())
    host = serial_view(service)
    host.bcon_baud.set(baud_label_for_index(4))
    host.applyBaudRate()
    assert host.settings.bcon_baud_index.get() == 3
    assert host.saved_indices == []
    service.snapshot = replace(service.snapshot, phase="hello", baud=None)
    host._apply_connection_snapshot(service.snapshot)
    assert host.settings.bcon_baud_index.get() == 3
    service.snapshot = replace(service.snapshot, phase="ready", baud=2000000)
    host._apply_connection_snapshot(service.snapshot)
    assert host.settings.bcon_baud_index.get() == 4
    assert host.saved_indices == [4]
    service.snapshot = replace(service.snapshot, phase="failed", baud=None)
    host._apply_connection_snapshot(service.snapshot)
    assert host.settings.bcon_baud_index.get() == 4
    assert host.saved_indices == [4]
    assert "不明" in host.connection_details["text"]


def test_disconnected_baud_choice_is_saved_for_next_connection() -> None:
    service = SnapshotService(ConnectionSnapshot(transport_name="switch-bcon"))
    host = serial_view(service)
    host.bcon_baud.set(baud_label_for_index(4))
    host.applyBaudRate()
    assert host.settings.bcon_baud_index.get() == 4
    assert host._currentBaudRate() == 2000000
    assert host.saved_indices == [4]
    assert service.requests == []


def test_summary_snapshots_clear_lamps_and_ignore_old_generation() -> None:
    service = SnapshotService(
        ready_snapshot(player_info=b"\x0f\x03", rumble=b"\xff\xff")
    )
    host = serial_view(service)
    detail = detail_view(service)
    host._apply_connection_snapshot(service.snapshot)
    detail._apply_snapshot(service.snapshot)
    assert set(host.player_lamp_canvas.colors.values()) == {"green yellow"}
    from BconSetup import PLAYER_LAMP_OFF_COLOR, PLAYER_LAMP_ON_COLOR

    assert set(detail._lamp_canvas.colors.values()) == {PLAYER_LAMP_ON_COLOR}
    old = service.snapshot
    service.snapshot = ConnectionSnapshot(generation=2, transport_name="switch-bcon")
    host._apply_connection_snapshot(service.snapshot)
    detail._apply_snapshot(service.snapshot)
    assert host.bcon_wired.get() == "不明"
    assert set(host.player_lamp_canvas.colors.values()) == {"gray"}
    assert set(detail._lamp_canvas.colors.values()) == {PLAYER_LAMP_OFF_COLOR}
    host._apply_connection_snapshot(old)
    detail._apply_snapshot(old)
    assert host._connection_view == service.snapshot
    assert detail._snapshot == service.snapshot
    assert set(detail._lamp_canvas.colors.values()) == {PLAYER_LAMP_OFF_COLOR}


@pytest.mark.parametrize("flags,mode", [(0x20, "有線"), (0, "無線")])
def test_status_freshness_is_display_only(
    monkeypatch: pytest.MonkeyPatch, flags: int, mode: str
) -> None:
    monkeypatch.setattr("ui.serial_panel.time.monotonic", lambda: 100.0)
    service = SnapshotService(
        ready_snapshot(status=(flags, 7, 0, 0, 0), status_received_at=99.0)
    )
    host = serial_view(service)
    detail = detail_view(service)
    host._apply_connection_snapshot(service.snapshot)
    detail._apply_snapshot(service.snapshot)
    assert host.bcon_wired.get() == mode
    fresh = detail._summary["text"]
    assert mode in fresh
    monkeypatch.setattr("ui.serial_panel.time.monotonic", lambda: 104.0)
    host._render_connection_summary()
    detail._render_summary()
    assert host.bcon_wired.get().startswith(mode)
    assert host.bcon_wired.get() != mode
    assert detail._summary["text"] != fresh
    assert service.snapshot.phase == "ready"
    assert service.requests == []


def test_closed_detail_disables_sending_and_direct_callback_cannot_submit() -> None:
    service = SnapshotService(ConnectionSnapshot())
    detail = detail_view(service)
    detail._set_busy()
    assert all(getattr(detail, name)["state"] == "disabled" for name in DETAIL_BUTTONS)
    for callback in (
        detail._on_ping,
        detail._on_status,
        detail._on_bootsel,
        detail._on_color,
    ):
        callback()
    assert service.requests == []
    assert not detail._queue.empty()


def test_runner_busy_guards_handlers_and_preserves_settings(capsys: Any) -> None:
    service = SnapshotService(ready_snapshot())
    host = serial_view(service)
    host.runner.state = "running"
    host.bcon_baud.set(baud_label_for_index(4))
    host.applyBaudRate()
    host.activateSerial()
    host.huntBconBaud()
    host.applyTransport()
    host.onComPortSelected()
    assert host.settings.bcon_baud_index.get() == 3
    assert host.saved_indices == []
    assert service.requests == []
    assert "コマンドを停止してから変更してください。" in capsys.readouterr().out
    host._refresh_connection_controls()
    assert host.reloadComPort["state"] == "disabled"
    assert host.disconnectComPort["state"] == "normal"
    detail = detail_view(service)
    detail._command_busy = lambda: True
    detail._on_ping()
    detail._set_busy()
    assert detail._btn_ping["state"] == "disabled"
    assert service.requests == []


@pytest.mark.parametrize("ports", [[], [("other-port", "other device")]])
def test_refresh_never_replaces_missing_saved_port_or_opens_another(
    monkeypatch: pytest.MonkeyPatch, ports: list[tuple[str, str]]
) -> None:
    service = SnapshotService(ConnectionSnapshot())
    host = serial_view(service)
    monkeypatch.setattr("ui.serial_panel.WindowUtils.listComPorts", lambda: ports)
    monkeypatch.setattr("ui.serial_panel.fit_combo_width", lambda *args, **kwargs: None)
    host.refreshComPorts()
    host.activateSerial()
    assert host.settings.com_port_name.get() == "test-port"
    assert host.com_port_name.get() == ""
    assert host.reloadComPort["state"] == "disabled"
    assert host.saved_indices == []
    assert service.requests == []


def test_worker_snapshot_delivery_waits_for_gui_drain_and_drops_after_close() -> None:
    service = SnapshotService(ready_snapshot())
    host = serial_view(service)
    detail = detail_view(service)
    before = host._connection_view

    def deliver() -> None:
        host._queue_connection_snapshot(service.snapshot)
        detail._receive_snapshot(service.snapshot)

    worker = Thread(target=deliver)
    worker.start()
    worker.join(5)
    assert not worker.is_alive()
    assert host._connection_view == before
    assert host.player_lamp_canvas.colors == {}
    assert detail._lamp_canvas.colors == {}
    host.inbox.drain()
    detail.inbox.drain()
    assert host._connection_view.phase == "ready"
    assert host.player_lamp_canvas.colors
    assert detail._lamp_canvas.colors
    host.inbox.close()
    detail.inbox.close()
    service.snapshot = replace(service.snapshot, phase="disconnected", opened=False)
    deliver()
    assert host.inbox.drain() == detail.inbox.drain() == 0
    assert host._connection_view.phase == "ready"


def test_transport_switch_reopens_using_destination_saved_baud(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core import Sender
    from core.transport.bcon_protocol import T_HELLO, T_HELLO_ACK
    from core.transport.text_serial import TextSerialTransport
    from services.serial_service import SerialService
    from test_bcon_connection_lifecycle import ControlledWire

    class ReplyWire(ControlledWire):
        def write(self, data: bytes) -> int:
            count = super().write(data)
            if len(data) >= 2 and data[1] == T_HELLO:
                self.inject(T_HELLO_ACK, bytes([4, 1, 0, 0]))
            return count

    opened_rates: list[int] = []

    def open_wire(*args: Any, **kwargs: Any) -> ControlledWire:
        rate = int(args[1] if len(args) > 1 else kwargs["baudrate"])
        opened_rates.append(rate)
        return ReplyWire(rate)

    monkeypatch.setattr("core.transport.bcon.serial.Serial", open_wire)
    service = SerialService(
        notify_user=lambda text: None, base_dir=".", input_log_emit=None
    )
    service.sender = Sender.Sender(
        is_show_serial=False, transport=TextSerialTransport()
    )
    changed = Event()
    host = serial_view(service)
    host.transport_name.set("legacy_text")
    host.settings.bcon_baud_index.set(4)

    def receive(snapshot: ConnectionSnapshot) -> None:
        host._queue_connection_snapshot(snapshot)
        changed.set()

    unsubscribe = service.subscribe_connection(receive)

    def drain_until(predicate: Callable[[ConnectionSnapshot], bool]) -> None:
        deadline = monotonic() + 5
        while True:
            # 通知は状態そのものではない。解除してから箱と現在値を確認する。
            changed.clear()
            host.inbox.drain()
            host._queue_connection_snapshot(service.connection_snapshot())
            host.inbox.drain()
            if predicate(host._connection_view):
                return
            remaining = deadline - monotonic()
            assert remaining > 0, service.connection_snapshot()
            # 待機の成否で判定せず、期限切れでも最後に状態と箱を確認する。
            changed.wait(remaining)

    try:
        assert service.connect(0, "test-port", 9600, False, "")[0]
        drain_until(lambda snapshot: snapshot.phase == "ready")
        for destination, rate in (("switch-bcon", 2000000), ("legacy_text", 9600)):
            before = service.connection_snapshot().generation
            host.transport_name.set(destination)
            host.applyTransport()
            drain_until(
                lambda snapshot: snapshot.generation > before
                and snapshot.phase == "ready"
            )
            assert host._connection_view.baud == rate
        assert opened_rates == [9600, 2000000, 9600]
    finally:
        unsubscribe()
        host.inbox.close()
        service.shutdown()
