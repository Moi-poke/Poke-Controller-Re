"""3Dプレビュー用の操作変換（実機不要）。"""

import math

import pytest


def _bit(name):
    from pokecon.domain.controller_preview import CONTROL_NAMES

    return 1 << CONTROL_NAMES.index(name)


def test_connected_neutral_is_all_zero():
    from pokecon.domain.controller_preview import PreviewControls, controls_from_pad
    from pokecon.domain.xinput import PadState

    controls = controls_from_pad(PadState(connected=True))
    assert controls == PreviewControls(connected=True)


def test_disconnected_clears_every_control():
    """未接続なら押下・スティックが残っていても全て中立へ戻す。"""
    from pokecon.domain.controller_preview import PreviewControls, controls_from_pad
    from pokecon.domain.xinput import PadState

    state = PadState(
        connected=False,
        buttons=0xFFFF,
        left_trigger=255,
        right_trigger=255,
        thumb_lx=32767,
        thumb_ly=-32768,
        thumb_rx=-32768,
        thumb_ry=32767,
        pressed=frozenset({"A", "DPAD_UP"}),
    )
    assert controls_from_pad(state) == PreviewControls()


def test_all_controls_accumulate_without_losing_simultaneous_presses():
    from pokecon.domain.controller_preview import controls_from_pad
    from pokecon.domain.xinput import PadState

    state = PadState(
        connected=True,
        left_trigger=31,
        right_trigger=255,
        pressed=frozenset(
            {
                "A",
                "B",
                "X",
                "Y",
                "START",
                "BACK",
                "HOME",
                "CAPTURE",
                "LEFT_SHOULDER",
                "RIGHT_SHOULDER",
                "LEFT_THUMB",
                "RIGHT_THUMB",
                "DPAD_UP",
                "DPAD_DOWN",
                "DPAD_LEFT",
                "DPAD_RIGHT",
                "SL",
                "SR",
            }
        ),
    )
    assert controls_from_pad(state).buttons == (1 << 20) - 1


@pytest.mark.parametrize("name", ["A", "B", "X", "Y"])
def test_face_button_keeps_its_physical_name(name):
    """1ボタンずつ押し、A/B・X/Yの取り違えを検出する。"""
    from pokecon.domain.controller_preview import controls_from_pad
    from pokecon.domain.xinput import PadState

    state = PadState(connected=True, pressed=frozenset({name}))
    assert controls_from_pad(state).buttons == _bit(name)


def test_trigger_threshold_matches_existing_deadzone():
    """既存の DEADZONE_TRIGGER を超えた分だけ ZL/ZR を押下にする。"""
    from pokecon.domain.controller_preview import controls_from_pad
    from pokecon.domain.xinput import DEADZONE_TRIGGER, PadState

    below = PadState(
        connected=True,
        left_trigger=DEADZONE_TRIGGER,
        right_trigger=DEADZONE_TRIGGER,
    )
    assert controls_from_pad(below).buttons == 0

    above = PadState(
        connected=True,
        left_trigger=DEADZONE_TRIGGER + 1,
        right_trigger=DEADZONE_TRIGGER + 1,
    )
    assert controls_from_pad(above).buttons == _bit("ZL") | _bit("ZR")


def test_sl_sr_pass_through_only_when_reported():
    """現行読取口は報告しないが、報告された名前は落とさない。"""
    from pokecon.domain.controller_preview import controls_from_pad
    from pokecon.domain.xinput import PadState

    controls = controls_from_pad(
        PadState(connected=True, pressed=frozenset({"SL", "SR"}))
    )
    assert controls.buttons == _bit("SL") | _bit("SR")


def test_stick_short_boundaries_are_asymmetric():
    """負は -32768、正は 32767 で割って両端を ±1.0 に届かせる。"""
    from pokecon.domain.controller_preview import controls_from_pad
    from pokecon.domain.xinput import PadState

    x = controls_from_pad(PadState(connected=True, thumb_lx=-32768, thumb_rx=32767))
    assert x.lx == -1.0
    assert x.rx == 1.0
    assert x.ly == x.ry == 0.0

    y = controls_from_pad(PadState(connected=True, thumb_ly=32767, thumb_ry=-32768))
    assert y.ly == 1.0
    assert y.ry == -1.0
    assert y.lx == y.rx == 0.0

    half = controls_from_pad(PadState(connected=True, thumb_lx=-16384, thumb_rx=16384))
    assert half.lx == -0.5
    assert half.rx == 16384 / 32767.0


def test_stick_has_no_deadzone_or_extra_scaling():
    from pokecon.domain.controller_preview import controls_from_pad
    from pokecon.domain.xinput import PadState

    controls = controls_from_pad(PadState(connected=True, thumb_lx=100, thumb_ly=-200))
    assert controls.lx == 100 / 32767.0
    assert controls.ly == -200 / 32768.0


def test_stick_diagonal_is_clamped_to_unit_circle():
    """斜め入力の半径を 1 に抑える（√2 倍にしない）。"""
    from pokecon.domain.controller_preview import controls_from_pad
    from pokecon.domain.xinput import PadState

    controls = controls_from_pad(
        PadState(connected=True, thumb_lx=32767, thumb_ly=32767)
    )
    assert math.isclose(controls.lx, math.sqrt(0.5), rel_tol=1e-12)
    assert math.isclose(controls.ly, math.sqrt(0.5), rel_tol=1e-12)
    assert math.isclose(math.hypot(controls.lx, controls.ly), 1.0, rel_tol=1e-12)


def test_nonfinite_axes_become_neutral():
    from pokecon.domain.controller_preview import controls_from_pad
    from pokecon.domain.xinput import PadState

    controls = controls_from_pad(
        PadState(
            connected=True,
            thumb_lx=float("nan"),  # type: ignore[arg-type]
            thumb_ly=float("inf"),  # type: ignore[arg-type]
        )
    )
    assert controls.lx == 0.0
    assert controls.ly == 0.0
