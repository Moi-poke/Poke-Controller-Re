"""マクロからのジャイロ再生（OperateMixin.gyro_play）の契約（実機不要）。"""

from __future__ import annotations

import os
import time
from types import SimpleNamespace
from typing import Any


class _LoopTransport:
    def __init__(self) -> None:
        self.started: list[tuple[float, bool, str]] = []
        self.stop_calls: list[str | None] = []
        self.owner = ""
        self.source: Any = None

    def start_imu_loop(self, source, interval_s=0.005, *, replace=False, owner=""):  # noqa: ANN001
        self.source = source
        self.owner = owner
        self.started.append((float(interval_s), bool(replace), owner))
        return True

    def stop_imu_loop(self, owner=None) -> bool:  # noqa: ANN001
        self.stop_calls.append(owner)
        if owner is not None and self.owner != owner:
            return False
        self.owner = ""
        return True


class _Cmd:
    """gyro_play が使う口だけを持つ最小の命令。"""

    from core.CommandOperate import OperateMixin as _Mix

    checkIfAlive = _Mix.checkIfAlive

    def __init__(self, transport) -> None:  # noqa: ANN001
        self.keys = SimpleNamespace(ser=SimpleNamespace(transport=transport))
        self.alive = True

    gyro_play = _Mix.gyro_play
    _stop_gyro_playback = _Mix._stop_gyro_playback
    _resolve_gyro_path = staticmethod(_Mix._resolve_gyro_path)
    _gyro_transport_handle = _Mix._gyro_transport_handle


def _write_track(path: str, offset_us: int = 0) -> None:
    from core.motion_track import MotionRecorder

    with MotionRecorder(path, start_offset_us=offset_us) as rec:
        rec.write((0, 0, 4096, 100, 0, 0))
        rec.write((0, 0, 4096, 200, 0, 0))


def test_gyro_play_starts_loop_with_file_period(tmp_path) -> None:
    """Given: .pgyro / When: gyro_play / Then: 5ms周期・replace でループを起こすこと。"""
    path = str(tmp_path / "take.pgyro")
    _write_track(path, offset_us=10_000)
    transport = _LoopTransport()
    cmd = _Cmd(transport)

    assert cmd.gyro_play(path) is True
    assert len(transport.started) == 1
    interval_s, replace, owner = transport.started[0]
    assert interval_s == 0.005
    assert replace is True
    assert owner.startswith("macro:")
    assert transport.source is not None
    # 先頭の遅れ（10ms）まではサンプルを返さない。
    assert transport.source() is None
    time.sleep(0.012)
    assert transport.source() == (0, 0, 4096, 100, 0, 0)
    assert transport.source() == (0, 0, 4096, 200, 0, 0)

    cmd._stop_gyro_playback()
    assert transport.stop_calls == [owner]
    # 二度目は止める相手が無い。
    cmd._stop_gyro_playback()
    assert transport.stop_calls == [owner]


def test_gyro_play_missing_file_returns_false(tmp_path) -> None:
    transport = _LoopTransport()
    cmd = _Cmd(transport)
    assert cmd.gyro_play(str(tmp_path / "none.pgyro")) is False
    assert transport.started == []


def test_gyro_play_without_transport_returns_false(tmp_path) -> None:
    from core.CommandOperate import OperateMixin

    path = str(tmp_path / "take.pgyro")
    _write_track(path)
    cmd = _Cmd(None)
    assert OperateMixin.gyro_play(cmd, path) is False


def test_gyro_play_relative_path_found_from_cwd(tmp_path, monkeypatch) -> None:
    path = tmp_path / "records" / "take.pgyro"
    os.makedirs(path.parent, exist_ok=True)
    _write_track(str(path))
    monkeypatch.chdir(tmp_path)
    transport = _LoopTransport()
    cmd = _Cmd(transport)
    assert cmd.gyro_play("records/take.pgyro") is True
    assert transport.started


def test_macro_stop_leaves_panel_takeover_running(tmp_path) -> None:
    """Given: マクロ再生中にパネルが引き継ぐ / When: マクロ後始末 / Then: パネルのループは止めないこと。"""
    path = str(tmp_path / "take.pgyro")
    _write_track(path)
    transport = _LoopTransport()
    cmd = _Cmd(transport)
    assert cmd.gyro_play(path) is True
    micro_owner = transport.started[0][2]
    # パネルが手動の送出で引き継ぐ。
    assert transport.start_imu_loop(lambda: None, 0.005, replace=True, owner="panel")
    cmd._stop_gyro_playback()
    assert transport.stop_calls == [micro_owner]
    assert transport.owner == "panel"
