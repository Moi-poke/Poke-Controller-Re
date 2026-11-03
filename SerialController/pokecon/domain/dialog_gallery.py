#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""選択肢グリッド用のサムネイル生成（GUI非依存）。

`cv2.imread` は Windows の非 ASCII パスで失敗するため、`np.fromfile` で
バイトを読んで `cv2.imdecode` する。アルファ付き画像は背景色に合成し、
小さい画像は最近傍で拡大する（ぼやけさせない）。返すのは PNG の
バイト列で、Tk 側が `PhotoImage` にする。
"""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np

#: 既定の背景色（RGB）。Tk の淡いグレーに合わせる。
DEFAULT_BACKGROUND = (240, 240, 240)


def decode_image(path: str | Path) -> np.ndarray | None:
    """画像を読む。読めなければ None（例外にしない）。"""
    try:
        raw = np.fromfile(str(path), dtype=np.uint8)
    except OSError:
        return None
    if raw.size == 0:
        return None
    image = cv2.imdecode(raw, cv2.IMREAD_UNCHANGED)
    if image is None or image.size == 0:
        return None
    return image


def _flatten(image: np.ndarray, background: tuple[int, int, int]) -> np.ndarray:
    """アルファを背景に合成し、3ch BGR にする。"""
    if image.ndim == 2:
        return cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)
    if image.shape[2] == 4:
        alpha = image[:, :, 3:4].astype(np.float32) / 255.0
        bgr = image[:, :, :3].astype(np.float32)
        # background は RGB。BGR に直してから合成する。
        bg = np.array(background[::-1], dtype=np.float32)
        return (bgr * alpha + bg * (1.0 - alpha)).astype(np.uint8)
    if image.shape[2] == 1:
        return cv2.cvtColor(image[:, :, 0], cv2.COLOR_GRAY2BGR)
    return image[:, :, :3]


def thumbnail_png(
    path: str | Path,
    *,
    size: int = 40,
    background: tuple[int, int, int] = DEFAULT_BACKGROUND,
) -> bytes | None:
    """`size` 四方のサムネイル PNG を返す。読めなければ None。

    アスペクト比を保って収め、余白は背景色で埋める。元が小さいときは
    最近傍で拡大し、大きいときは面積平均で縮小する。
    """
    if size <= 0:
        return None
    image = decode_image(path)
    if image is None:
        return None
    image = _flatten(image, background)
    height, width = image.shape[:2]
    if height <= 0 or width <= 0:
        return None
    scale = min(size / width, size / height)
    new_w = max(1, min(size, int(round(width * scale))))
    new_h = max(1, min(size, int(round(height * scale))))
    interpolation = cv2.INTER_NEAREST if scale > 1.0 else cv2.INTER_AREA
    resized = cv2.resize(image, (new_w, new_h), interpolation=interpolation)
    canvas = np.full((size, size, 3), background[::-1], dtype=np.uint8)
    x = (size - new_w) // 2
    y = (size - new_h) // 2
    canvas[y : y + new_h, x : x + new_w] = resized
    ok, encoded = cv2.imencode(".png", canvas)
    if not ok:
        return None
    return encoded.tobytes()
