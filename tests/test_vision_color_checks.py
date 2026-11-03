"""色判定と照合前チェック（CPU のみ・実機なし）。

`getColorRatio` / `isSimilarColor` はテンプレート画像を作らずに
「暗転」「HPバーが赤い」を判定するための入口。`_checkTemplate` は
cv2 の分かりにくい例外を、利用者が直せる文言へ置き換える門番。
どちらも描画もカメラも要らないので、合成フレームで確かめる。
"""

from __future__ import annotations

from typing import Any

import cv2
import numpy as np
import pytest
from pokecon.domain.CommandVision import VisionMixin

RED_LOW = [0, 100, 100]
RED_HIGH = [10, 255, 255]


class _FrameCmd:
    """`getColorRatio` が要求する読み取りだけを持つ代役。"""

    getColorRatio = VisionMixin.getColorRatio

    def __init__(self, frame: np.ndarray) -> None:
        self.frame = frame

    def _readFrameOrRaise(self) -> np.ndarray:
        return self.frame

    def _cropOrRaise(self, frame: np.ndarray, crop: Any) -> np.ndarray:
        return frame


def _solid(bgr: tuple[int, int, int]) -> np.ndarray:
    frame = np.zeros((10, 10, 3), np.uint8)
    frame[:] = bgr
    return frame


def _hue(value: int) -> np.ndarray:
    """指定の色相だけを持つ 10x10 の BGR フレームを作る。"""
    hsv = np.zeros((10, 10, 3), np.uint8)
    hsv[:, :, 0] = value
    hsv[:, :, 1] = 255
    hsv[:, :, 2] = 255
    return cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR)


def test_color_ratio_is_one_for_a_matching_frame() -> None:
    cmd = _FrameCmd(_solid((0, 0, 255)))  # 赤

    assert VisionMixin.getColorRatio(cmd, [], RED_LOW, RED_HIGH) == pytest.approx(1.0)


def test_color_ratio_is_zero_for_another_hue() -> None:
    cmd = _FrameCmd(_solid((255, 0, 0)))  # 青

    assert VisionMixin.getColorRatio(cmd, [], RED_LOW, RED_HIGH) == pytest.approx(0.0)


def test_color_ratio_splits_a_hue_range_that_wraps_around() -> None:
    """赤は 0 をまたぐ。下限>上限を2つに割らないと常に 0 件になる。"""
    wrap_low = [170, 100, 100]
    wrap_high = [5, 255, 255]

    near_end = _FrameCmd(_hue(178))
    near_start = _FrameCmd(_hue(2))

    assert VisionMixin.getColorRatio(
        near_end, [], wrap_low, wrap_high
    ) == pytest.approx(1.0)
    assert VisionMixin.getColorRatio(
        near_start, [], wrap_low, wrap_high
    ) == pytest.approx(1.0)


def test_color_ratio_reports_a_fraction() -> None:
    frame = np.zeros((10, 10, 3), np.uint8)
    frame[:5] = (0, 0, 255)  # 上半分だけ赤
    cmd = _FrameCmd(frame)

    assert VisionMixin.getColorRatio(cmd, [], RED_LOW, RED_HIGH) == pytest.approx(0.5)


def test_is_similar_color_uses_the_threshold() -> None:
    cmd = _FrameCmd(_solid((0, 0, 255)))

    assert VisionMixin.isSimilarColor(cmd, [], RED_LOW, RED_HIGH, ratio=0.5) is True
    assert VisionMixin.isSimilarColor(cmd, [], RED_LOW, RED_HIGH, ratio=0.6) is True
    assert (
        VisionMixin.isSimilarColor(
            _FrameCmd(_solid((255, 0, 0))), [], RED_LOW, RED_HIGH, ratio=0.1
        )
        is False
    )


def _check(src: Any, template: Any, mask: Any = None) -> None:
    VisionMixin._checkTemplate(src, template, mask)


def test_check_template_accepts_a_matching_pair() -> None:
    src = np.zeros((20, 20, 3), np.uint8)
    template = np.zeros((5, 5, 3), np.uint8)

    _check(src, template)  # 例外が出ない


def test_check_template_explains_an_empty_template() -> None:
    with pytest.raises(ValueError, match="空"):
        _check(np.zeros((20, 20, 3), np.uint8), np.array([]))


def test_check_template_explains_a_colour_mismatch() -> None:
    src = np.zeros((20, 20, 3), np.uint8)

    with pytest.raises(ValueError, match="use_gray"):
        _check(src, np.zeros((5, 5), np.uint8))


def test_check_template_explains_a_channel_mismatch() -> None:
    src = np.zeros((20, 20, 3), np.uint8)

    with pytest.raises(ValueError, match="チャンネル数"):
        _check(src, np.zeros((5, 5, 4), np.uint8))


def test_check_template_explains_a_template_larger_than_the_frame() -> None:
    src = np.zeros((10, 10, 3), np.uint8)

    with pytest.raises(ValueError, match="より大きい"):
        _check(src, np.zeros((11, 10, 3), np.uint8))


def test_check_template_explains_a_mask_size_mismatch() -> None:
    src = np.zeros((20, 20, 3), np.uint8)
    template = np.zeros((5, 5, 3), np.uint8)

    with pytest.raises(ValueError, match="マスク"):
        _check(src, template, np.zeros((4, 5), np.uint8))
