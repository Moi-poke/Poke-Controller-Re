"""Rxポンプの headless 検証。実機なしで回す。

実使用の順序 (応答待ちの登録→応答の到着) を守る。ポンプは到着後の
行だけを届ける (過去は見ない) のが正しいため、仕込みは待機の登録後に
足す。
"""

import threading
import time

from fakes import NoSerTransport
from pokecon.domain import transport as Transport


class ScriptedSer:
    """read() で仕込みバイトを返す偽シリアル。無い間は空で返す。"""

    def __init__(self, chunks: list[bytes] | None = None) -> None:
        self._chunks = list(chunks or [])
        self._lock = threading.Lock()
        self.is_open = True
        self.written: list[bytes] = []
        self.resets = 0
        self.reply_on_write: list[bytes] = []

    def read(self, n: int) -> bytes:
        _ = n
        with self._lock:
            if self._chunks:
                return self._chunks.pop(0)
        time.sleep(0.001)
        return b""

    def feed(self, data: bytes) -> None:
        with self._lock:
            self._chunks.append(data)

    def write(self, data: bytes) -> int:
        self.written.append(data)
        with self._lock:
            self._chunks.extend(self.reply_on_write)
        return len(data)

    def reset_input_buffer(self) -> None:
        self.resets += 1
        with self._lock:
            self._chunks.clear()


def _pump_transport() -> tuple[Transport.TextSerialTransport, ScriptedSer]:
    t = Transport.TextSerialTransport()
    ser = ScriptedSer()
    t.ser = ser  # type: ignore[assignment]
    t.start_rx_pump()
    return (t, ser)


def _wait_in_thread(t: Transport.TextSerialTransport, out: dict) -> threading.Thread:
    def _wait() -> None:
        out["line"] = t.wait_rx(("PONG",), timeout=3.0)

    thread = threading.Thread(target=_wait, daemon=True)
    thread.start()
    return thread


def test_subscriber_receives_lines() -> None:
    t, ser = _pump_transport()
    try:
        got: list[str] = []
        unsub = t.subscribe_rx(got.append)
        try:
            out: dict[str, str | None] = {}
            waiter = _wait_in_thread(t, out)
            time.sleep(0.05)
            ser.feed(b"PONG\r\n")
            waiter.join(4.0)
            assert out.get("line") == "PONG"
        finally:
            unsub()
        assert "PONG" in got
    finally:
        t.stop_rx_pump()


def test_query_and_monitor_do_not_steal() -> None:
    t, ser = _pump_transport()
    try:
        got: list[str] = []
        unsub = t.subscribe_rx(got.append)
        try:
            out: dict[str, str | None] = {}
            waiter = _wait_in_thread(t, out)
            time.sleep(0.05)
            ser.feed(b"PONG\r\n")
            waiter.join(4.0)
            assert out.get("line") == "PONG"
        finally:
            unsub()
        assert got == ["PONG"]
    finally:
        t.stop_rx_pump()


def test_base_transport_has_no_rx() -> None:
    t = NoSerTransport()
    assert t.rx_pump_running() is False
    assert t.wait_rx(("PONG",), timeout=0.05) is None
    unsub = t.subscribe_rx(lambda line: None)
    unsub()


def test_wait_rx_receives_reply_alongside_subscriber() -> None:
    """送信後の応答はポンプ経由で wait_rx と購読者の双方へ届く。"""
    t, ser = _pump_transport()
    try:
        got: list[str] = []
        out: dict[str, str | None] = {}
        unsub = t.subscribe_rx(got.append)
        try:
            waiter = _wait_in_thread(t, out)
            time.sleep(0.05)
            ser.feed(b"PONG\r\n")
            waiter.join(4.0)
            assert out.get("line") == "PONG"
        finally:
            unsub()
        assert "PONG" in got
        assert ser.resets == 0
    finally:
        t.stop_rx_pump()
