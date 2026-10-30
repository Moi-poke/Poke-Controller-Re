"""bcon計測カウンタのhostベクタ（実機不要）。

H1(RPC遅延)/H2(drain数+ms)/H3(rx率)切り分け用の素朴な数え。
int/float/perf_counterだけで、per-frameのI/Oはしない。
worker側（snapshotの受渡し・dispatch）からwidget・after()には触らない。
BconSetupとpanelは窓を作らず __new__＋stub で見る。
"""

import queue
import threading
import time
from types import SimpleNamespace
from typing import Any


def test_poll_counts_drained_and_wall_ms() -> None:
    from bcon_ui_support import setup

    obj = setup()
    obj.set_visible(True)
    for _ in range(3):
        obj._queue.put(("noop", ""))
    obj._poll()
    snapshot = obj.counters_snapshot()
    assert snapshot["poll_last_drained"] == 3
    assert snapshot["poll_drained_total"] == 3
    assert snapshot["poll_last_ms"] >= 0
    assert snapshot["queue_max_qsize"] >= 3


def test_latest_snapshot_is_coalesced_before_gui_drain() -> None:
    from bcon_ui_support import Service, setup

    service = Service()
    obj = setup(service)
    obj.set_visible(True)
    for generation in range(1, 100):
        service.publish(generation=generation)
    assert obj.inbox.pending() == 1
    obj.inbox.drain()
    assert obj._snapshot.generation == 99


def test_serial_summary_renders_memory_without_transport_access() -> None:
    from bcon_ui_support import Widget
    from core.connection_state import ConnectionSnapshot
    from ui.serial_panel import SerialPanelMixin

    class Serial:
        @property
        def sender(self) -> Any:
            raise AssertionError("GUIからtransportへ触れてはいけません")

        @staticmethod
        def uses_bcon(_name: str) -> bool:
            return True

    obj: Any = SerialPanelMixin.__new__(SerialPanelMixin)
    obj.serial = Serial()
    obj._connection_view = ConnectionSnapshot(
        opened=True,
        phase="ready",
        transport_name="switch-bcon",
        player_info=b"\x05\x03",
        rumble=b"\x0a\x05",
    )
    obj.com_port_name = SimpleNamespace(get=lambda: "COM3")
    obj.transport_name = SimpleNamespace(get=lambda: "switch-bcon")
    obj.bcon_wired = SimpleNamespace(set=lambda _value: None)
    obj._player_lamp_items = [0, 1, 2, 3]
    for name in (
        "connection_status",
        "connection_details",
        "player_lamp_canvas",
        "rumble_label",
    ):
        setattr(obj, name, Widget())
    obj._render_connection_summary()
    assert obj.player_lamp_canvas.values["0"]["fill"] == "green yellow"
    assert obj.player_lamp_canvas.values["1"]["fill"] == "gray"
    assert "L=10 R=5" in obj.rumble_label.values["text"]


def test_proc_call_latency_and_fanout(monkeypatch) -> None:
    """別過程RPCの往復msとrx配達数を残す（H1切り分け用）。"""
    import core.transport.bcon as _bcon
    from core.transport import bcon_proc

    class _FakeWorkerHandle:
        def __init__(self, entry: Any, cmd_q: Any, evt_q: Any, cfg: dict) -> None:
            self._thread = threading.Thread(
                target=entry, args=(cmd_q, evt_q, cfg), daemon=True
            )
            self._alive = False

        def start(self) -> None:
            self._alive = True
            self._thread.start()

        def is_alive(self) -> bool:
            return self._alive and self._thread.is_alive()

        def terminate(self) -> None:
            self._alive = False

        def join(self, timeout: float | None = None) -> None:
            self._thread.join(timeout)

    def _fake_spawn(entry: Any, cfg: dict) -> tuple[Any, Any, Any]:
        cmd_q: Any = queue.Queue()
        evt_q: Any = queue.Queue()
        return _FakeWorkerHandle(entry, cmd_q, evt_q, cfg), cmd_q, evt_q

    class _FakeSerial:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.is_open = True

        def read(self, n: int = 1) -> bytes:
            _ = n
            time.sleep(0.001)
            return b""

        @property
        def in_waiting(self) -> int:
            return 0

        def write(self, data: bytes) -> int:
            return len(data)

        def close(self) -> None:
            self.is_open = False

    monkeypatch.setattr(_bcon.serial, "Serial", _FakeSerial)
    made = bcon_proc.BconProcTransport(_spawn=_fake_spawn)
    try:
        assert made.open(3, "COM3", 1000000) is True
        ok, _result = made._call("last_player_info", (), timeout=3.0)
        assert ok is True
        stats = made.rpc_stats()
        assert stats["count"] >= 1
        assert stats["last_method"] == "last_player_info"
        assert stats["last_ms"] >= 0.0
        assert stats["max_ms"] >= stats["last_ms"]
        seen: list[Any] = []
        made.subscribe_rx(seen.append)
        made._evt_q.put(("rx", 0x23, b"\x05\x03", 7))
        deadline = time.monotonic() + 2.0
        while not seen and time.monotonic() < deadline:
            time.sleep(0.01)
        assert seen != []
        again = made.rpc_stats()
        assert again["rx_fanout_total"] >= 1
        assert again["rx_fanout_last"] >= 1
    finally:
        made.close()


# -- 送信数(sent)は線に出た分だけ・開き直しはSEQ口座を戻す（実機不要）--


class _CountingSer:
    """writeの結果だけ操作できる疑似線。1呼び出し=1試行。"""

    is_open = True

    def __init__(self, result: int = 0) -> None:
        self.result = result
        self.attempts = 0
        self.written: list[bytes] = []

    def write(self, data: bytes) -> int:
        self.attempts += 1
        if self.result > 0:
            self.written.append(bytes(data))
        return self.result

    def read(self, n: int) -> bytes:
        _ = n
        time.sleep(0.001)
        return b""

    def close(self) -> None:
        self.is_open = False


def test_bcon_sent_counts_only_successful_writes() -> None:
    """失敗writeでsentを増やさない（run-journalのsent差分を過大にしない）。"""
    from core.transport import create_transport

    made = create_transport("bcon")
    ser = _CountingSer(result=0)
    made.ser = ser
    made._hello_established = True
    before = made.live_stats()["sent"]
    made.send_row("10 08")
    assert ser.attempts == 1
    assert made.live_stats()["sent"] == before
    ser.result = 13  # STATE LEN8フレーム長（3+8+2）
    made.send_row("10 08")
    assert made.live_stats()["sent"] == before + 1


def test_bcon_live_loop_sent_counts_only_successful_writes() -> None:
    """120Hz loopも同じ。書けなかった刻みはsentに載せない。"""
    from core.transport import create_transport

    made = create_transport("bcon")
    ser = _CountingSer(result=0)
    made.ser = ser
    made._hello_established = True
    assert made.start_live_loop() is True
    try:
        made.send_row("10 08")
        deadline = time.monotonic() + 2.0
        while ser.attempts < 3 and time.monotonic() < deadline:
            time.sleep(0.005)
        assert ser.attempts >= 3
        assert made.live_stats()["sent"] == 0
    finally:
        made.stop_live_loop()


def test_bcon_open_resets_rx_seq_gap_accounting(monkeypatch: Any) -> None:
    """開き直しは新しい握手。再開後の最初のSEQを欠番に数えない。"""
    import core.transport.bcon as _bcon

    class _FakeSerial:
        is_open = True

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            _ = args, kwargs

        def write(self, data: bytes) -> int:
            return len(data)

        def read(self, n: int) -> bytes:
            _ = n
            return b""

        def close(self) -> None:
            self.is_open = False

    monkeypatch.setattr(_bcon.serial, "Serial", _FakeSerial)
    made = _bcon.BconTransport()
    assert made.open(0, "COM9", 1000000) is True
    made._note_rx_seq(200)
    assert made.open(0, "COM9", 1000000) is True
    made._note_rx_seq(0)
    assert made.live_stats()["seq_gap"] == 0
