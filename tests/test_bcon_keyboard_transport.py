"""実 Transport の wire・共有 SEQ・抑止・失敗を疑似シリアルで調べる。"""

import threading
from typing import Any

import pytest
from fakes import BconKeyboardSerial
from pokecon.domain.serial.bcon_keyboard import NEUTRAL, report
from pokecon.domain.transport.bcon import BconTransport
from pokecon.domain.transport.bcon_proc import BconProcTransport
from pokecon.domain.transport.bcon_protocol import (
    T_IMU_SAMPLE,
    T_KEYBOARD,
    T_PING,
    T_STATE,
    BconParser,
)
from test_bcon_proc import _fake_spawn


def ready() -> tuple[BconTransport, BconKeyboardSerial]:
    transport = BconTransport()
    wire = BconKeyboardSerial()
    transport.ser = wire
    transport._hello_established = True
    return transport, wire


def test_keyboard_writes_one_frame_without_sender_hooks_or_live_state() -> None:
    transport, wire = ready()
    hooks: list[Any] = []
    transport.set_hooks(lambda *a: hooks.append(a), lambda *a: hooks.append(a))
    transport.add_listener(hooks.append)
    before = (transport._live_pending, transport._live_latest, transport.live_stats())
    assert transport.send_keyboard(report(0, 4))
    assert wire.written == [bytes.fromhex("AB 3C 08 00 00 04 00 00 00 00 00 00 A6")]
    assert hooks == []
    assert (
        transport._live_pending,
        transport._live_latest,
        transport.live_stats(),
    ) == before


def test_keyboard_shares_sequence_with_state_imu_and_session() -> None:
    transport, wire = ready()
    transport._tx_seq = 254
    transport.send_row("10 08")
    assert transport.send_keyboard(report(2, 4))
    assert transport.send_imu(1, 2, 3, 4, 5, 6)
    assert transport._send_session_frame(T_PING, b"", "ping")
    frames = BconParser().feed(b"".join(wire.written))
    assert [(kind, seq) for kind, _, seq in frames] == [
        (T_STATE, 254),
        (T_KEYBOARD, 255),
        (T_IMU_SAMPLE, 0),
        (T_PING, 1),
    ]


@pytest.mark.parametrize(
    "flag", ["_tx_hold", "_input_hold", "_hello_established", "ser"]
)
def test_keyboard_is_suppressed_without_changing_sequence(flag: str) -> None:
    transport, wire = ready()
    setattr(transport, flag, None if flag == "ser" else flag != "_hello_established")
    assert not transport.send_keyboard(NEUTRAL)
    assert wire.written == []
    assert transport._tx_seq == 0


@pytest.mark.parametrize(
    "body", [b"", bytes(7), bytes(9), None, 8, "12345678", [256] * 8]
)
@pytest.mark.parametrize("proc", [False, True])
def test_bad_report_is_rejected_without_writes(body: Any, proc: bool) -> None:
    transport: Any
    transport, wire = ready()
    if proc:
        transport = BconProcTransport()
    assert transport.send_keyboard(body) is False
    assert wire.written == []


@pytest.mark.parametrize("raises", [False, True])
def test_short_write_and_exception_return_false(
    monkeypatch: pytest.MonkeyPatch, raises: bool
) -> None:
    transport, wire = ready()

    def fail(data: bytes) -> int:
        if raises:
            raise OSError("切断")
        return len(data) - 1

    monkeypatch.setattr(wire, "write", fail)
    assert not transport.send_keyboard(NEUTRAL)
    assert transport._tx_seq == 1


def test_concurrent_keyboard_and_state_keep_wire_sequence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport, wire = ready()
    entered, release, second_write = (
        threading.Event(),
        threading.Event(),
        threading.Event(),
    )

    class ObservedLock:
        """2人目の取得試行をイベントで通知する write lock。"""

        def __init__(self) -> None:
            self._lock = threading.Lock()
            self.attempted = threading.Event()

        def __enter__(self) -> "ObservedLock":
            if not self._lock.acquire(blocking=False):
                self.attempted.set()
                self._lock.acquire()
            return self

        def __exit__(self, *args: object) -> None:
            self._lock.release()

    observed = ObservedLock()
    transport._write_lock = observed  # type: ignore[assignment]
    original = wire.write

    def write(data: bytes) -> int:
        if data[1] == T_KEYBOARD:
            entered.set()
            assert release.wait(5)
        else:
            second_write.set()
        return original(data)

    monkeypatch.setattr(wire, "write", write)
    first = threading.Thread(target=lambda: transport.send_keyboard(report(0, 4)))
    second = threading.Thread(target=lambda: transport.send_row("10 08"))
    first.start()
    try:
        assert entered.wait(5)
        second.start()
        # 時計ではなく lock の待機イベントで「2人目が入れない」を固定する。
        assert observed.attempted.wait(5)
        assert not second_write.is_set()
    finally:
        release.set()
        first.join(5)
        if second.ident is not None:
            second.join(5)
    assert not first.is_alive() and not second.is_alive()
    assert [(frame[1], frame[-2]) for frame in wire.written] == [
        (T_KEYBOARD, 0),
        (T_STATE, 1),
    ]


@pytest.mark.parametrize("proc", [False, True], ids=["native", "proc"])
def test_firmware_minor_13_and_actual_keyboard_wire(
    monkeypatch: pytest.MonkeyPatch, proc: bool
) -> None:
    wire = BconKeyboardSerial()
    monkeypatch.setattr("pokecon.domain.transport.bcon.serial.Serial", lambda **_: wire)
    transport = BconProcTransport(_spawn=_fake_spawn) if proc else BconTransport()
    try:
        assert transport.open(0, "fake-keyboard", 1000000)
        assert transport.hello()
        assert transport.hello_established()
        wire.written.clear()
        hooks: list[Any] = []
        transport.set_hooks(lambda *a: hooks.append(a), lambda *a: hooks.append(a))
        transport.add_listener(hooks.append)
        assert transport.send_keyboard(report(0, 4))
        assert transport.send_keyboard(NEUTRAL)
        # send_keyboard は線への write 結果まで返す（proc は子RPC、本家は同期write）。
        assert [
            (kind, body) for kind, body, _ in BconParser().feed(b"".join(wire.written))
        ] == [
            (T_KEYBOARD, report(0, 4)),
            (T_KEYBOARD, NEUTRAL),
        ]
        assert hooks == []
    finally:
        transport.close()


@pytest.mark.parametrize("proc", [False, True], ids=["native", "proc"])
def test_keyboard_returns_rejection_and_write_outcome(
    monkeypatch: pytest.MonkeyPatch, proc: bool
) -> None:
    """Given: 入力抑止中/書き込み失敗 / When: キーボードを送る / Then: 拒否と失敗がFalseで返ること。"""
    wire = BconKeyboardSerial()
    monkeypatch.setattr("pokecon.domain.transport.bcon.serial.Serial", lambda **_: wire)
    transport: Any = BconProcTransport(_spawn=_fake_spawn) if proc else BconTransport()
    try:
        assert transport.open(0, "fake-keyboard", 1000000)
        assert transport.hello()
        assert transport.hello_established()
        # 入力抑止中の送信は、通信層が実際に write しないので False。
        before = list(wire.written)
        assert transport.hold_input(True)
        assert transport.send_keyboard(report(0, 4)) is False
        assert wire.written == before
        assert transport.hold_input(False)
        assert transport.send_keyboard(report(0, 4)) is True
        # write が短く終わる場合も False（親はキュー投入ではなく結果を返す）。
        monkeypatch.setattr(wire, "write", lambda data: len(data) - 1)
        assert transport.send_keyboard(NEUTRAL) is False
    finally:
        transport.close()
