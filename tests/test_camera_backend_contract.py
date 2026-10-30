"""The capture-side contract (``core/camera_backend.py``) and the "latest only" rule.

The renderer is written against ``CameraBackend``, not against ``core.Camera``.
That only holds if the real backends actually expose the members the protocol
names, and if the latest-frame box really overwrites instead of queueing -- a
queue would add latency that no renderer can undo.
"""

from __future__ import annotations

import inspect
from typing import Any

import numpy as np
import pytest
from core.Camera import Camera, CameraQueue, LatestFrame
from core.camera_backend import CameraBackend, TimedFrameSource

#: The members ``VideoRenderer``'s read path relies on. Kept as a literal so a
#: silently renamed method on a backend is a per-test failure. ``capture_size``
#: is not here: both backends set it in ``__init__`` (an instance attribute is
#: not visible on the class), so it is checked from the source below.
REQUIRED: tuple[str, ...] = (
    "readFrame",
    "readFrameWithSeq",
    "frame_seq",
    "isOpened",
    "setFps",
    "saveCapture",
    "destroy",
)


@pytest.mark.parametrize(
    "backend", [Camera, CameraQueue], ids=["Camera", "CameraQueue"]
)
def test_a_backend_exposes_every_member_the_renderer_reads(backend: type[Any]) -> None:
    missing = [name for name in REQUIRED if not hasattr(backend, name)]

    assert missing == [], f"{backend.__name__} is missing {missing}"
    # 座標変換の名目サイズ。実フレームが来る前の ``_mapper`` が読む。
    assert "self.capture_size" in inspect.getsource(backend.__init__)


def test_only_the_thread_backend_can_answer_with_publish_timestamps() -> None:
    # The phase lock needs t_ready_ns; the shared-memory backend never carried
    # it, so the renderer treats that method as optional.
    assert hasattr(Camera, "readFrameWithTiming")
    assert not hasattr(CameraQueue, "readFrameWithTiming")
    assert hasattr(Camera, "readFrameWithTiming")  # TimedFrameSource の一部


def test_the_protocols_are_runtime_checkable() -> None:
    backend = _FakeBackend()
    assert isinstance(backend, CameraBackend)
    timed = _FakeTimedBackend()
    assert isinstance(timed, TimedFrameSource)
    # 時刻を返せない個体は TimedFrameSource ではない（描画側は任意機能として扱う）。
    assert not isinstance(backend, TimedFrameSource)

    assert not isinstance(_Incomplete(), CameraBackend)


def test_the_latest_box_overwrites_and_never_queues() -> None:
    box = LatestFrame()
    frames = [_frame(value) for value in (1, 2, 3)]

    for frame in frames:
        box.put(frame, 123)

    current = box.get()
    assert current is frames[2], "the box kept an older frame instead of the newest"
    assert box.seq == 3, "each put must advance the sequence"


def test_put_ignores_none_so_a_failed_read_does_not_erase_the_picture() -> None:
    box = LatestFrame()
    frame = _frame(5)
    box.put(frame, 1)
    seq = box.seq

    box.put(None, 2)

    assert box.get() is frame
    assert box.seq == seq


def test_clear_drops_the_frame_and_advances_the_sequence() -> None:
    box = LatestFrame()
    box.put(_frame(1), 1)
    before = box.seq

    box.clear()

    assert box.get() is None
    assert box.seq == before + 1, "a consumer keyed on seq would reuse the old frame"


def test_the_snapshot_pairs_the_frame_with_its_own_generation() -> None:
    box = LatestFrame()
    frame = _frame(9)
    box.put(frame, 555)

    snapshot = box.get_with_timing()

    assert snapshot.frame is frame
    assert snapshot.seq == 1
    assert snapshot.t_capture_ns == 555
    assert snapshot.t_ready_ns > 0


def _frame(value: int) -> np.ndarray:
    frame = np.zeros((2, 2, 3), np.uint8)
    frame[:, :] = value
    return frame


class _FakeBackend:
    """Minimal conforming backend: every protocol member, nothing else."""

    capture_size = (2, 2)

    def readFrame(self, copy: bool = False) -> Any:
        return None

    def readFrameWithSeq(self, copy: bool = False) -> tuple[Any, int]:
        return None, 0

    def frame_seq(self) -> int:
        return 0

    def isOpened(self) -> bool:
        return False

    def setFps(self, fps: int) -> None:
        return None

    def saveCapture(self, *args: Any, **kwargs: Any) -> bool:
        return True

    def destroy(self) -> None:
        return None


class _Incomplete:
    """Only part of the read path; the renderer must not accept it as a backend."""

    capture_size = (2, 2)

    def readFrame(self, copy: bool = False) -> Any:
        return None


class _FakeTimedBackend(_FakeBackend):
    def readFrameWithTiming(self) -> tuple[Any, int, int, int]:
        return None, 0, 0, 0
