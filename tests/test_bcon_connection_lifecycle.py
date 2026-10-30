"""接続の外部結果を実 BCON と制御可能なシリアル線で確認する。"""

from collections.abc import Callable
from threading import Event, Lock, Thread
from typing import Any

import pytest
from core import Sender
from core.connection_state import ConnectionSnapshot
from core.transport.bcon import BconTransport
from core.transport.bcon_protocol import (
    T_BAUD_SET,
    T_HELLO,
    T_HELLO_ACK,
    T_IMU_SAMPLE,
    T_PLAYER_INFO,
    T_RUMBLE,
    T_STATE,
    T_STATUS,
    frame_build,
)
from services.serial_service import SerialService


class ControlledWire:
    """応答投入と送信観測を Event で同期する。実ポートは使わない。"""

    def __init__(self, baudrate: int = 1000000) -> None:
        self.baudrate = baudrate
        self.is_open = True
        self.hello_seen = Event()
        self.input_seen = Event()
        self.written: list[bytes] = []
        self._pending = bytearray()
        self._lock = Lock()

    @property
    def in_waiting(self) -> int:
        with self._lock:
            return len(self._pending)

    def read(self, size: int = 1) -> bytes:
        with self._lock:
            data = bytes(self._pending[:size])
            del self._pending[:size]
            return data

    def write(self, data: bytes) -> int:
        with self._lock:
            self.written.append(bytes(data))
        if len(data) >= 2 and data[1] == T_HELLO:
            self.hello_seen.set()
        if len(data) >= 2 and data[1] in (T_STATE, T_IMU_SAMPLE):
            self.input_seen.set()
        return len(data)

    def inject(self, kind: int, payload: bytes) -> None:
        with self._lock:
            self._pending.extend(frame_build(kind, payload, 0))

    def close(self) -> None:
        self.is_open = False


def make_service(
    monkeypatch: pytest.MonkeyPatch,
    *,
    proc: bool = False,
) -> tuple[SerialService, Any, list[ControlledWire]]:
    wires: list[ControlledWire] = []

    def open_wire(**kwargs: Any) -> ControlledWire:
        wire = ControlledWire(kwargs["baudrate"])
        wires.append(wire)
        return wire

    monkeypatch.setattr("core.transport.bcon.serial.Serial", open_wire)
    service = SerialService(
        notify_user=lambda text: None, base_dir=".", input_log_emit=None
    )
    if proc:
        from core.transport.bcon_proc import BconProcTransport
        from test_bcon_proc import _fake_spawn

        transport: Any = BconProcTransport(_spawn=_fake_spawn)
    else:
        transport = BconTransport()
    service.sender = Sender.Sender(is_show_serial=False, transport=transport)
    return service, transport, wires


def connect_in_thread(service: SerialService, baud: int = 1000000) -> Thread:
    thread = Thread(target=lambda: service.connect(0, "test-port", baud, False, ""))
    thread.start()
    return thread


def wait_snapshot(
    service: SerialService, predicate: Callable[[ConnectionSnapshot], bool]
) -> None:
    reached = Event()
    unsubscribe = service.subscribe_connection(
        lambda snapshot: reached.set() if predicate(snapshot) else None
    )
    try:
        assert reached.wait(5), service.connection_snapshot()
    finally:
        unsubscribe()


@pytest.mark.parametrize("ack", [bytes([4, 8, 9, 0]), bytes([3, 8, 9, 2]), None])
@pytest.mark.parametrize("proc", [False, True], ids=["native", "proc"])
def test_real_wire_blocks_input_until_compatible_ack(
    monkeypatch: pytest.MonkeyPatch, ack: bytes | None, proc: bool
) -> None:
    service, transport, wires = make_service(monkeypatch, proc=proc)
    thread = connect_in_thread(service, baud=2000000)
    try:
        wait_snapshot(service, lambda snapshot: snapshot.phase == "hello")
        wire = wires[0]
        assert wire.hello_seen.wait(5)
        assert wire.baudrate == 2000000
        transport.send_row("10 08")
        if not proc:
            transport.start_live_loop()
        transport.send_imu(0, 0, 0, 0, 0, 0)
        assert not wire.input_seen.is_set()
        if ack is not None:
            wire.inject(T_HELLO_ACK, ack)
        thread.join(5)
        assert not thread.is_alive()
        snapshot = service.connection_snapshot()
        if ack is not None and ack[0] == 4:
            assert snapshot.phase == "ready"
            assert snapshot.hello_ack == (4, 8, 9, 0)
            assert snapshot.baud == 2000000
            assert transport.live_loop_running()
            assert service.sender is not None
            assert service.sender.isLiveWorkerRunning()
            transport.send_row("10 08")
            assert wire.input_seen.wait(5)
        else:
            assert snapshot.phase == "failed"
            assert snapshot.baud is None
            transport.send_row("10 08")
            transport.send_imu(0, 0, 0, 0, 0, 0)
            # proc の IMU は返信不要。後続の会話完了を配送済みの境界にする。
            transport.current_baud()
            assert not wire.input_seen.is_set()
    finally:
        if wires and ack is not None:
            wires[0].inject(T_HELLO_ACK, ack)
        thread.join(5)
        service.shutdown()


@pytest.mark.parametrize("proc", [False, True], ids=["native", "proc"])
def test_open_error_preserves_actual_reason_and_never_claims_ready(
    monkeypatch: pytest.MonkeyPatch, proc: bool
) -> None:
    service, _, _ = make_service(monkeypatch, proc=proc)

    def unavailable(**kwargs: Any) -> Any:
        raise OSError("USB device unavailable")

    monkeypatch.setattr("core.transport.bcon.serial.Serial", unavailable)
    try:
        assert service.connect(0, "test-port", 1000000, False, "") == (False, False)
        snapshot = service.connection_snapshot()
        assert snapshot.phase == "failed"
        assert not snapshot.opened
        assert snapshot.hello_ack is snapshot.status is snapshot.baud is None
        assert "USB device unavailable" in snapshot.message
    finally:
        service.shutdown()


def establish(service: SerialService, wires: list[ControlledWire]) -> None:
    thread = connect_in_thread(service)
    try:
        wait_snapshot(service, lambda snapshot: snapshot.phase == "hello")
        assert wires[-1].hello_seen.wait(5)
        wires[-1].inject(T_HELLO_ACK, bytes([4, 1, 0, 0]))
    finally:
        thread.join(5)
    assert not thread.is_alive()
    assert service.connection_snapshot().phase == "ready"


def test_status_decodes_real_len7_without_periodic_log_flood(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, transport, wires = make_service(monkeypatch)
    logs: list[tuple[int, str]] = []
    service.subscribe_connection_log(
        lambda generation, text: logs.append((generation, text))
    )
    try:
        establish(service, wires)
        log_count = len(logs)
        for sequence in range(4):

            def status_matches(snapshot: ConnectionSnapshot) -> bool:
                return snapshot.status == (3, sequence, 0x1234, 0x5678, 5)

            wires[0].inject(T_STATUS, bytes([3, sequence, 0x34, 0x12, 0x78, 0x56, 5]))
            wait_snapshot(service, status_matches)
        before = service.connection_snapshot()
        received = Event()
        unsubscribe = transport.subscribe_rx(
            lambda frame: received.set() if frame[0] == T_PLAYER_INFO else None
        )
        try:
            # 後続の有効フレームまで通過したことを確認し、不正長の処理待ちをなくす。
            wires[0].inject(T_STATUS, b"\x00" * 6)
            wires[0].inject(T_PLAYER_INFO, b"\x01\x02")
            assert received.wait(5)
        finally:
            unsubscribe()
        after = service.connection_snapshot()
        assert after.status == before.status
        assert after.status_received_at == before.status_received_at
        assert after.player_info == b"\x01\x02"
        assert len(logs) == log_count
        service.disconnect()
        wait_snapshot(service, lambda snapshot: snapshot.phase == "disconnected")
        cleared = service.connection_snapshot()
        assert cleared.status is None
        assert cleared.status_received_at is None
        assert cleared.player_info == cleared.rumble == b""
    finally:
        service.shutdown()


def test_old_subscription_cannot_mutate_reused_transport_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, transport, wires = make_service(monkeypatch)
    subscriptions: list[Callable[..., Any]] = []
    subscribe = transport.subscribe_rx

    def remember(callback: Callable[..., Any]) -> Callable[[], None]:
        subscriptions.append(callback)
        return subscribe(callback)

    monkeypatch.setattr(transport, "subscribe_rx", remember)
    logs: list[tuple[int, str]] = []
    service.subscribe_connection_log(
        lambda generation, text: logs.append((generation, text))
    )
    try:
        first = connect_in_thread(service)
        wait_snapshot(service, lambda snapshot: snapshot.phase == "hello")
        assert wires[0].hello_seen.wait(5)
        old_receive = subscriptions[0]
        old_generation = service.connection_snapshot().generation
        service.disconnect()
        assert service.connection_snapshot().generation > old_generation
        log_boundary = len(logs)
        wires[0].inject(T_HELLO_ACK, bytes([4, 99, 99, 0]))
        wires[0].inject(T_STATUS, bytes([3, 1, 0, 0, 0, 0, 0]))
        first.join(5)
        assert not first.is_alive()
        wait_snapshot(service, lambda snapshot: snapshot.phase == "disconnected")
        assert not wires[0].input_seen.is_set()
        assert all(
            generation != old_generation for generation, _ in logs[log_boundary:]
        )
        thread = connect_in_thread(service)
        try:
            wait_snapshot(service, lambda snapshot: snapshot.phase == "hello")
            assert wires[-1].hello_seen.wait(5)
            before = service.connection_snapshot()
            # 解除前に配送側が取り出していた callback を、解除後に実行する競合。
            for kind, payload in (
                (T_HELLO_ACK, bytes([4, 99, 99, 0])),
                (T_STATUS, bytes([3, 1, 0, 0, 0, 0, 0])),
                (T_PLAYER_INFO, b"\xff\xff"),
                (T_RUMBLE, b"\xff\xff"),
            ):
                old_receive((kind, payload, 0))
            assert service.connection_snapshot() == before
            assert before.phase == "hello"
            assert not wires[-1].input_seen.is_set()
            wires[-1].inject(T_HELLO_ACK, bytes([4, 2, 3, 0]))
        finally:
            thread.join(5)
        assert not thread.is_alive()
        assert service.connection_snapshot().hello_ack == (4, 2, 3, 0)
        assert service.sender is not None and service.sender.transport is transport
    finally:
        service.shutdown()


def test_held_hello_rejects_parallel_baud_and_hunt_then_disconnect_cancels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, transport, wires = make_service(monkeypatch)
    logs: list[str] = []
    service.subscribe_connection_log(lambda generation, text: logs.append(text))
    try:
        establish(service, wires)
        wires[0].hello_seen.clear()
        assert service.retry_bcon_hello()
        assert wires[0].hello_seen.wait(5)
        log_count = len(logs)
        written_count = len(wires[0].written)
        assert not service.change_bcon_baud(4)
        assert not service.hunt_bcon_baud()
        assert len(logs) > log_count
        assert not any(
            frame[1] == T_BAUD_SET for frame in wires[0].written[written_count:]
        )
        before_disconnect = service.connection_snapshot().generation
        service.disconnect()
        assert service.connection_snapshot().generation > before_disconnect
        wires[0].inject(T_HELLO_ACK, bytes([4, 1, 0, 0]))
        wait_snapshot(service, lambda snapshot: snapshot.phase == "disconnected")
        assert not transport.live_loop_running()
        assert not transport.send_imu(0, 0, 0, 0, 0, 0)
        assert service.connection_snapshot().hello_ack is None
    finally:
        service.shutdown()


def test_sender_that_cannot_stop_prevents_retry_ready_or_new_hello(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, transport, wires = make_service(monkeypatch)
    try:
        establish(service, wires)
        hello_count = sum(frame[1] == T_HELLO for frame in wires[0].written)
        assert service.sender is not None
        monkeypatch.setattr(service.sender, "stopLiveWorker", lambda: False)
        assert service.retry_bcon_hello()
        wait_snapshot(service, lambda snapshot: snapshot.phase == "failed")
        assert sum(frame[1] == T_HELLO for frame in wires[0].written) == hello_count
        assert not transport.send_imu(0, 0, 0, 0, 0, 0)
        assert service.connection_snapshot().baud is None
    finally:
        # 本物の worker の終了口は後始末で元へ戻す。
        monkeypatch.undo()
        service.shutdown()


@pytest.mark.parametrize("old_rate_ack", [True, False], ids=["confirmed", "unknown"])
def test_failed_baud_change_requires_new_hello_to_confirm_old_rate(
    monkeypatch: pytest.MonkeyPatch, old_rate_ack: bool
) -> None:
    service, transport, wires = make_service(monkeypatch)
    try:
        establish(service, wires)
        monkeypatch.setattr(transport, "set_baud_index", lambda index: False)
        wires[0].hello_seen.clear()
        assert service.change_bcon_baud(4)
        assert wires[0].hello_seen.wait(5)
        assert service.connection_snapshot().baud is None
        if old_rate_ack:
            wires[0].inject(T_HELLO_ACK, bytes([4, 1, 0, 0]))
        wait_snapshot(service, lambda snapshot: snapshot.phase in {"ready", "failed"})
        snapshot = service.connection_snapshot()
        assert snapshot.phase == ("ready" if old_rate_ack else "failed")
        assert snapshot.baud == (1000000 if old_rate_ack else None)
        if not old_rate_ack:
            assert not transport.send_imu(0, 0, 0, 0, 0, 0)
    finally:
        service.shutdown()
