"""bcon別プロセス送出のhostベクタ（実機不要）。

子プロセスの代役（fake spawn＋本物の子mainをスレッド実行）で
proxy全系を決定的に検証する。実spawnはsmokeのみ。
"""

from __future__ import annotations

import queue
import threading
from typing import Any

import pytest
from pokecon.domain.transport.bcon_protocol import T_BEACON_START as _T_BEACON


class _FakeWorkerHandle:
    """multiprocessing.Processの代役。子mainをスレッドで回す。"""

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
    """pyserialの代役。開閉と空読みだけ行う。"""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.is_open = True
        self.written: list[bytes] = []

    def read(self, n: int = 1) -> bytes:
        import time as _time

        _time.sleep(0.001)
        return b""

    @property
    def in_waiting(self) -> int:
        return 0

    def write(self, data: bytes) -> int:
        self.written.append(bytes(data))
        return len(data)

    def close(self) -> None:
        self.is_open = False


def test_proc_proxy_opens_and_closes_with_fake_worker(monkeypatch):
    import pokecon.domain.transport.bcon as _bcon
    from pokecon.domain.transport import bcon_proc

    monkeypatch.setattr(_bcon.serial, "Serial", _FakeSerial)
    made = bcon_proc.BconProcTransport(_spawn=_fake_spawn)
    try:
        assert made.open(3, "COM3", 1000000) is True
        assert made.is_open() is True
    finally:
        made.close()
    assert made.is_open() is False


class _ScriptedSer(_FakeSerial):
    """応答を差し込める疑似シリアル。"""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        import threading as _threading

        self._chunks: list[bytes] = []
        self._lock = _threading.Lock()
        self.baudrate = 1000000

    def read(self, n: int = 1) -> bytes:
        with self._lock:
            if self._chunks:
                return self._chunks.pop(0)
        import time as _time

        _time.sleep(0.001)
        return b""

    @property
    def in_waiting(self) -> int:
        with self._lock:
            return sum(len(c) for c in self._chunks)

    def feed(self, data: bytes) -> None:
        with self._lock:
            self._chunks.append(bytes(data))

    def write(self, data: bytes) -> int:
        with self._lock:
            self.written.append(bytes(data))
        return len(data)


def _open_proc_with_scripted_ser(monkeypatch):
    import threading as _threading
    import time as _time

    import pokecon.domain.transport.bcon as _bcon
    from pokecon.domain.transport import bcon_proc
    from pokecon.domain.transport.bcon_protocol import (
        T_HELLO,
        T_HELLO_ACK,
        T_STATUS,
        frame_build,
    )

    # openのたび新しい線を渡す。使い回すと旧ワーカーのcloseが
    # 同一実体を閉じて再open後のis_openが偽になる（実物は毎回新規）。
    sers: list[_ScriptedSer] = []

    def _factory(*args: Any, **kwargs: Any) -> _ScriptedSer:
        ser = _ScriptedSer(*args, **kwargs)
        sers.append(ser)
        return ser

    monkeypatch.setattr(_bcon.serial, "Serial", _factory)
    made = bcon_proc.BconProcTransport(_spawn=_fake_spawn)
    assert made.open(3, "COM3", 1000000) is True
    stop = _threading.Event()

    def _frames() -> list[tuple[_ScriptedSer, bytes]]:
        out: list[tuple[_ScriptedSer, bytes]] = []
        for ser in list(sers):
            with ser._lock:
                out.extend((ser, f) for f in ser.written)
        return out

    def _responder() -> None:
        seen = 0
        while not stop.is_set():
            for ser, frame in _frames()[seen:]:
                seen += 1
                if len(frame) < 2 or frame[0] != 0xAB:
                    continue
                if frame[1] == T_HELLO:
                    ser.feed(
                        frame_build(T_HELLO_ACK, bytes([0x04, 0x00, 0x01, 0x00]), 0x41)
                    )
                elif frame[1] == _T_BEACON:
                    ser.feed(
                        frame_build(
                            T_STATUS,
                            bytes([0x40, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00]),
                            0x52,
                        )
                    )
                else:
                    ser.feed(
                        frame_build(
                            T_STATUS,
                            bytes([0x40, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00]),
                            0x50,
                        )
                    )
            _time.sleep(0.002)

    worker = _threading.Thread(target=_responder, daemon=True)
    worker.start()
    return made, sers, stop, worker


def _all_written(sers: list[_ScriptedSer]) -> list[bytes]:
    out: list[bytes] = []
    for ser in sers:
        with ser._lock:
            out.extend(ser.written)
    return out


def test_proc_session_ops_round_trip(monkeypatch):
    made, sers, stop, worker = _open_proc_with_scripted_ser(monkeypatch)
    try:
        assert made.hello(timeout=2.0) is True
        status = made.send_beacon(timeout=2.0)
        assert status is not None
        assert status["errcode"] == 0
        assert any(
            len(f) >= 2 and f[0] == 0xAB and f[1] == _T_BEACON
            for f in _all_written(sers)
        )
        stats = made.live_stats()
        assert stats["sent"] >= 0
        assert stats["err_drop"] == 0
    finally:
        stop.set()
        worker.join(1.0)
        made.close()


def test_send_config_public_path(monkeypatch):
    from pokecon.domain.transport import BconTransport
    from pokecon.domain.transport.bcon_protocol import T_STATUS_REQ

    made = BconTransport()
    ser = _ScriptedSer()
    made.ser = ser
    assert made.send_config(T_STATUS_REQ, b"") is True
    assert any(
        len(f) >= 3 and f[0] == 0xAB and f[1] == T_STATUS_REQ for f in ser.written
    )
    assert made.send_config(0x99, b"toolong" * 8) is False


def test_proc_pump_delivers_on_arrival():
    """到着確認駆動のポンプが即時分配する（タイムアウト待ちしない）。"""
    import time as _time

    from pokecon.domain.transport import BconTransport
    from pokecon.domain.transport.bcon_protocol import T_STATUS, frame_build

    made = BconTransport()
    ser = _ScriptedSer()
    made.ser = ser
    assert made.start_rx_pump() is True
    try:
        ser.feed(
            frame_build(
                T_STATUS, bytes([0x40, 0x07, 0x00, 0x00, 0x00, 0x00, 0x00]), 0x60
            )
        )
        deadline = _time.perf_counter() + 2.0
        while _time.perf_counter() < deadline:
            if made.last_status().get("last_seq") == 0x07:
                break
            _time.sleep(0.01)
        assert made.last_status().get("last_seq") == 0x07
    finally:
        made.stop_rx_pump()


def test_proc_spawn_smoke_and_double_close():
    """本物のspawnで起動・丁寧な失敗・二重closeする。"""
    from pokecon.domain.transport import bcon_proc

    calls: list[int] = []
    real_spawn = bcon_proc._default_spawn

    def _counting(entry: Any, cfg: dict) -> Any:
        calls.append(1)
        return real_spawn(entry, cfg)

    made = bcon_proc.BconProcTransport(_spawn=_counting)
    try:
        assert made._spawn_worker() is True
        assert made.proc_alive() is True
        assert made.open(9999, "", 1000000) is False
        assert calls == [1]
        assert made.is_open() is False
        assert made.request_status(timeout=0.2) is None
        assert made.live_stats()["sent"] == 0
    finally:
        made.close()
        made.close()


def test_proc_worker_crash_fail_safe(monkeypatch):
    """子が死んでもハングせず安全既定値。reopenで復帰。"""
    made, sers, stop, worker = _open_proc_with_scripted_ser(monkeypatch)
    try:
        assert made.hello(timeout=2.0) is True
        made._terminate_worker_for_test()
        assert made.request_status(timeout=0.3) is None
        assert made.send_beacon(timeout=0.3) is None
        assert made.live_stats()["sent"] == 0
        assert made.open(3, "COM3", 1000000) is True
        assert made.is_open() is True
    finally:
        stop.set()
        worker.join(1.0)
        made.close()


def test_proc_socket_framing_round_trip_and_eof():
    """ソケットキューは往復し、相手死亡でEOFになる。"""
    import socket as _socket
    import time as _time

    from pokecon.domain.transport import bcon_proc

    listener = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = int(listener.getsockname()[1])
    sender = _socket.create_connection(("127.0.0.1", port), timeout=5.0)
    conn, _addr = listener.accept()
    listener.close()
    putter = bcon_proc._SocketQueue(sender, name="put")
    getter = bcon_proc._SocketQueue(conn, name="get")
    try:
        putter.put(("hello", 1, b"abc"))
        # 1本目は非ブロッキングget（子のIMU合流が使う口）で取る。
        deadline = _time.perf_counter() + 5.0
        got: Any = bcon_proc._NO_IMU
        while got is bcon_proc._NO_IMU and _time.perf_counter() < deadline:
            got = getter.get_nowait()
            if got is bcon_proc._NO_IMU:
                _time.sleep(0.002)
        assert got == ("hello", 1, b"abc")
        assert getter.get_nowait() is bcon_proc._NO_IMU, "空なのに値が返る"
        putter.put(("hello", 2, b"def"))
        assert getter.get(timeout=5.0) == ("hello", 2, b"def")
        sender.close()
        with pytest.raises(OSError):
            putter.put(("imu", 0, 0, 4096, 0, 0, 125))
        deadline = _time.perf_counter() + 5.0
        while True:
            try:
                getter.get(timeout=0.2)
            except bcon_proc._PeerGone:
                break
            assert _time.perf_counter() < deadline, "EOFが来ない"
    finally:
        putter.close()
        getter.close()


def test_proc_send_imu_queues_without_rpc(monkeypatch):
    """IMUは同期RPCを通らず、窓内なら和で畳む。合計角度は保つ。"""
    import time as _time

    from pokecon.domain.transport.bcon_protocol import T_IMU_SAMPLE

    made, sers, stop, worker = _open_proc_with_scripted_ser(monkeypatch)
    calls: list[str] = []
    real_call = made._call

    def _counting(method: str, args: tuple = (), **kwargs: Any) -> tuple[bool, Any]:
        calls.append(method)
        return real_call(method, args, **kwargs)

    made._call = _counting  # type: ignore[method-assign]
    try:
        assert made.hello(timeout=2.0)
        calls.clear()
        for i in range(20):  # 100ms窓に収まる範囲（cap超過は別テスト）。
            # ジャイロを変える（加速度は最新だけ残るので合計の証拠にならない）。
            assert made.send_imu(0, 0, 4096, i, 0, 0) is True
        assert calls == [], f"IMUが同期RPCを通った: {calls}"
        deadline = _time.perf_counter() + 5.0
        frames: list[bytes] = []
        gx_total = 0
        while _time.perf_counter() < deadline:
            frames = [
                f
                for f in _all_written(sers)
                if len(f) >= 2 and f[0] == 0xAB and f[1] == T_IMU_SAMPLE
            ]
            gx_total = sum(
                int.from_bytes(f[9:11], "little", signed=True) for f in frames
            )
            if gx_total >= 190:  # 0+1+...+19。全サンプルのジャイロが届いた証拠。
                break
            _time.sleep(0.005)
        assert frames, "子のIMUフレームが1本も出ない"
        assert len(frames) <= 20, f"畳まずに{len(frames)}本書いた"
        assert gx_total == 190, f"ジャイロの合計が欠けた: {gx_total}"
        assert all(f[2] == 12 for f in frames)
        stats = made.imu_stats()
        assert stats["sent"] == 20
        assert stats["dropped"] == 0
        assert stats["stale"] == 0
        assert stats["frames"] >= 1
    finally:
        stop.set()
        worker.join(1.0)
        made.close()


class _SeamTransport:
    """子プロセス内のBconTransport代役（worker/parent境界の検査用）。

    最初の `send_imu` だけを指定イベントまで止める。止めている間に
    親が何フレーム積むか、復帰後に何本線へ出るかを決定的に見る。
    """

    instances: list["_SeamTransport"] = []
    block_first = True

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.calls: list[tuple[int, int, int, int, int, int]] = []
        self.lock = threading.Lock()
        self.entered = threading.Event()
        self.release = threading.Event()
        self.closed = False
        type(self).instances.append(self)

    def set_hooks(self, *args: Any, **kwargs: Any) -> None:
        return None

    def subscribe_rx(self, *args: Any, **kwargs: Any) -> None:
        return None

    def add_listener(self, *args: Any, **kwargs: Any) -> None:
        return None

    def send_imu(self, ax: int, ay: int, az: int, gx: int, gy: int, gz: int) -> bool:
        with self.lock:
            self.calls.append((ax, ay, az, gx, gy, gz))
            first = len(self.calls) == 1
        if first and type(self).block_first:
            self.entered.set()
            self.release.wait(5.0)
        return True

    def hold_input(self, hold: bool) -> bool:
        return True

    def close(self) -> None:
        self.closed = True


def _start_seam(
    monkeypatch: Any, *, block_first: bool = True
) -> tuple[Any, type[_SeamTransport]]:
    """fake spawn＋本物の `_worker_main` をスレッドで起こす。"""
    import time as _time

    from pokecon.domain.transport import bcon_proc

    monkeypatch.setattr(bcon_proc, "BconTransport", _SeamTransport)
    # 直前のテストの子が残した実体と混ざらないようにする。
    _SeamTransport.instances.clear()
    _SeamTransport.block_first = bool(block_first)
    made = bcon_proc.BconProcTransport(_spawn=_fake_spawn)
    assert made._spawn_worker() is True
    deadline = _time.perf_counter() + 5.0
    while not _SeamTransport.instances and _time.perf_counter() < deadline:
        _time.sleep(0.005)
    assert _SeamTransport.instances, "子のtransportが作られない"
    return made, _SeamTransport


def _wait_imu_idle(made: Any, timeout: float = 5.0) -> dict[str, int]:
    """全IMUフレームのackが返るまで待ち、統計を返す。"""
    import time as _time

    deadline = _time.perf_counter() + timeout
    while _time.perf_counter() < deadline:
        stats = made.imu_stats()
        if stats["delivered"] >= stats["frames"]:
            return stats
        _time.sleep(0.005)
    return made.imu_stats()


def test_proc_imu_blocked_child_bounds_and_coalesces_backlog(monkeypatch):
    """100ms窓内の子ブロックで、古いIMUを1本ずつ線へ書かせない。

    Given: 子の最初のIMU書き込みを止める / When: その間に20サンプル投入 /
    Then: FIFOはack待ち上限で頭打ち・復帰後の線への書き込みは数本で、
    窓内のジャイロ合計と最新加速度は保たれる。
    """
    from pokecon.domain.transport import bcon_proc

    made, cls = _start_seam(monkeypatch)
    tr = cls.instances[0]
    try:
        assert made.send_imu(0, 0, 4096, 0, 0, 0) is True
        assert tr.entered.wait(2.0), "子の最初のIMU書き込みが始まらない"
        # ここから先は子がRPCで詰まっている状態（ackが返らない）。
        for i in range(1, 20):
            assert made.send_imu(i % 7, 0, 4096, i, -i, 2 * i) is True
        # 入力は20件すべて受理。子のFIFOはack待ち上限で頭打ち。
        assert made._cmd_q.qsize() <= bcon_proc.PROC_IMU_INFLIGHT_MAX
        stats = made.imu_stats()
        assert stats["sent"] == 20
        assert stats["dropped"] == 0
        assert stats["coalesced"] == 18
        assert stats["frames"] == 2
        assert stats["stale"] == 0, "100ms窓内で捨ててはいけない"
        # 復帰させ、線へ出た本数と合計を見る。
        tr.release.set()
        final = _wait_imu_idle(made)
        assert final["delivered"] == final["frames"]
        with tr.lock:
            calls = list(tr.calls)
        assert 1 <= len(calls) <= 1 + bcon_proc.PROC_IMU_INFLIGHT_MAX
        assert sum(c[3] for c in calls) == sum(range(20))
        assert sum(c[4] for c in calls) == -sum(range(20))
        assert sum(c[5] for c in calls) == 2 * sum(range(20))
        assert calls[-1][:3] == (19 % 7, 0, 4096), "加速度が最新でない"
        assert all(-32768 <= v <= 32767 for c in calls for v in c[3:])
        assert final["stale"] == 0
    finally:
        tr.release.set()
        made.close()


def test_proc_imu_single_sample_survives_accounting(monkeypatch):
    """子が止まっていても5ms1件の小さな入力は1本・統計1件で通る。"""
    made, cls = _start_seam(monkeypatch)
    tr = cls.instances[0]
    try:
        assert made.send_imu(0, 0, 4096, 125, -3, 7) is True
        assert tr.entered.wait(2.0)
        # 子がこの1本で止まっている間も、入力は畳まれずそのまま残る。
        stats = made.imu_stats()
        assert stats["sent"] == 1
        assert stats["coalesced"] == 0
        assert stats["frames"] == 1
        tr.release.set()
        stats = _wait_imu_idle(made)
        with tr.lock:
            calls = list(tr.calls)
        assert calls == [(0, 0, 4096, 125, -3, 7)]
        assert stats == {
            "sent": 1,
            "dropped": 0,
            "coalesced": 0,
            "frames": 1,
            "delivered": 1,
            "stale": 0,
        }
    finally:
        tr.release.set()
        made.close()


def test_proc_imu_hold_drops_stale_angle_and_stops_delivery(monkeypatch):
    """holdで合算待ちを捨て、解除後に古い角度を再生しない。"""
    import time as _time

    made, cls = _start_seam(monkeypatch)
    tr = cls.instances[0]
    holder: dict[str, Any] = {}

    def _hold() -> None:
        holder["ok"] = made.hold_input(True)

    thread = threading.Thread(target=_hold, daemon=True)
    try:
        assert made.send_imu(0, 0, 4096, 0, 0, 0) is True
        assert tr.entered.wait(2.0)
        for i in range(1, 40):
            assert made.send_imu(0, 0, 4096, i, 0, 0) is True
        thread.start()
        deadline = _time.perf_counter() + 2.0
        while not made._imu_hold and _time.perf_counter() < deadline:
            _time.sleep(0.002)
        assert made._imu_hold, "holdが親側で立たない"
        # 100ms窓を超えて捨てた18本 + holdで捨てた20本。
        assert made.imu_stats()["stale"] == 38, "合算待ちが捨てられていない"
        tr.release.set()
        thread.join(5.0)
        assert not thread.is_alive()
        assert holder.get("ok") is True
        # hold中の入力は受理しない（子へ積まない）。
        assert made.send_imu(0, 0, 4096, 999, 0, 0) is False
        stats = made.imu_stats()
        assert stats["sent"] == 40
        assert stats["dropped"] == 1
        assert stats["stale"] == 38
        assert stats["coalesced"] == 0, "捨てた分が合算待ちに残っている"
        with tr.lock:
            calls = list(tr.calls)
        # hold前にack待ちだった2本（0と1）だけ。合算待ち20本とcap超過18本は出ない。
        assert sum(c[3] for c in calls) == 1
        assert max(c[3] for c in calls) <= 1
        # 解除後の新しい動きは通る。捨てた合算が混ざらないこと。
        assert made.hold_input(False) is True
        assert made.send_imu(0, 0, 4096, 7, 0, 0) is True
        _wait_imu_idle(made)
        with tr.lock:
            calls = list(tr.calls)
        assert sum(c[3] for c in calls) == 8
    finally:
        tr.release.set()
        made.close()


def test_proc_imu_large_backlog_splits_i16_without_losing_angle(monkeypatch):
    """窓内で合算がint16を超えても、分割して合計角度を保つ（飽和で消さない）。"""
    from pokecon.domain.transport import bcon_proc

    made, cls = _start_seam(monkeypatch)
    tr = cls.instances[0]
    try:
        assert made.send_imu(0, 0, 4096, 4000, 0, 0) is True
        assert tr.entered.wait(2.0)
        for _ in range(19):  # 20サンプル=100ms窓内。
            assert made.send_imu(0, 0, 4096, 4000, 0, 0) is True
        tr.release.set()
        final = _wait_imu_idle(made)
        with tr.lock:
            calls = list(tr.calls)
        assert (
            len(calls)
            <= bcon_proc.PROC_IMU_INFLIGHT_MAX + bcon_proc.PROC_IMU_CHUNKS_MAX
        ), f"分割が多すぎる: {len(calls)}本"
        assert sum(c[3] for c in calls) == 4000 * 20, "窓内の合計角度が欠けた"
        assert all(-32768 <= c[3] <= 32767 for c in calls)
        assert final["delivered"] == final["frames"]
        assert final["stale"] == 0
    finally:
        tr.release.set()
        made.close()


def test_proc_imu_tail_100ms_cap_drops_oldest_keeps_fresh(monkeypatch):
    """100msを超えたら最古を stale として捨て、直近の微小動作は残す。

    40サンプル（200ms相当）を子ブロック中に投入する。窓は20サンプル。
    最初の2本はack待ちでFIFOにあり、残り38本のうち古い18本が捨てられ、
    直近20本だけが畳まれる。最後の1本（1LSB相当）は生きたまま届く。
    """
    from pokecon.domain.transport import bcon_proc

    made, cls = _start_seam(monkeypatch)
    tr = cls.instances[0]
    try:
        assert made.send_imu(0, 0, 4096, 1000, 0, 0) is True
        assert tr.entered.wait(2.0)
        for _ in range(38):
            assert made.send_imu(0, 0, 4096, 1000, 0, 0) is True
        # 最後だけ微小な1LSB。畳んだ和の末尾に残ることを見る。
        assert made.send_imu(0, 0, 4096, 1, 0, 0) is True
        stats = made.imu_stats()
        assert stats["sent"] == 40
        assert stats["dropped"] == 0
        assert stats["coalesced"] == 20, "staleへ転じた分が残っている"
        assert stats["frames"] == 2
        assert stats["stale"] == 18, "窓を超えた古い18本が数えられていない"
        assert len(made._imu_mailbox) == bcon_proc.PROC_IMU_MERGE_MAX
        tr.release.set()
        final = _wait_imu_idle(made)
        with tr.lock:
            calls = list(tr.calls)
        # sample1+sample2+直近20本。捨てた18本×1000は出ない。
        assert sum(c[3] for c in calls) == 39 * 1000 + 1 - 18 * 1000
        assert sum(c[3] for c in calls) % 1000 == 1, "直近の1LSBが消えた"
        assert all(-32768 <= c[3] <= 32767 for c in calls)
        assert len(calls) <= 1 + bcon_proc.PROC_IMU_INFLIGHT_MAX
        assert final["stale"] == 18
        assert final["delivered"] == final["frames"]
    finally:
        tr.release.set()
        made.close()


def test_proc_imu_ack_discards_mailbox_older_than_wall_clock_window(monkeypatch):
    """入力が止まった後の長い子ブロックでは、古い角度を復帰時に再生しない。"""
    from pokecon.domain.transport import bcon_proc

    clock = [100.0]
    monkeypatch.setattr(bcon_proc.time, "monotonic", lambda: clock[0])
    made, cls = _start_seam(monkeypatch)
    tr = cls.instances[0]
    try:
        assert made.send_imu(0, 0, 4096, 0, 0, 0)
        assert tr.entered.wait(2.0)
        assert made.send_imu(0, 0, 4096, 0, 0, 0)
        assert made.send_imu(0, 0, 4096, 500, 0, 0)
        clock[0] += 0.2
        tr.release.set()
        final = _wait_imu_idle(made)
        with tr.lock:
            calls = list(tr.calls)
        assert sum(sample[3] for sample in calls) == 0
        assert final["stale"] == 1
        assert final["coalesced"] == 0
    finally:
        tr.release.set()
        made.close()


def test_proc_imu_stale_ack_does_not_flush_new_mailbox(monkeypatch):
    """未知・前世代のackで、新しい世代の合算待ちを押し出さない。"""
    made, cls = _start_seam(monkeypatch)
    tr = cls.instances[0]
    try:
        assert made.send_imu(0, 0, 4096, 0, 0, 0) is True
        assert tr.entered.wait(2.0)
        # sample2はFIFO、sample3はメールボックスに残る。
        assert made.send_imu(0, 0, 4096, 1, 0, 0) is True
        assert made.send_imu(0, 0, 4096, 2, 0, 0) is True
        assert len(made._imu_mailbox) == 1
        before = made.imu_stats()
        made._on_imu_ack(999999, True)
        stats = made.imu_stats()
        assert len(made._imu_mailbox) == 1, "未知ackでメールボックスが流れた"
        assert made._cmd_q.qsize() == 1
        assert stats["frames"] == before["frames"]
        assert stats["delivered"] == before["delivered"]
        assert stats["stale"] == 0
        tr.release.set()
        final = _wait_imu_idle(made)
        with tr.lock:
            calls = list(tr.calls)
        assert sum(c[3] for c in calls) == 0 + 1 + 2
        assert final["delivered"] == final["frames"]
    finally:
        tr.release.set()
        made.close()


def test_proc_imu_stop_and_reopen_do_not_replay_stale_angle(monkeypatch):
    """停止で合算待ちを捨て、再open後に古い角度を流さない。"""
    import time as _time

    made, cls = _start_seam(monkeypatch)
    tr = cls.instances[0]
    try:
        assert made.send_imu(0, 0, 4096, 0, 0, 0) is True
        assert tr.entered.wait(2.0)
        for i in range(1, 40):
            assert made.send_imu(0, 0, 4096, i, 0, 0) is True
        assert made.stop_imu_loop() is True
        # 100ms窓を超えて捨てた18本 + stopで捨てた20本。
        assert made.imu_stats()["stale"] == 38
        assert made.imu_stats()["coalesced"] == 0
        tr.release.set()
        _wait_imu_idle(made)
        with tr.lock:
            calls = list(tr.calls)
        assert sum(c[3] for c in calls) == 1, "停止後に合算が流れた"
        assert max(c[3] for c in calls) <= 1
        made.close()
        # 作り直し: 新しい子には新しい入力だけが渡る。
        cls.block_first = False
        assert made._spawn_worker() is True
        deadline = _time.perf_counter() + 5.0
        while len(cls.instances) < 2 and _time.perf_counter() < deadline:
            _time.sleep(0.005)
        assert len(cls.instances) >= 2
        new_tr = cls.instances[1]
        assert made.send_imu(0, 0, 4096, 123, 0, 0) is True
        _wait_imu_idle(made)
        with new_tr.lock:
            new_calls = list(new_tr.calls)
        assert [c[3] for c in new_calls] == [123]
    finally:
        cls.block_first = True
        tr.release.set()
        made.close()


def test_proc_sockets_enable_tcp_nodelay(monkeypatch):
    """accept側・connect側の両方でTCP_NODELAYを立てる。"""
    import socket as _socket

    import pokecon.domain.transport.bcon_proc as _proc
    from pokecon.domain.transport import bcon_proc_child

    nodelay = (_socket.IPPROTO_TCP, _socket.TCP_NODELAY, 1)
    got_accepted: list[tuple[int, int, int]] = []
    got_connected: list[tuple[int, int, int]] = []

    class _Sock:
        def __init__(self, log: list[tuple[int, int, int]] | None = None) -> None:
            self._log = log

        def setsockopt(self, level: int, opt: int, value: int) -> None:
            if self._log is not None:
                self._log.append((level, opt, value))

        def setblocking(self, flag: bool) -> None:
            pass

        def settimeout(self, value: Any) -> None:
            pass

        def getsockname(self) -> tuple[str, int]:
            return ("127.0.0.1", 4321)

        def sendall(self, data: bytes) -> None:
            pass

        def close(self) -> None:
            pass

    conn = _Sock(got_accepted)

    class _Listener(_Sock):
        def bind(self, addr: Any) -> None:
            pass

        def listen(self, backlog: int) -> None:
            pass

        def accept(self) -> tuple[Any, tuple[str, int]]:
            return conn, ("127.0.0.1", 1)

    class _Proc:
        def terminate(self) -> None:
            pass

    monkeypatch.setattr(_proc.socket, "socket", lambda *a, **k: _Listener())
    monkeypatch.setattr(_proc.subprocess, "Popen", lambda *a, **k: _Proc())
    monkeypatch.setattr(_proc.secrets, "token_hex", lambda n: "tok")
    monkeypatch.setattr(_proc, "_recv_framed", lambda sock: ("hello-child", "tok"))
    _proc._default_spawn(None, {})

    child_sock = _Sock(got_connected)
    monkeypatch.setattr(
        bcon_proc_child.socket, "create_connection", lambda *a, **k: child_sock
    )
    monkeypatch.setattr(_proc, "_worker_main", lambda *a, **k: None)
    import base64
    import pickle

    cfg_b64 = base64.b64encode(pickle.dumps({})).decode("ascii")
    assert bcon_proc_child._child_main(4321, "tok", cfg_b64) == 0

    assert got_accepted == [nodelay]
    assert got_connected == [nodelay]


def test_proc_live_loop_and_hooks(monkeypatch):
    import time as _time

    made, sers, stop, worker = _open_proc_with_scripted_ser(monkeypatch)
    hooked: list[tuple[str, str]] = []
    made.set_hooks(
        lambda row, show=True: hooked.append(("begin", row)),
        lambda row, show=True: hooked.append(("end", row)),
    )
    try:
        assert made.hello(timeout=2.0)
        hooked.clear()
        assert made.start_live_loop() is True
        assert made.live_loop_running() is True
        made.send_row("0 8 80 80 80 80")
        made.flush_pending()
        deadline = _time.perf_counter() + 3.0
        states: list[bytes] = []
        while _time.perf_counter() < deadline:
            states = [
                f
                for f in _all_written(sers)
                if len(f) >= 2 and f[0] == 0xAB and f[1] == 0x01
            ]
            if states:
                break
            _time.sleep(0.05)
        assert states, "STATEが線に出ない"
        assert any(kind == "begin" for kind, _row in hooked)
        assert made.stop_live_loop() is True
        assert made.live_loop_running() is False
    finally:
        stop.set()
        worker.join(1.0)
        made.close()


def test_proc_child_input_hold_and_current_baud(monkeypatch):
    """入力の門は子で判定し、親はフレームごとの RPC を追加しない。"""
    from pokecon.domain.transport.bcon_protocol import T_STATE

    made, sers, stop, worker = _open_proc_with_scripted_ser(monkeypatch)
    try:
        assert made.current_baud() == 1000000
        assert not made.hold_input(False)
        made.send_row("10 08")
        # 会話 RPC を barrier とし、その前の stage が子で処理済みになる。
        assert made.current_baud() == 1000000
        assert not any(frame[1] == T_STATE for frame in _all_written(sers))
        assert made.hello(timeout=2.0)
        assert made.hold_input(True)
        made.send_row("10 08")
        assert made.current_baud() == 1000000
        assert not any(frame[1] == T_STATE for frame in _all_written(sers))
        assert made.hold_input(False)
        made.send_row("10 08")
        assert made.current_baud() == 1000000
        assert any(frame[1] == T_STATE for frame in _all_written(sers))
    finally:
        stop.set()
        worker.join(1.0)
        made.close()


def test_proc_baud_hunt_upgrades_low_lock_to_1m(monkeypatch):
    """子プロセス側のhuntも1Mへ上がり、親は同じ結果を受け取る。"""
    from pokecon.domain.transport.bcon_protocol import T_BAUD_SET, T_HELLO

    made, sers, stop, worker = _open_proc_with_scripted_ser(monkeypatch)
    try:
        # Given: まず115200へ落とし、そのrateで確定できる子にする。
        assert made.set_baud_index(0) is True
        assert made.current_baud() == 115200
        before = len(_all_written(sers))
        # When: 115200だけを候補にhuntする
        locked = made.baud_hunt(candidates=[115200], timeout_per=0.2)
        frames = [
            f for f in _all_written(sers)[before:] if len(f) >= 2 and f[0] == 0xAB
        ]
        # Then: scanのHELLOで確定した後にBAUD_SET(3)→adopt HELLOで1Mになる。
        types = [f[1] for f in frames]
        at = types.index(T_BAUD_SET)
        assert locked == 1000000
        assert made.current_baud() == 1000000
        assert T_HELLO in types[:at]
        assert T_HELLO in types[at + 1 :]
        assert [int(f[3]) for f in frames if f[1] == T_BAUD_SET] == [3]
    finally:
        stop.set()
        worker.join(1.0)
        made.close()
