"""画像認識・カメラ読み取りの操作単位リカバリ（実機なし）。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import cv2
import numpy as np
import pytest
from core.CommandVision import VisionMixin
from core.recovery import CameraUnavailableError, RecoveryAborted, TemplateImageError


class Probe(VisionMixin):
    """人に尋ねる部分と待ちだけを差し替えた VisionMixin。"""

    def __init__(self, answer: Any = None) -> None:
        self.answer = answer
        self.asks: list[Any] = []
        self.sleeps: list[float] = []
        self.paused = False
        self.pause_calls = 0
        self.resume_calls = 0

    def wait(self, seconds: float) -> None:
        self.sleeps.append(float(seconds))

    def pause(self) -> None:
        self.paused = True
        self.pause_calls += 1

    def resume(self) -> None:
        self.paused = False
        self.resume_calls += 1

    def isPaused(self) -> bool:
        return self.paused

    def ask_recovery(self, advice: Any) -> bool | None:
        self.asks.append(advice)
        return self.answer(advice) if callable(self.answer) else self.answer


def _write_png(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    assert cv2.imwrite(str(path), np.zeros((8, 8), dtype=np.uint8))


def test_missing_template_prompts_and_reads_after_fix(tmp_path: Path) -> None:
    # Given: まだ無いテンプレート
    path = tmp_path / "pack" / "a.png"

    def answer(_advice: Any) -> bool:
        _write_png(path)
        return True

    probe = Probe(answer)

    # When: 読み込みが失敗し、利用者が置いてから再試行する
    image = probe._readTemplateWithRecovery(str(path), cv2.IMREAD_GRAYSCALE)

    # Then: 同じ読みがやり直され、押しっぱなしも戻る
    assert image.shape == (8, 8)
    assert len(probe.asks) == 1
    assert probe.asks[0].title == "テンプレート画像を読み込めません"
    assert probe.pause_calls == 1
    assert probe.resume_calls == 1


def test_missing_template_abort_stops_cleanly(tmp_path: Path) -> None:
    path = tmp_path / "pack" / "missing.png"
    probe = Probe(False)

    with pytest.raises(RecoveryAborted):
        probe._readTemplateWithRecovery(str(path), cv2.IMREAD_GRAYSCALE)
    assert len(probe.asks) == 1
    assert probe.resume_calls == 1


def test_missing_template_without_gui_raises_original(tmp_path: Path) -> None:
    # Given: 人に尋ねる口が無い（CUI・テスト）
    path = tmp_path / "pack" / "missing.png"
    probe = BareProbe()

    # Then: 勝手に待たず、診断情報つきの元例外を出す
    with pytest.raises(TemplateImageError) as info:
        probe._readTemplateWithRecovery(str(path), cv2.IMREAD_GRAYSCALE)
    assert "missing.png" in str(info.value)


class BareProbe(VisionMixin):
    """ask_recovery を持たない（GUI 無し相当）。"""

    def wait(self, seconds: float) -> None:
        pass


class FakeCamera:
    def __init__(self, frames: list[Any]) -> None:
        self._frames = list(frames)
        self.calls = 0

    def readFrame(self) -> Any:
        self.calls += 1
        return self._frames.pop(0) if self._frames else None


def test_camera_transient_loss_retries_without_prompt() -> None:
    # Given: 2回Noneを返し、3回目に復帰するカメラ
    frame = np.zeros((4, 4, 3), dtype=np.uint8)
    camera = FakeCamera([None, None, frame])
    probe = Probe(True)
    probe.camera = camera

    # When
    got = probe._readFrameOrRaise()

    # Then: 自動の2回で復帰し、人には尋ねない
    assert got is frame
    assert camera.calls == 3
    assert probe.asks == []
    assert probe.sleeps == [0.25, 0.5]


def test_camera_loss_after_auto_retries_asks_for_help() -> None:
    camera = FakeCamera([])
    probe = Probe(False)
    probe.camera = camera

    with pytest.raises(RecoveryAborted):
        probe._readFrameOrRaise()
    assert camera.calls == 3
    assert len(probe.asks) == 1


def test_camera_without_assignment_is_not_recoverable() -> None:
    # Given: カメラの割り当てが無い（設定・コードの誤り）
    probe = Probe(True)
    probe.camera = None

    # Then: 再試行せず、従来どおり即座に止める
    with pytest.raises(RuntimeError, match="割り当てられていません"):
        probe._readFrameOrRaise()
    assert probe.asks == []
    assert probe.sleeps == []


def test_camera_error_is_a_recoverable_runtime_error() -> None:
    # 既存スクリプトの except RuntimeError も壊さない。
    assert isinstance(CameraUnavailableError("x"), RuntimeError)
