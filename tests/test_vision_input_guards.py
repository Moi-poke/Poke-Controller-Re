"""照合まわりの入力検証（フールプルーフ）の確認。

利用者が最も間違えやすい2つを、照合の前に落とす:
  - `threshold` を 0.7 ではなく 70 と書く（パーセントとの取り違え）
  - `crop` を [x1, y1, x2, y2] 以外で書く／画面の外を指す
どちらも素通しすると「一致しないまま待ち続ける」形で表面化し、
原因が画像側に見える。ここではフレームを読む前に ValueError になること、
crop は切り詰めずに断ることを確かめる。
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pytest
from pokecon.domain.CommandVision import VisionMixin


class _Vision(VisionMixin):
    """待ちの期限だけを持つ素の Mixin。検証はフレーム読み取りの手前で起きる。"""

    def _deadline(self, timeout: float) -> Any:
        return lambda: True

    def wait(self, seconds: float) -> None:
        return None


def _vision() -> _Vision:
    return _Vision()


@pytest.mark.parametrize("value", [1.5, 70, -0.1, None, "abc"])
@pytest.mark.parametrize(
    "call",
    [
        lambda cmd, v: cmd.isContainTemplate("x.png", threshold=v),
        lambda cmd, v: cmd.waitTemplate("x.png", threshold=v, timeout=0.0),
        lambda cmd, v: cmd.isContainTemplate_max(["x.png"], threshold=v),
        lambda cmd, v: cmd.isContainTemplateGPU("x.png", threshold=v),
        lambda cmd, v: cmd.findAllTemplates("x.png", threshold=v),
        lambda cmd, v: cmd._matchOnce("x.png", threshold=v),
    ],
)
def test_bad_threshold_is_refused_before_reading_a_frame(call: Any, value: Any) -> None:
    with pytest.raises(ValueError, match="threshold"):
        call(_vision(), value)


def test_threshold_accepts_the_edges_and_numeric_strings() -> None:
    VisionMixin._checkThreshold(0.0)
    VisionMixin._checkThreshold(1.0)
    VisionMixin._checkThreshold(0.7)
    # 数値として読める文字列は許す（Blockly 経由の値など）
    VisionMixin._checkThreshold("0.7")


@pytest.mark.parametrize("max_count", [0, -1, "many"])
def test_find_all_templates_refuses_a_non_positive_count(max_count: Any) -> None:
    with pytest.raises(ValueError, match="max_count"):
        _vision().findAllTemplates("x.png", max_count=max_count)


def _frame() -> np.ndarray:
    return np.zeros((40, 60, 3), np.uint8)


def test_crop_accepts_a_rectangle_inside_the_frame() -> None:
    cropped = VisionMixin._cropOrRaise(_frame(), [10, 5, 30, 35])

    assert cropped.shape[:2] == (30, 20)


@pytest.mark.parametrize(
    "crop",
    [
        [1, 2, 3],  # 4要素でない
        [30, 10, 10, 30],  # 左右が逆
        [10, 30, 30, 10],  # 上下が逆
        [0, 0, 61, 40],  # 画面の外（右）
        [0, 0, 60, 41],  # 画面の外（下）
        [-1, 0, 10, 10],  # 負の座標
    ],
)
def test_crop_refuses_a_bad_rectangle_instead_of_clipping(crop: list[int]) -> None:
    """numpy のスライスは黙って狭くなる。切り詰めずに断るのが要点。"""
    with pytest.raises(ValueError, match="crop"):
        VisionMixin._cropOrRaise(_frame(), crop)


def test_empty_crop_means_the_whole_frame() -> None:
    frame = _frame()

    assert VisionMixin._cropOrRaise(frame, []) is frame
    assert VisionMixin._cropOrRaise(frame, None) is frame
