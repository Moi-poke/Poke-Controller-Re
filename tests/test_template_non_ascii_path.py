"""テンプレート画像を日本語を含むパスから読めることの検証。

cv2.imread は Windows で非 ASCII のパスを開けず None を返す。この
ワークスペース名（`リポジトリの構成見直し`）のような場所では、実在する
画像でも「ファイルはありますが画像として読めません」になっていた。
np.fromfile → cv2.imdecode の経路なら読める。
"""

from __future__ import annotations

import cv2
import numpy as np
import pytest
from pokecon.domain import CommandVision
from pokecon.domain.Camera import imread


def _write_png(path: object) -> None:
    image = np.zeros((4, 6, 3), np.uint8)
    image[:, :, 1] = 255
    ok, encoded = cv2.imencode(".png", image)
    assert ok
    encoded.tofile(str(path))


def test_imread_reads_a_non_ascii_path(tmp_path: object) -> None:
    from pathlib import Path

    directory = Path(str(tmp_path)) / "日本語フォルダ"
    directory.mkdir()
    target = directory / "a.png"
    _write_png(target)

    loaded = imread(str(target))

    assert loaded is not None
    assert loaded.shape == (4, 6, 3)


def test_missing_file_returns_none(tmp_path: object) -> None:
    from pathlib import Path

    assert imread(str(Path(str(tmp_path)) / "ない.png")) is None


def test_template_loads_through_a_non_ascii_base(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """テンプレート読み込みが、置き場所のパスに日本語を含んでも通る。"""
    from pathlib import Path

    directory = Path(str(tmp_path)) / "テンプレート"
    directory.mkdir()
    _write_png(directory / "sample.png")
    monkeypatch.setattr(CommandVision, "TEMPLATE_PATH", str(directory))
    CommandVision._imread_or_raise.cache_clear()
    try:
        image = CommandVision._imread_or_raise("sample.png", cv2.IMREAD_COLOR)
        assert image.shape == (4, 6, 3)
    finally:
        CommandVision._imread_or_raise.cache_clear()
