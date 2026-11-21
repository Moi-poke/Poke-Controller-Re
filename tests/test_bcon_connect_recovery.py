"""接続だけで探索・状態取得・入力再開まで進むことを実フレームで検証する。"""

import threading
import time

import pytest
from bcon_hunt_double import HuntingPicoSer
from pokecon.domain.serial.sender import Sender
from pokecon.domain.transport.bcon import BconTransport
from pokecon.domain.transport.bcon_proc import BconProcTransport
from pokecon.domain.transport.bcon_protocol import (
    T_BAUD_SET,
    T_CONTROLLER_INFO,
    T_HELLO,
    T_HELLO_ACK,
    T_NEUTRAL,
    T_PLAYER_INFO,
    T_STATE,
    T_STATUS,
    frame_build,
)
from pokecon.runtime.serial_service import SerialService


class StateWire(HuntingPicoSer):
    def __init__(self, *, hunting: bool, controller_info: bool = True) -> None:
        super().__init__((1000000, 115200))
        if not hunting:
            self.locked_rate = 1000000
        self.input_seen = threading.Event()
        self.controller_info = controller_info

    def _reply(self, kind: int, payload: bytes) -> None:
        super()._reply(kind, payload)
        if kind == T_STATUS:
            super()._reply(T_PLAYER_INFO, b"\x05\x03")
            if self.controller_info:
                super()._reply(T_CONTROLLER_INFO, b"\x02")

    def write(self, data: bytes) -> int:
        result = super().write(data)
        if data[1] == T_STATE and self.baudrate == self.locked_rate:
            self.input_seen.set()
        return result

    def close(self) -> None:
        self.is_open = False


def make_service(
    monkeypatch: pytest.MonkeyPatch, wire: StateWire, *, proc: bool = False
) -> SerialService:
    monkeypatch.setattr(
        "pokecon.domain.transport.bcon.serial.Serial", lambda **kwargs: wire
    )
    service = SerialService(
        notify_user=lambda text: None, base_dir=".", input_log_emit=None
    )
    if proc:
        from test_bcon_proc import _fake_spawn

        transport = BconProcTransport(_spawn=_fake_spawn)
    else:
        transport = BconTransport()
    service.sender = Sender(is_show_serial=False, transport=transport)
    return service


@pytest.mark.parametrize("hunting", [False, True], ids=["locked", "cold-boot"])
@pytest.mark.parametrize("proc", [False, True], ids=["native", "proc"])
def test_connect_at_1m_populates_state_and_allows_input_without_manual_actions(
    monkeypatch: pytest.MonkeyPatch, hunting: bool, proc: bool
) -> None:
    wire = StateWire(hunting=hunting)
    service = make_service(monkeypatch, wire, proc=proc)
    try:
        populated = threading.Event()
        service.subscribe_connection(
            lambda snapshot: populated.set() if snapshot.controller_role == 2 else None
        )
        assert service.connect(0, "test", 1000000, False, "")[0]
        assert populated.wait(1)
        snapshot = service.connection_snapshot()
        assert snapshot.phase == "ready" and snapshot.baud == 1000000
        assert snapshot.player_info == b"\x05\x03"
        assert snapshot.controller_role == 2
        assert service.sender is not None
        service.sender.transport.send_row("10 08")
        assert wire.input_seen.wait(1)
    finally:
        service.shutdown()


@pytest.mark.parametrize("proc", [False, True], ids=["native", "proc"])
def test_recovered_hunt_refetches_player_info_without_status_button(
    monkeypatch: pytest.MonkeyPatch, proc: bool
) -> None:
    wire = StateWire(hunting=False)
    service = make_service(monkeypatch, wire, proc=proc)
    complete = threading.Event()
    try:
        assert service.connect(0, "test", 1000000, False, "")[0]
        service.subscribe_connection(
            lambda snapshot: complete.set()
            if snapshot.phase == "ready"
            and snapshot.operation is None
            and snapshot.controller_role == 2
            else None
        )
        complete.clear()
        assert service.hunt_bcon_baud()
        assert complete.wait(5)
        snapshot = service.connection_snapshot()
        assert snapshot.player_info == b"\x05\x03"
        assert snapshot.controller_role == 2
    finally:
        service.shutdown()


def test_old_firmware_connects_without_controller_info(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wire = StateWire(hunting=False, controller_info=False)
    service = make_service(monkeypatch, wire)
    try:
        assert service.connect(0, "test", 1000000, False, "")[0]
        snapshot = service.connection_snapshot()
        assert snapshot.phase == "ready"
        assert snapshot.player_info == b"\x05\x03"
        assert snapshot.controller_role is None
    finally:
        service.shutdown()


def test_failed_candidate_recovers_original_confirmed_rate() -> None:
    wire = StateWire(hunting=False)
    transport = BconTransport()
    transport.ser = wire
    transport.start_rx_pump()
    try:
        assert transport.hello(timeout=0.1)
        assert transport.baud_hunt([115200], timeout_per=0.1) == 1000000
        assert transport.current_baud() == 1000000
        assert transport.hello_established()
        transport.send_row("10 08")
        assert wire.input_seen.wait(1)
    finally:
        transport.stop_rx_pump()


def test_auto_discovery_does_not_run_when_sender_cannot_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wire = StateWire(hunting=False)
    service = make_service(monkeypatch, wire)
    assert service.sender is not None
    monkeypatch.setattr(service.sender, "stopLiveWorker", lambda: False)
    try:
        assert not service.connect(0, "test", 1000000, False, "")[0]
        assert not any(frame[1] == T_HELLO for frame in wire.written)
        assert not wire.input_seen.is_set()
    finally:
        monkeypatch.undo()
        service.shutdown()


class GuardedBaudWire:
    """FWの100ms guardと2連続受信までの未確定期間を再現する。"""

    is_open = True

    def __init__(self) -> None:
        self.baudrate = 115200
        self.pending = bytearray()
        self.lock = threading.Lock()
        self.switch_at = float("inf")
        self.adopted = 0
        self.seq = 0

    @property
    def in_waiting(self) -> int:
        with self.lock:
            return len(self.pending)

    def read(self, count: int) -> bytes:
        with self.lock:
            data = bytes(self.pending[:count])
            del self.pending[:count]
            return data

    def write(self, data: bytes) -> int:
        with self.lock:
            kind = data[1]
            if kind == T_BAUD_SET and self.baudrate == 115200:
                # ACKとCore1側のguard開始は非同期。20ms遅れて切り替わる。
                self.switch_at = time.perf_counter() + 0.120
                self.pending.extend(frame_build(T_STATUS, bytes(7), self.seq))
                self.seq += 1
            elif self.baudrate == 1000000 and time.perf_counter() >= self.switch_at:
                if kind in (T_HELLO, T_NEUTRAL):
                    self.adopted += 1
                if kind == T_HELLO and self.adopted >= 2:
                    self.pending.extend(
                        frame_build(T_HELLO_ACK, b"\x04\x00\x10\x00", self.seq)
                    )
                    self.seq += 1
        return len(data)


def test_baud_change_confirms_two_frames_after_firmware_guard() -> None:
    wire = GuardedBaudWire()
    transport = BconTransport()
    transport.ser = wire
    transport.start_rx_pump()
    try:
        assert transport.set_baud_index(3, timeout=1.0)
        assert transport.current_baud() == 1000000
        assert wire.adopted >= 2
        assert transport.hello_established()
    finally:
        transport.stop_rx_pump()
