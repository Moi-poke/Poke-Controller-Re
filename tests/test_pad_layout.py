"""仮想コントローラの並びと当たり判定（core.pad_layout、Tk を使わない）。

ボタンの位置・大きさ・押した場所からどのボタンかを決める判断をここに閉じ、
描画（GuiAssets.ControllerGUI）は形を拡大縮小して描くだけにする。
"""

from __future__ import annotations

import math

import pytest
from Commands.Keys import Button
from core.pad_layout import (
    AUTO,
    DPAD_NAMES,
    JOYCON_L,
    JOYCON_R,
    MIN_TARGET,
    PAD_H,
    PAD_W,
    PROCON,
    SHAPES,
    STICK_REACH,
    detect_layout,
    fit_scale,
    hit_test,
    layout_shapes,
    layout_size,
    resolve_layout,
    stick_value,
)

_BUTTONS = [
    "A",
    "B",
    "X",
    "Y",
    "L",
    "R",
    "ZL",
    "ZR",
    "LCLICK",
    "RCLICK",
    "MINUS",
    "PLUS",
    "CAPTURE",
    "HOME",
]


def test_every_switch_button_has_exactly_one_shape_named_after_the_button_enum() -> (
    None
):
    # Then: 名前は Commands.Keys.Button の属性そのまま（送信側でそのまま引ける）。
    names = [s.name for s in SHAPES if s.kind != "dpad"]
    assert sorted(names) == sorted(_BUTTONS)
    for name in names:
        assert hasattr(Button, name)
    assert [s.kind for s in SHAPES].count("dpad") == 1


@pytest.mark.parametrize("name", _BUTTONS)
def test_pressing_the_centre_of_a_button_hits_that_button(name: str) -> None:
    shape = next(s for s in SHAPES if s.name == name)
    assert hit_test(shape.cx, shape.cy) == name


@pytest.mark.parametrize(
    ("angle", "name"),
    [
        (0, "RIGHT"),
        (45, "UP_RIGHT"),
        (90, "UP"),
        (135, "UP_LEFT"),
        (180, "LEFT"),
        (225, "DOWN_LEFT"),
        (270, "DOWN"),
        (315, "DOWN_RIGHT"),
    ],
)
def test_the_dpad_maps_the_pressed_direction_including_diagonals(
    angle: int, name: str
) -> None:
    # Given: 十字キーの中心から、指定の向きへ少し離れた点（画面座標は y が下向き）。
    pad = next(s for s in SHAPES if s.kind == "dpad")
    r = pad.w / 2 * 0.7
    x = pad.cx + r * math.cos(math.radians(angle))
    y = pad.cy - r * math.sin(math.radians(angle))

    # Then: 8 方向のどれかになる。斜めは角を押せば入る（旧 UI の斜めボタンの代わり）。
    assert hit_test(x, y) == name
    assert name in DPAD_NAMES


def test_the_dpad_centre_is_a_dead_zone() -> None:
    # Then: 真ん中では向きが決まらないので何も押さない。
    pad = next(s for s in SHAPES if s.kind == "dpad")
    assert hit_test(pad.cx, pad.cy) is None


def test_empty_body_and_outside_the_pad_hit_nothing() -> None:
    assert hit_test(PAD_W / 2, PAD_H - 4) is None
    assert hit_test(-10, -10) is None
    assert hit_test(PAD_W + 10, PAD_H / 2) is None


def test_targets_are_big_enough_and_do_not_overlap_inside_the_pad() -> None:
    # Then: 等倍（96dpi で 1 単位 = 1px）でも 24px 以上の的にする（WCAG 2.2 の最小）。
    boxes = []
    for s in SHAPES:
        assert s.w >= MIN_TARGET and s.h >= MIN_TARGET, s.name
        x0, y0, x1, y1 = s.cx - s.w / 2, s.cy - s.h / 2, s.cx + s.w / 2, s.cy + s.h / 2
        assert 0 <= x0 and x1 <= PAD_W and 0 <= y0 and y1 <= PAD_H, s.name
        boxes.append((s.name, x0, y0, x1, y1))
    # 的同士が重なると、押した場所と違うボタンが入る。
    for i, (na, ax0, ay0, ax1, ay1) in enumerate(boxes):
        for nb, bx0, by0, bx1, by1 in boxes[i + 1 :]:
            assert ax1 <= bx0 or bx1 <= ax0 or ay1 <= by0 or by1 <= ay0, (na, nb)


def test_the_pad_is_far_smaller_than_the_old_600x300_window() -> None:
    # Then: 旧 UI（600x300 の窓）の半分以下の面積で全部のボタンが並ぶ。
    assert PAD_W * PAD_H <= 600 * 300 / 2


def test_fit_scale_follows_the_space_but_keeps_the_aspect_and_a_cap() -> None:
    # Then: 幅・高さの狭い方に合わせる（縦横比を保つ）。
    assert fit_scale(PAD_W, PAD_H * 10, base=1.0) == pytest.approx(1.0)
    assert fit_scale(PAD_W * 10, PAD_H, base=1.0) == pytest.approx(1.0)
    assert fit_scale(PAD_W / 2, PAD_H, base=1.0) == pytest.approx(0.5)
    # 広い所でも大きくなりすぎない（画面を占領しない）。
    assert fit_scale(PAD_W * 10, PAD_H * 10, base=1.0) == pytest.approx(1.5)
    # 高 DPI では上限も DPI 倍。
    assert fit_scale(PAD_W * 10, PAD_H * 10, base=2.0) == pytest.approx(3.0)
    # 極端に狭くても潰れきらない（0 や負で描画が壊れない）。
    assert fit_scale(1, 1, base=1.0) > 0.2
    # タブに埋め込むときは等倍まで（max_zoom=1）。別窓は大きくできる。
    assert fit_scale(PAD_W * 10, PAD_H * 10, base=1.0, max_zoom=1.0) == 1.0
    assert fit_scale(PAD_W * 3, PAD_H * 3, base=1.0, max_zoom=3.0) == 3.0


@pytest.mark.parametrize(
    ("dx", "dy", "expected"),
    [
        # 中心は中立。
        (0, 0, (128, 128)),
        # 右へ倒し切る / 上へ倒し切る（画面座標は y が下向き、値は上が 0）。
        (STICK_REACH, 0, (255, 128)),
        (0, -STICK_REACH, (128, 0)),
        (-STICK_REACH, 0, (0, 128)),
        (0, STICK_REACH, (128, 255)),
        # 外まで引っ張っても倒し切り止まり（値の範囲を超えない）。
        (STICK_REACH * 5, 0, (255, 128)),
        # 半分だけ倒す。
        (STICK_REACH / 2, 0, (191, 128)),
    ],
)
def test_stick_value_turns_the_drag_offset_into_0_to_255_coordinates(
    dx: float, dy: float, expected: tuple[int, int]
) -> None:
    assert stick_value(dx, dy) == expected


def test_stick_value_keeps_diagonals_inside_the_circle() -> None:
    # Then: 斜めに引っ張り切っても、円の外（両軸とも端）にはならない。
    x, y = stick_value(STICK_REACH * 3, -STICK_REACH * 3)
    assert math.hypot(x - 128, y - 128) <= 128
    assert x > 200 and y < 55


def test_joy_con_layouts_only_hold_the_buttons_on_their_own_side() -> None:
    # Then: Joy-Con (L) に ABXY や右スティック、Joy-Con (R) に十字キーは無い。
    left = {s.name for s in layout_shapes(JOYCON_L)}
    right = {s.name for s in layout_shapes(JOYCON_R)}
    assert left == {
        "ZL",
        "L",
        "MINUS",
        "LCLICK",
        "UP",
        "RIGHT",
        "DOWN",
        "LEFT",
        "CAPTURE",
    }
    assert right == {"ZR", "R", "PLUS", "X", "Y", "A", "B", "RCLICK", "HOME"}


@pytest.mark.parametrize("layout", (PROCON, JOYCON_L, JOYCON_R))
def test_every_layout_has_big_non_overlapping_targets_inside_the_body(
    layout: str,
) -> None:
    # Then: 等倍でも 24px 以上・本体の内側・互いに重ならない。
    pad_w, pad_h = layout_size(layout)
    boxes = []
    for shape in layout_shapes(layout):
        assert shape.w >= MIN_TARGET and shape.h >= MIN_TARGET, shape.name
        x0, y0 = shape.cx - shape.w / 2, shape.cy - shape.h / 2
        x1, y1 = shape.cx + shape.w / 2, shape.cy + shape.h / 2
        assert 0 <= x0 and x1 <= pad_w and 0 <= y0 and y1 <= pad_h, shape.name
        boxes.append((shape.name, x0, y0, x1, y1))
    for i, (na, ax0, ay0, ax1, ay1) in enumerate(boxes):
        for nb, bx0, by0, bx1, by1 in boxes[i + 1 :]:
            assert ax1 <= bx0 or bx1 <= ax0 or ay1 <= by0 or by1 <= ay0, (
                layout,
                na,
                nb,
            )


@pytest.mark.parametrize("layout", (PROCON, JOYCON_L, JOYCON_R))
def test_hitting_the_centre_of_a_shape_names_that_shape(layout: str) -> None:
    # Then: 十字キー（ProCon の dpad）の中心以外は、その名前が返る。
    for shape in layout_shapes(layout):
        if shape.kind == "dpad":
            continue
        assert hit_test(shape.cx, shape.cy, layout) == shape.name, shape.name


@pytest.mark.parametrize("layout", (PROCON, JOYCON_L, JOYCON_R))
def test_outside_each_layout_hits_nothing(layout: str) -> None:
    pad_w, pad_h = layout_size(layout)
    assert hit_test(-10, -10, layout) is None
    assert hit_test(pad_w + 10, pad_h / 2, layout) is None
    assert hit_test(pad_w / 2, pad_h + 10, layout) is None


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Joy-Con (L)", JOYCON_L),
        ("Joy-Con (R)", JOYCON_R),
        ("Nintendo Switch Joy-Con L", JOYCON_L),
        ("JoyCon(R)", JOYCON_R),
        ("Joy-Con Left", JOYCON_L),
        ("Joy-Con (L/R)", None),  # 左右の組は 1 台に決められない
        ("Joy-Con", None),
        ("Pro Controller", None),
        ("XInput パッド", None),
        ("", None),
    ],
)
def test_detect_layout_reads_the_side_from_a_pad_name(
    name: str, expected: str | None
) -> None:
    assert detect_layout(name) == expected


def test_resolve_layout_honours_a_manual_choice_and_defaults_to_procon() -> None:
    # Then: 手で選んだ形はパッド名より優先し、auto は名前から決める。
    assert resolve_layout(JOYCON_R, "Joy-Con (L)") == JOYCON_R
    assert resolve_layout(AUTO, "Joy-Con (L)") == JOYCON_L
    assert resolve_layout(AUTO, "Joy-Con (R)") == JOYCON_R
    assert resolve_layout(AUTO, "Pro Controller") == PROCON
    assert resolve_layout("unknown", "Joy-Con (L)") == JOYCON_L
    assert resolve_layout(AUTO, "") == PROCON


def test_joy_con_fit_scale_uses_the_tall_body() -> None:
    # Then: 縦長の Joy-Con は、高さで倍率が決まる（ProCon の幅基準ではない）。
    pad_w, pad_h = layout_size(JOYCON_L)
    assert (pad_w, pad_h) == (176, 300)
    assert fit_scale(pad_w, pad_h, base=1.0, pad_w=pad_w, pad_h=pad_h) == pytest.approx(
        1.0
    )
    assert fit_scale(
        pad_w / 2, pad_h, base=1.0, pad_w=pad_w, pad_h=pad_h
    ) == pytest.approx(0.5)
