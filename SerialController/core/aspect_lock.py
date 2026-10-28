#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""aspect_lock.py - 「16:9 に固定」の寸法計算。

ウィンドウの外形（GetWindowRect。タイトルバー・メニュー・縁を含む）を 16:9 に
保つ。OBS のウィンドウキャプチャは「クライアント領域」の設定次第で枠込みの
外形を取るため、外形を合わせる。中身（クライアント領域）は枠の分だけ 16:9
からずれる（枠の縦横比が 16:9 でない限り、外形と中身の両方を同時に 16:9 に
することはできない）。掴んだ縁は指に付いて動かし、反対側の縁は動かさない。
Tk にも Win32 にも触れない（core 層）。

縁の番号は Win32 の WMSZ_* と同じ値にしてある（WM_SIZING の wParam を
そのまま渡せる）。
"""

from __future__ import annotations

import math
from typing import Final

RATIO_W: Final[int] = 16
RATIO_H: Final[int] = 9

# 掴んだ縁（WMSZ_*）。
LEFT: Final[int] = 1
RIGHT: Final[int] = 2
TOP: Final[int] = 3
TOPLEFT: Final[int] = 4
TOPRIGHT: Final[int] = 5
BOTTOM: Final[int] = 6
BOTTOMLEFT: Final[int] = 7
BOTTOMRIGHT: Final[int] = 8

_LEFT_SIDE: Final[frozenset[int]] = frozenset({LEFT, TOPLEFT, BOTTOMLEFT})
_TOP_SIDE: Final[frozenset[int]] = frozenset({TOP, TOPLEFT, TOPRIGHT})

Rect = tuple[int, int, int, int]  # left, top, right, bottom（外形）
Size = tuple[int, int]


def _minimum_outer(minimum: Size, frame: Size) -> Size:
    """中身の最小寸法を外形の最小寸法へ直す。frame は外形と中身の差。"""
    return max(1, minimum[0] + frame[0]), max(1, minimum[1] + frame[1])


def _from_width(width: int, minimum: Size) -> Size:
    min_w, min_h = minimum
    width = max(width, min_w, math.ceil(min_h * RATIO_W / RATIO_H), 1)
    return width, max(1, round(width * RATIO_H / RATIO_W))


def _from_height(height: int, minimum: Size) -> Size:
    min_w, min_h = minimum
    height = max(height, min_h, math.ceil(min_w * RATIO_H / RATIO_W), 1)
    return max(1, round(height * RATIO_W / RATIO_H)), height


def snap_outer(width: int, height: int, frame: Size, minimum: Size) -> Size:
    """固定を入れた瞬間の外形の寸法。今の幅を保ち、高さを 16:9 に合わせる。

    frame は外形と中身の差（タイトルバー・メニュー・縁の分）。minimum は
    中身の最小寸法（wm_minsize）。
    """
    del height  # 幅を基準にする（幅の方が画面の並びに効く）。
    return _from_width(width, _minimum_outer(minimum, frame))


def snap_client(width: int, height: int, *, minimum: Size) -> Size:
    """固定を入れた瞬間の中身の寸法（Windows 以外）。今の幅を保ち、高さを合わせる。"""
    del height
    return _from_width(width, minimum)


def locked_outer(edge: int, proposed: Size, frame: Size, minimum: Size) -> Size:
    """ドラッグ中の外形の寸法を 16:9 にする。

    左右の縁は幅、上下の縁は高さに合わせる。角はマウスの位置に一番近い
    16:9 の大きさ（16:9 の対角線へ投影した点）にする。角で「幅と高さの
    どちらが大きく動いたか」を毎回選ぶと、斜めに引いたときに数 px ごとに
    基準が入れ替わり、窓がガタガタと跳ねた。

    frame は外形と中身の差。minimum は中身の最小寸法なので、frame を足して
    外形の最小にしてから当てる。
    """
    minimum_outer = _minimum_outer(minimum, frame)
    width, height = proposed
    if edge in (LEFT, RIGHT):
        return _from_width(width, minimum_outer)
    if edge in (TOP, BOTTOM):
        return _from_height(height, minimum_outer)
    k = (width * RATIO_W + height * RATIO_H) / (RATIO_W**2 + RATIO_H**2)
    return _from_width(round(k * RATIO_W), minimum_outer)


def locked_rect(edge: int, rect: Rect, frame: Size, minimum: Size) -> Rect:
    """WM_SIZING の枠（外形）を 16:9 に直す。

    frame は外形と中身の差（タイトルバー・メニュー・縁の分）。minimum は
    中身の最小寸法。
    """
    left, top, right, bottom = rect
    outer_w, outer_h = locked_outer(edge, (right - left, bottom - top), frame, minimum)
    # 掴んだ側だけを動かし、反対側の縁は元の位置に残す。
    if edge in _LEFT_SIDE:
        left = right - outer_w
    else:
        right = left + outer_w
    if edge in _TOP_SIDE:
        top = bottom - outer_h
    else:
        bottom = top + outer_h
    return left, top, right, bottom
