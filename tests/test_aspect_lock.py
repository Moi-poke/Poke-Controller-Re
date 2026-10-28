"""16:9 固定の寸法計算（core.aspect_lock）。Tk を作らない。

ウィンドウの縁をドラッグしている途中の枠（WM_SIZING の RECT）を、外形
（タイトルバー・メニュー・縁を含む GetWindowRect）が 16:9 になるよう直す。
OBS のウィンドウキャプチャは枠込みで取ることがあるため、中身ではなく外形を
基準にする。掴んだ縁は利用者の指に付いて動き、反対側の縁は動かさない。
"""

from __future__ import annotations

import pytest
from core.aspect_lock import (
    BOTTOM,
    BOTTOMLEFT,
    BOTTOMRIGHT,
    LEFT,
    RIGHT,
    TOP,
    TOPLEFT,
    TOPRIGHT,
    locked_outer,
    locked_rect,
    snap_client,
    snap_outer,
)

# 枠（タイトルバー・メニュー・縁）の分。外形は中身にこれを足したもの。
FRAME = (18, 72)


def _outer(rect: tuple[int, int, int, int]) -> tuple[int, int]:
    return rect[2] - rect[0], rect[3] - rect[1]


def _client(rect: tuple[int, int, int, int]) -> tuple[int, int]:
    width, height = _outer(rect)
    return width - FRAME[0], height - FRAME[1]


def _rect(width: int, height: int) -> tuple[int, int, int, int]:
    """外形が width x height の矩形。"""
    return (100, 100, 100 + width, 100 + height)


def _assert_16_to_9(width: int, height: int) -> None:
    assert abs(width * 9 - height * 16) <= 16


@pytest.mark.parametrize(
    "edge", [LEFT, RIGHT, TOP, BOTTOM, TOPLEFT, TOPRIGHT, BOTTOMLEFT, BOTTOMRIGHT]
)
def test_any_drag_keeps_the_frame_inclusive_rect_at_16_to_9(edge: int) -> None:
    # When: the user drags an edge to an arbitrary, non-16:9 size.
    rect = locked_rect(edge, _rect(1100, 450), FRAME, minimum=(1, 1))

    # Then: 枠込みの外形が 16:9（整数に丸めた 1px 以内）。
    _assert_16_to_9(*_outer(rect))


def test_dragging_the_right_edge_sets_the_height_from_the_width() -> None:
    rect = locked_rect(RIGHT, _rect(1120, 450), FRAME, (1, 1))

    # Then: 横に広げた分だけ下へ伸びる。左上は動かない。
    assert _outer(rect) == (1120, 630)
    assert rect[:2] == (100, 100)


def test_dragging_the_bottom_edge_sets_the_width_from_the_height() -> None:
    # Given: 下の縁だけを引き下げた（幅は元のまま）。
    rect = locked_rect(BOTTOM, _rect(800, 630), FRAME, (1, 1))

    # Then: 高さに合わせて幅が広がる（下の縁のドラッグが打ち消されない）。
    assert _outer(rect) == (1120, 630)
    assert rect[:2] == (100, 100)


def test_dragging_the_left_edge_keeps_the_right_edge_in_place() -> None:
    proposed = (0, 100, 1000, 550)

    rect = locked_rect(LEFT, proposed, FRAME, (1, 1))

    # Then: 掴んだ左の縁が指に付いて動き、右の縁は動かない。
    assert (rect[0], rect[2]) == (0, 1000)
    assert _outer(rect) == (1000, 562)


def test_dragging_the_top_left_corner_keeps_the_bottom_right_corner() -> None:
    right, bottom = 100 + 800, 100 + 450
    proposed = (0, 0, right, bottom)

    rect = locked_rect(TOPLEFT, proposed, FRAME, (1, 1))

    # Then: 反対の角（右下）は動かず、マウスに付いて広がる。
    assert (rect[2], rect[3]) == (right, bottom)
    _assert_16_to_9(*_outer(rect))


def test_a_mostly_vertical_corner_drag_still_grows_the_window() -> None:
    # Given: 右下の角を、ほとんど真下へ引いた。
    rect = locked_rect(BOTTOMRIGHT, _rect(810, 630), FRAME, (1, 1))

    # Then: 縦に引いた分も効いて大きくなる（横の動きが小さくても止まらない）。
    width, height = _outer(rect)
    assert width > 810
    assert height > 450
    _assert_16_to_9(width, height)


def test_the_locked_size_never_goes_below_the_client_minimum() -> None:
    # When: dragged far smaller than the window's minimum size.
    rect = locked_rect(BOTTOMLEFT, _rect(50, 40), FRAME, (640, 300))

    # Then: 最小寸法の両方を満たす一番小さい 16:9 で止まる。
    width, height = _outer(rect)
    client_w, client_h = _client(rect)
    assert client_w >= 640
    assert client_h >= 300
    _assert_16_to_9(width, height)


def test_the_minimum_is_a_client_size_so_the_frame_is_added() -> None:
    # Given: 中身の最小が 640x300。外形の最小は frame を足した 658x372。
    width, height = locked_outer(BOTTOM, (100, 300), FRAME, (640, 300))

    # Then: 高さは外形 372 で止まり、中身はちょうど 640x300 以上になる。
    assert (width, height) == (661, 372)
    assert _client((0, 0, width, height)) == (643, 300)


def test_snapping_on_enable_preserves_the_outer_width_and_fixes_the_height() -> None:
    # Then: 有効にした瞬間は今の外形の幅を保ち、高さを 16:9 に合わせる。
    assert snap_outer(1298, 792, FRAME, (1, 1)) == (1298, 730)
    assert snap_outer(800, 300, FRAME, (640, 480)) == (982, 552)


def test_snapping_never_goes_below_the_client_minimum() -> None:
    width, height = snap_outer(100, 100, FRAME, (640, 300))

    client_w, client_h = width - FRAME[0], height - FRAME[1]
    assert client_w >= 640
    assert client_h >= 300
    _assert_16_to_9(width, height)


def test_snapping_the_client_on_non_windows_keeps_the_width() -> None:
    # Then: Windows 以外（wm_aspect）は中身の幅を保ち、高さを合わせる。
    assert snap_client(1280, 900, minimum=(1, 1)) == (1280, 720)
    assert snap_client(800, 300, minimum=(640, 480)) == (854, 480)


def test_a_diagonal_corner_drag_grows_smoothly_without_jumping_back() -> None:
    # Given: 右下の角を斜めに引いている。Windows はマウスの位置から作った枠を
    #        数 px ずつ送ってくる（マウスの軌跡は 16:9 から少しずれ、揺れる）。
    size = (800, 450)
    sizes = [size]
    for i in range(1, 60):
        wobble = 3 if i % 2 else -3
        cursor = (800 + 8 * i + wobble, 450 + 5 * i - wobble)
        size = locked_outer(BOTTOMRIGHT, cursor, FRAME, (1, 1))
        sizes.append(size)

    # Then: 大きさは引くにつれて単調に増え、途中で跳ね戻らない（ガタつかない）。
    widths = [w for w, _h in sizes]
    assert widths == sorted(widths)
    steps = [b - a for a, b in zip(widths, widths[1:], strict=False)]
    assert max(steps) <= 16
