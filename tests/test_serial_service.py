"""SerialService の手順の検証。線は偽物、画面なしで回す。"""

import os
import threading

from core import Sender, Transport
from fakes import CannedMonTransport, FakeTransport
from services.serial_service import SenderSpec, SerialService


def make_service() -> tuple[SerialService, list[str]]:
    notices: list[str] = []
    service = SerialService(
        notify_user=notices.append,
        base_dir=".",
        input_log_emit=lambda line: None,
    )
    service.subscribe_connection_log(
        lambda generation, message: notices.append(message)
    )
    return service, notices


def make_spec(**over: object) -> SenderSpec:
    values: dict[str, object] = {
        "transport_name": "legacy_text",
        "is_show_serial": False,
        "arbitration_mode": "off",
        "arbitration_cooldown": 2.0,
        "input_log_format": "simple",
        "input_log_actions": "",
        "input_log_enabled": True,
        "input_log_stick_change": False,
        # ライブ入力の最低保持ミリ秒（SenderSpecの必須項目）
        "live_min_dwell_ms": 16,
    }
    values.update(over)
    return SenderSpec(**values)  # type: ignore[arg-type]


def test_build_sender_legacy() -> None:
    service, _ = make_service()
    service.build_sender(make_spec())
    assert isinstance(service.sender, Sender.Sender)
    assert service.sender.getTransportName() == "legacy_text"
    assert service.sender.isInputLogLinked() is True


def test_selected_transport_override() -> None:
    service, _ = make_service()
    assert service.selected_transport_name("legacy_text") == "legacy_text"
    service.transport_override = "switch-bcon"
    assert service.selected_transport_name("legacy_text") == "switch-bcon"
    service.clear_override()
    assert service.selected_transport_name("legacy_text") == "legacy_text"
    # 未知名は既定へ落ちる（理由つき）
    assert (
        service.selected_transport_name("no_such_name") == Transport.DEFAULT_TRANSPORT
    )


def test_switch_same_name_noop() -> None:
    service, _ = make_service()
    service.build_sender(make_spec())
    switched, linked = service.switch_transport("legacy_text")
    assert switched is False
    assert linked is True


def test_switch_alias_of_current_transport_is_noop() -> None:
    """同じ運搬物の別名で押し直しても作り直さない（bcon→switch-bcon）。"""
    service, _ = make_service()

    class _BconSender:
        def __init__(self) -> None:
            self.set_calls = 0

        def getTransportName(self) -> str:
            return "bcon"

        def setTransport(self, _transport: object) -> bool:
            self.set_calls += 1
            return True

    sender = _BconSender()
    service.sender = sender  # type: ignore[assignment]
    assert service.switch_transport("switch-bcon") == (False, True)
    assert sender.set_calls == 0
    assert service.sender is sender


def test_switch_to_dummy_legacy() -> None:
    class DummyLegacy(FakeTransport):
        name = "dummy_legacy_tmp"

    assert Transport.register_transport("dummy_legacy_tmp", DummyLegacy) is True
    try:
        service, _ = make_service()
        service.build_sender(make_spec())
        switched, linked = service.switch_transport("dummy_legacy_tmp")
        assert switched is True
        assert linked is True
        assert service.sender is not None
        assert service.sender.getTransportName() == "dummy_legacy_tmp"
    finally:
        assert Transport.unregister_transport("dummy_legacy_tmp") is True


def test_gui_call_conventions() -> None:
    """GUI が使う呼び出し形（キーワード引数含む）の回帰検証。

    実起動で TypeError（unexpected keyword argument）が出た反省から、
    画面側と同じ渡し方で呼べることをここで確かめる。
    """
    from loguru import logger

    service, _ = make_service()
    assert service.resolve_arbitration("human", logger=logger) == "human"
    assert service.resolve_arbitration("typo!", logger=logger) == "off"
    service.build_sender(make_spec())
    switched, linked = service.switch_transport("legacy_text", transport_logger=logger)
    assert switched is False
    assert linked is True


def test_connect_success_and_disconnect() -> None:
    service, _ = make_service()
    service.sender = Sender.Sender(is_show_serial=False, transport=FakeTransport())
    connected, keyboard_active = service.connect(
        port_num=0,
        port_name="fake",
        baud=9600,
        keyboard_enabled=False,
        setting_path="",
    )
    assert connected is True
    assert keyboard_active is False
    assert service.is_open() is True
    assert service.key_press is not None
    service.disconnect()
    assert service.is_open() is False
    service.shutdown()
    assert service.key_press is None


def test_connect_bcon_runs_hello_automatically(monkeypatch) -> None:
    """ACK 前に入力が流れず、live 開始まで済んだ ready を待つ。"""
    import time

    from core.transport.bcon import BconTransport
    from core.transport.bcon_protocol import (
        T_HELLO,
        T_HELLO_ACK,
        T_STATE,
        T_STATUS_REQ,
        frame_build,
    )

    class Wire:
        is_open = True
        baudrate = 1000000

        def __init__(self, **kwargs: object) -> None:
            self.written: list[bytes] = []
            self.pending = bytearray()
            self.lock = threading.Lock()
            self.hello_seen = threading.Event()
            self.ack_allowed = threading.Event()

        @property
        def in_waiting(self) -> int:
            with self.lock:
                return len(self.pending)

        def read(self, n: int) -> bytes:
            with self.lock:
                out = bytes(self.pending[:n])
                del self.pending[:n]
                return out

        def write(self, data: bytes) -> int:
            with self.lock:
                self.written.append(bytes(data))
            if data[1] == T_HELLO:
                self.hello_seen.set()
                if self.ack_allowed.wait(2.0):
                    with self.lock:
                        self.pending.extend(
                            frame_build(T_HELLO_ACK, bytes([4, 1, 2, 0]), 0)
                        )
            return len(data)

        def close(self) -> None:
            self.is_open = False

    wire = Wire()
    monkeypatch.setattr("core.transport.bcon.serial.Serial", lambda **kwargs: wire)
    service, _ = make_service()
    line = BconTransport()
    service.sender = Sender.Sender(is_show_serial=False, transport=line)
    ready = threading.Event()
    ready_inputs: list[bool] = []
    accepted: list[bool] = []
    completion_count = 0
    completion_lock = threading.RLock()

    def unsuccessful_config(target) -> bool:
        assert target.send_config(T_STATUS_REQ, b"")
        return False

    def completed(snapshot) -> None:
        nonlocal completion_count
        with completion_lock:
            if (
                snapshot.phase != "ready"
                or snapshot.operation is not None
                or service.connection_snapshot() is not snapshot
            ):
                return
            ready_inputs.append(service.key_press is not None)
            completion_count += 1
            # 完了通知から次を直ちに受付できる。False の通常操作も ready を保つ。
            if completion_count == 1:
                accepted.append(
                    service.run_bcon_operation("状態確認", unsuccessful_config)
                )
            elif completion_count == 2:
                accepted.append(service.retry_bcon_hello())
            else:
                ready.set()

    service.subscribe_connection(completed)
    try:
        assert service.request_connect(0, "fake", 1000000, False, "")
        assert wire.hello_seen.wait(2.0)
        assert service.connection_snapshot().phase == "hello"
        service.sender.writeRow("10 08")
        assert not any(frame[1] == T_STATE for frame in wire.written)
        wire.ack_allowed.set()
        assert ready.wait(3.0)
        assert accepted == [True, True]
        assert ready_inputs and all(ready_inputs)
        assert service.connection_snapshot().hello_ack == (4, 1, 2, 0)
        assert service.connection_snapshot().phase == "ready"
        assert service.connection_snapshot().operation is None
        assert sum(frame[1] == T_HELLO for frame in wire.written) == 2
        assert any(frame[1] == T_STATUS_REQ for frame in wire.written)
        assert line.live_loop_running()
        assert service.sender.isLiveWorkerRunning()
        # 単独操作の未対応は、既に成立した接続を壊さず受付を返す。
        pending = threading.Event()
        operation_done = threading.Event()

        def unsupported_completed(snapshot):
            if snapshot.operation is not None:
                pending.set()
            elif pending.is_set() and snapshot.phase == "ready":
                operation_done.set()

        unsubscribe = service.subscribe_connection(unsupported_completed)
        try:
            for method, request in (
                ("current_baud", service.retry_bcon_hello),
                ("baud_hunt", service.hunt_bcon_baud),
            ):
                monkeypatch.setattr(line, method, None)
                pending.clear()
                operation_done.clear()
                assert request()
                assert operation_done.wait(2.0)
                snapshot = service.connection_snapshot()
                assert snapshot.phase == "ready"
                assert snapshot.opened
                assert snapshot.baud == 1000000
                assert snapshot.hello_ack == (4, 1, 2, 0)
        finally:
            unsubscribe()
        deadline = time.monotonic() + 2.0
        while (
            not any(frame[1] == T_STATE for frame in wire.written)
            and time.monotonic() < deadline
        ):
            time.sleep(0.005)
        assert any(frame[1] == T_STATE for frame in wire.written)
    finally:
        wire.ack_allowed.set()
        service.shutdown()


def test_connect_legacy_skips_hello(monkeypatch) -> None:
    """従来方式の実際の書込を観察し、BCON バイナリを混ぜない。"""
    from core.transport.text_serial import TextSerialTransport

    class Wire:
        is_open = True

        def __init__(self) -> None:
            self.written: list[bytes] = []

        @property
        def in_waiting(self) -> int:
            return 0

        def read(self, n: int = 1) -> bytes:
            return b""

        def write(self, data: bytes) -> int:
            self.written.append(bytes(data))
            return len(data)

        def close(self) -> None:
            self.is_open = False

    wire = Wire()
    monkeypatch.setattr(
        "core.transport.text_serial.serial.Serial", lambda *args, **kwargs: wire
    )
    service, _ = make_service()
    service.sender = Sender.Sender(
        is_show_serial=False, transport=TextSerialTransport()
    )
    try:
        assert service.connect(0, "fake", 9600, False, "")[0]
        service.sender.writeRow("10 08")
        service.sender.flushPending()
        assert wire.written
        assert all(not frame.startswith(b"\xab") for frame in wire.written)
        assert any(b"10 08" in frame for frame in wire.written)
        assert service.connection_snapshot().phase == "ready"
        assert service.connection_snapshot().hello_ack is None
    finally:
        service.shutdown()


def test_connect_failure() -> None:
    service, notices = make_service()
    service.build_sender(make_spec())
    # 実機が無いので開けない。例外なく (False, False) で戻る
    connected, keyboard_active = service.connect(
        port_num=9999,
        port_name="",
        baud=9600,
        keyboard_enabled=True,
        setting_path="",
    )
    assert (connected, keyboard_active) == (False, False)
    assert any("開けませんでした" in m for m in notices)


def test_keyboard_without_connection() -> None:
    service, _ = make_service()
    service.sender = Sender.Sender(is_show_serial=False, transport=FakeTransport())
    # 未接続（key_press なし）では理由を返して断る
    err = service.set_keyboard_enabled(True, "")
    assert err is not None
    assert service.keyboard is None
    # 切る側は常に成功
    assert service.set_keyboard_enabled(False, "") is None


def test_shutdown() -> None:
    service, _ = make_service()
    service.sender = Sender.Sender(is_show_serial=False, transport=FakeTransport())
    assert service.shutdown() is False  # 開いていない
    assert service.request_connect(0, "fake", 9600, False, "") is False
    service, _ = make_service()
    service.sender = Sender.Sender(is_show_serial=False, transport=FakeTransport())
    service.connect(
        port_num=0,
        port_name="fake",
        baud=9600,
        keyboard_enabled=False,
        setting_path="",
    )
    assert service.shutdown() is True  # 閉じた


def test_load_plugins_noop() -> None:
    service, notices = make_service()
    service.load_plugins("")
    service.load_plugins("no_such_dir_at_all")
    assert notices == []


def test_run_diag_wire_and_stats(tmp_path: object) -> None:
    from pathlib import Path

    log_dir = Path(str(tmp_path))
    service, _ = make_service()
    service.set_log_dir(str(log_dir))
    service.sender = Sender.Sender(is_show_serial=False, transport=FakeTransport())
    service.begin_command_run("Fake", "pico")
    assert service.sender is not None
    transport = service.sender.transport
    # Pico経路は入力ログへ繋がないため聞き手を外す（S行で落ちる偽物の癖除け）。
    transport._listeners.clear()
    transport.send_row("S 4 8 80 80 80 80")
    transport.send_row("S 0 8 80 80 80 80")
    summary = service.end_command_run("手動停止")
    assert summary["rows"] == 2
    assert summary["reason"] == "手動停止"
    assert os.path.isfile(summary["csv_path"])
    assert "dropped" in summary["live_delta"]
    thread = summary.get("m_thread")
    if thread is not None:
        thread.join(timeout=5.0)
    # 記録解除後は追記されない（CSVは2行のまま）
    with open(summary["csv_path"], encoding="utf-8") as f:
        assert len(f.readlines()) == 3  # 見出し＋2行


def test_run_diag_m_counts_with_canned(tmp_path: object) -> None:
    from pathlib import Path

    service, _ = make_service()
    service.set_log_dir(str(Path(str(tmp_path))))
    service.sender = Sender.Sender(is_show_serial=False, transport=CannedMonTransport())
    service.begin_command_run("Fake", "pico")
    summary = service.end_command_run("完了")
    thread = summary.get("m_thread")
    if thread is not None:
        thread.join(timeout=5.0)
    assert service.last_m == {"press": 5, "ok": 10, "ng": 1}


def test_run_diag_without_sender_never_raises(tmp_path: object) -> None:
    from pathlib import Path

    service, _ = make_service()
    service.set_log_dir(str(Path(str(tmp_path))))
    service.begin_command_run("Fake", "pico")
    summary = service.end_command_run("完了")
    assert summary["rows"] == 0
    thread = summary.get("m_thread")
    if thread is not None:
        thread.join(timeout=5.0)


def test_apply_input_log_no_crash() -> None:
    service, _ = make_service()
    service.apply_input_log("simple", "", True, False)  # sender 無しは何もしない
    service.build_sender(make_spec())
    service.apply_input_log("simple", "", True, False)
    service.set_input_log_enabled(False)


def test_set_show_serial_pushes_the_value_into_the_sender() -> None:
    """表示の切替を、画面から送信側へ押し込めること。

    Tk 変数を送信スレッドに読ませると、GUI が忙しい間そこで待たされる。
    画面側が変わった時に値を渡し、送信側はそれを使う。

    Given: 表示しない側で作った Sender を持つサービス
    When : 表示の切替を押し込む
    Then : 送信側（GUI 以外のスレッドから見た値）がそれに従う
    """
    service, _ = make_service()
    service.sender = Sender.Sender(is_show_serial=False, transport=FakeTransport())
    assert service.sender is not None

    service.set_show_serial(True)
    assert service.sender._should_show_serial() is True
    service.set_show_serial(False)
    assert service.sender._should_show_serial() is False


def test_set_show_serial_without_sender_never_raises() -> None:
    """送信側がまだ無いとき（起動直後）でも落ちない。

    Given: Sender を持たないサービス
    When : 表示の切替を押し込む
    Then : 例外を出さずに戻る
    """
    service, _ = make_service()
    service.set_show_serial(True)
    service.set_show_serial(False)
    assert service.sender is None
    service.set_arbitration("off", 2.0)


def test_bcon_connect_stop_failure_never_sends_hello() -> None:
    from services.bcon_connect import connect_bcon

    class Target:
        capability = "BCON_STATE"

        def __init__(self) -> None:
            self.held = False
            self.hello_calls = 0

        def is_open(self) -> bool:
            return True

        def hold_input(self, hold: bool) -> bool:
            self.held = hold
            return True

        def stop_live_loop(self) -> bool:
            return False

        def hello(self, timeout: float = 3.0, quiet: bool = False) -> bool:
            self.hello_calls += 1
            return True

    target = Target()
    assert not connect_bcon(target, lambda text: None)
    assert target.held
    assert target.hello_calls == 0


def test_bcon_connect_sender_resume_failure_reholds() -> None:
    from services.bcon_connect import connect_bcon

    class Target:
        capability = "BCON_STATE"

        def __init__(self) -> None:
            self.held = False
            self.loop = False

        def is_open(self) -> bool:
            return True

        def hold_input(self, hold: bool) -> bool:
            self.held = hold
            return True

        def stop_live_loop(self) -> bool:
            self.loop = False
            return True

        def hello(self, timeout: float = 3.0, quiet: bool = False) -> bool:
            return True

        def start_live_loop(self) -> bool:
            self.loop = True
            return True

    class SenderDouble:
        def stopLiveWorker(self) -> bool:
            return True

        def discardLive(self) -> bool:
            return False

        def releaseAll(self) -> None:
            pass

        def startLiveWorker(self) -> bool:
            return False

    target = Target()
    assert not connect_bcon(target, lambda text: None, SenderDouble())
    assert target.held
    assert not target.loop


def test_script_reopen_bcon_requires_hello_before_success() -> None:
    from types import SimpleNamespace

    from Commands.PythonCommandBase import PythonCommand

    class Target:
        capability = "BCON_STATE"

        def __init__(self, accepted: bool) -> None:
            self.accepted = accepted
            self.hello_timeouts: list[float] = []

        def hello(self, timeout: float = 3.0) -> bool:
            self.hello_timeouts.append(timeout)
            return self.accepted

    class SenderDouble:
        def __init__(self, target: Target) -> None:
            self.transport = target
            self.opened = False
            self.resumed = 0
            self.closed = 0

        def isOpened(self) -> bool:
            return self.opened

        def openSerial(self, port: int, name: str, baud: int) -> bool:
            self.opened = True
            return True

        def closeSerial(self) -> None:
            self.opened = False
            self.closed += 1

        def startLiveWorker(self) -> bool:
            self.resumed += 1
            return True

    for accepted in (False, True):
        target = Target(accepted)
        sender = SenderDouble(target)
        command = SimpleNamespace(
            keys=SimpleNamespace(ser=sender),
            _serialConfig=lambda: {
                "com_port": 1,
                "com_port_name": "fake",
                "baud_rate": 1000000,
            },
            wait=lambda seconds: None,
        )
        assert PythonCommand.reload_com_port(command, retry=1) is accepted
        assert target.hello_timeouts == [3.0]
        assert sender.opened is accepted
        assert sender.resumed == int(accepted)
        assert sender.closed == int(not accepted)


def test_transport_switch_completion_accepts_destination_baud_reconnect() -> None:
    """切替完了通知では受付が空いており、移動先速度を GUI が渡せる。"""

    class Destination(FakeTransport):
        name = "switch_destination_test"

        def __init__(self) -> None:
            super().__init__()
            self.opens: list[tuple[str, int]] = []

        def open(
            self, portNum: int, portName: str = "", baudrate: int = 9600, **extra
        ) -> bool:
            self.opens.append((portName, baudrate))
            return super().open(portNum, portName, baudrate, **extra)

    assert Transport.register_transport(Destination.name, Destination)
    service, _ = make_service()
    service.sender = Sender.Sender(is_show_serial=False, transport=FakeTransport())
    ready = threading.Event()
    accepted: list[bool] = []
    try:
        assert service.connect(9, "COM9", 9600, False, "")[0]

        def changed(snapshot) -> None:
            if snapshot.transport_name != Destination.name:
                return
            if snapshot.phase == "disconnected" and snapshot.operation is None:
                accepted.append(service.request_connect(9, "COM9", 2000000, False, ""))
            elif snapshot.phase == "ready":
                ready.set()

        service.subscribe_connection(changed)
        assert service.request_switch_transport(Destination.name)
        assert ready.wait(3.0)
        assert accepted == [True]
        assert service.sender.transport.opens == [("COM9", 2000000)]
    finally:
        service.shutdown()
        Transport.unregister_transport(Destination.name)


def test_transport_switch_does_not_open_a_closed_line() -> None:
    class Destination(FakeTransport):
        name = "closed_switch_destination_test"

        def __init__(self) -> None:
            super().__init__()
            self.opens = 0

        def open(self, *args, **kwargs) -> bool:
            self.opens += 1
            return super().open(*args, **kwargs)

    assert Transport.register_transport(Destination.name, Destination)
    service, _ = make_service()
    service.sender = Sender.Sender(is_show_serial=False, transport=FakeTransport())
    try:
        assert service.switch_transport(Destination.name)[0]
        assert service.connection_snapshot().phase == "disconnected"
        assert service.sender.transport.opens == 0
    finally:
        service.shutdown()
        Transport.unregister_transport(Destination.name)


def test_terminal_completion_can_reconnect_after_failure_and_disconnect() -> None:
    """失敗と切断の完了通知から同じ線へ再接続できる。"""

    class FirstOpenFails(FakeTransport):
        def __init__(self) -> None:
            super().__init__()
            self.opens = 0

        def open(self, *args, **kwargs) -> bool:
            self.opens += 1
            if self.opens == 1:
                return False
            return super().open(*args, **kwargs)

    service, _ = make_service()
    line = FirstOpenFails()
    service.sender = Sender.Sender(is_show_serial=False, transport=line)
    done = threading.Event()
    accepted: list[bool] = []
    stages: list[str] = []

    def completed(snapshot) -> None:
        if snapshot.operation is not None or snapshot.generation == 0:
            return
        if not stages and snapshot.phase == "failed":
            stages.append("failed")
            accepted.append(service.request_connect(9, "COM9", 9600, False, ""))
        elif stages == ["failed"] and snapshot.phase == "ready":
            stages.append("ready")
            assert service.key_press is not None
            service.disconnect()
        elif stages == ["failed", "ready"] and snapshot.phase == "disconnected":
            stages.append("disconnected")
            accepted.append(service.request_connect(9, "COM9", 9600, False, ""))
        elif (
            stages == ["failed", "ready", "disconnected"] and snapshot.phase == "ready"
        ):
            stages.append("ready")
            done.set()

    service.subscribe_connection(completed)
    try:
        assert service.request_connect(9, "COM9", 9600, False, "")
        assert done.wait(3.0)
        assert accepted == [True, True]
        assert stages == ["failed", "ready", "disconnected", "ready"]
        assert line.opens == 3
        snapshot = service.connection_snapshot()
        assert snapshot.phase == "ready"
        assert snapshot.operation is None
        assert snapshot.baud == 9600
        assert service.key_press is not None
        assert line.is_open()
    finally:
        service.shutdown()


def test_missing_establishment_api_fails_open_connection_and_releases_admission(
    monkeypatch,
):
    from core.transport.bcon import BconTransport
    from core.transport.bcon_protocol import T_HELLO, T_STATE

    class Wire:
        is_open = True
        baudrate = 1000000
        in_waiting = 0

        def __init__(self):
            self.written = []

        def read(self, n=1):
            return b""

        def write(self, data):
            self.written.append(bytes(data))
            return len(data)

        def close(self):
            self.is_open = False

    wire = Wire()
    monkeypatch.setattr("core.transport.bcon.serial.Serial", lambda **kwargs: wire)
    line = BconTransport()
    monkeypatch.setattr(line, "current_baud", None)
    service, _ = make_service()
    service.sender = Sender.Sender(is_show_serial=False, transport=line)
    failed = []
    done = threading.Event()
    accepted = []

    def completed(snapshot):
        if snapshot.phase == "failed" and snapshot.operation is None:
            failed.append(snapshot)
            accepted.append(service.request_switch_transport("legacy_text"))
        elif snapshot.phase == "disconnected" and failed:
            done.set()

    service.subscribe_connection(completed)
    try:
        assert service.connect(0, "fake", 1000000, False, "") == (False, False)
        assert done.wait(3.0)
        assert accepted == [True]
        assert len(failed) == 1
        assert failed[0].opened
        assert failed[0].baud is None
        assert failed[0].hello_ack is None
        assert failed[0].status is None
        assert failed[0].player_info == b""
        assert failed[0].rumble == b""
        assert not any(frame[1] in {T_HELLO, T_STATE} for frame in wire.written)
        assert not line.live_loop_running()
        assert service.connection_snapshot().phase == "disconnected"
    finally:
        service.shutdown()
