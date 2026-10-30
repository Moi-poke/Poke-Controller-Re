"""診断 CLI が HELLO 成立後の実 BCON 入力経路だけを使うことを確認する。"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from core.transport.bcon import BconTransport
from core.transport.bcon_protocol import (
    T_HELLO,
    T_HELLO_ACK,
    T_IMU_SAMPLE,
    T_STATE,
    T_STATUS,
    T_STATUS_REQ,
    frame_build,
)


class _DiagnosticWire:
    """HELLO と STATUS に応答する制御可能な疑似シリアル線。"""

    def __init__(self, baudrate: int, hello_ack: bool) -> None:
        self.baudrate = baudrate
        self.hello_ack = hello_ack
        self.is_open = True
        self.closed = False
        self.written: list[bytes] = []
        self._incoming = bytearray()
        self._lock = threading.Lock()

    @property
    def in_waiting(self) -> int:
        with self._lock:
            return len(self._incoming)

    def read(self, size: int = 1) -> bytes:
        with self._lock:
            data = bytes(self._incoming[:size])
            del self._incoming[:size]
        if not data:
            time.sleep(0.001)
        return data

    def write(self, data: bytes) -> int:
        frame = bytes(data)
        with self._lock:
            self.written.append(frame)
        if len(frame) < 3 or frame[0] != 0xAB:
            return len(frame)
        if frame[1] == T_HELLO and self.hello_ack:
            self.inject(T_HELLO_ACK, bytes([4, 1, 0, 0]))
        elif frame[1] == T_STATUS_REQ:
            self.inject(T_STATUS, bytes(7))
        return len(frame)

    def inject(self, frame_type: int, payload: bytes) -> None:
        with self._lock:
            self._incoming.extend(frame_build(frame_type, payload, 0))

    def close(self) -> None:
        self.closed = True
        self.is_open = False


def _load_tool(name: str) -> Any:
    path = Path(__file__).resolve().parent.parent / "tools" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"test_{name}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _install_wire(
    monkeypatch: pytest.MonkeyPatch, *, hello_ack: bool = True
) -> list[_DiagnosticWire]:
    wires: list[_DiagnosticWire] = []

    def open_wire(*, baudrate: int, **kwargs: Any) -> _DiagnosticWire:
        _ = kwargs
        wire = _DiagnosticWire(baudrate, hello_ack)
        wires.append(wire)
        return wire

    monkeypatch.setattr("core.transport.bcon.serial.Serial", open_wire)
    return wires


def _wire_types(wire: _DiagnosticWire) -> list[int]:
    return [frame[1] for frame in wire.written if len(frame) >= 3 and frame[0] == 0xAB]


@pytest.mark.parametrize("failure", ["false", "exception"])
def test_bcon_jitter_stops_before_measurement_and_csv_on_hello_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, failure: str
) -> None:
    wires = _install_wire(monkeypatch, hello_ack=failure == "exception")
    original_hello = BconTransport.hello

    def failed_hello(self: BconTransport, timeout: float = 3.0) -> bool:
        _ = timeout
        if failure == "exception":
            raise OSError("HELLO transport error")
        return original_hello(self, timeout=0.02, quiet=True)

    monkeypatch.setattr(BconTransport, "hello", failed_hello)
    tool = _load_tool("bcon_jitter")
    csv_path = tmp_path / "must-not-exist.csv"
    stdout = io.StringIO()
    stderr = io.StringIO()

    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        result = tool._run(["--port", "FAKE", "--secs", "1", "--csv", str(csv_path)])

    assert result != 0
    assert "p50=" not in stdout.getvalue()
    assert not csv_path.exists()
    assert wires[0].closed
    assert T_STATE not in _wire_types(wires[0])


def test_bcon_jitter_reports_only_neutral_state_intervals_after_hello(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    wires = _install_wire(monkeypatch)
    tool = _load_tool("bcon_jitter")
    csv_path = tmp_path / "measurement.csv"
    stdout = io.StringIO()
    stderr = io.StringIO()

    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        result = tool._run(["--port", "FAKE", "--secs", "1", "--csv", str(csv_path)])

    types = _wire_types(wires[0])
    state_count = types.count(T_STATE)
    assert result == 0
    assert T_HELLO in types
    assert state_count > 5
    assert T_STATUS_REQ in types
    assert "sent=" in stdout.getvalue()
    content = csv_path.read_text(encoding="utf-8")
    assert "# no_hello=False\n" in content
    assert f"# samples={state_count - 1}\n" in content
    assert "# samples=" in content
    assert wires[0].closed


def test_bcon_jitter_does_not_report_when_live_loop_rejects_start(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    wires = _install_wire(monkeypatch)

    def reject_live_loop(self: BconTransport) -> bool:
        _ = self
        return False

    monkeypatch.setattr(BconTransport, "start_live_loop", reject_live_loop)
    tool = _load_tool("bcon_jitter")
    csv_path = tmp_path / "loop-not-started.csv"
    stdout = io.StringIO()
    stderr = io.StringIO()

    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        result = tool._run(["--port", "FAKE", "--secs", "1", "--csv", str(csv_path)])

    assert result != 0
    assert "p50=" not in stdout.getvalue()
    assert not csv_path.exists()
    assert T_HELLO in _wire_types(wires[0])
    assert wires[0].closed


def test_imu_send_stops_before_imu_frames_on_hello_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wires = _install_wire(monkeypatch, hello_ack=False)
    original_hello = BconTransport.hello

    def failed_hello(self: BconTransport, timeout: float = 3.0) -> bool:
        _ = timeout
        return original_hello(self, timeout=0.02, quiet=True)

    monkeypatch.setattr(BconTransport, "hello", failed_hello)
    tool = _load_tool("imu_send")
    monkeypatch.setattr(
        sys,
        "argv",
        ["imu_send.py", "--port", "FAKE", "--wave", "rest", "--secs", "0.01"],
    )

    assert tool.main() != 0
    types = _wire_types(wires[0])
    assert T_HELLO in types
    assert T_IMU_SAMPLE not in types
    assert wires[0].closed


def test_imu_send_uses_real_imu_path_after_hello_ack(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wires = _install_wire(monkeypatch)
    tool = _load_tool("imu_send")
    monkeypatch.setattr(
        sys,
        "argv",
        ["imu_send.py", "--port", "FAKE", "--wave", "rest", "--secs", "0.01"],
    )

    assert tool.main() == 0
    types = _wire_types(wires[0])
    assert T_HELLO in types
    assert T_IMU_SAMPLE in types
    assert wires[0].closed
