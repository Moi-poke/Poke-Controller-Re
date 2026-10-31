"""実時刻サンプルを5ms格子へ並べる再標本化のhostベクタ（実機不要）。"""

from __future__ import annotations

import pytest


def test_empty_events() -> None:
    from core.motion_track import resample_to_grid

    assert resample_to_grid([]) == (0, [])


def test_holds_last_value_between_events() -> None:
    """5ms格子に前値ホールドで並べ、先頭の遅れはoffsetで返す。"""
    from core.motion_track import resample_to_grid

    events = [
        (10.010, (0, 0, 4096, 100, 0, 0)),
        (10.030, (0, 0, 4096, 300, 0, 0)),
    ]
    offset_us, samples = resample_to_grid(events, origin_s=10.0)
    assert offset_us == 10_000
    # 10ms から 30ms まで 5ms 刻み = 5件。
    assert len(samples) == 5
    assert samples[0] == (0, 0, 4096, 100, 0, 0)
    assert samples[1] == (0, 0, 4096, 100, 0, 0)
    assert samples[-1] == (0, 0, 4096, 300, 0, 0)


def test_long_gap_becomes_rest() -> None:
    """前値ホールドの上限を超える空白は静止値で埋める。"""
    from core.motion_track import REST_SAMPLE, resample_to_grid

    events = [
        (5.000, (0, 0, 4096, 1000, 0, 0)),
        (5.600, (0, 0, 4096, 2000, 0, 0)),
    ]
    _, samples = resample_to_grid(events, origin_s=5.0)
    assert samples[0] == (0, 0, 4096, 1000, 0, 0)
    # 100ms後はまだ前値（ホールド上限0.25秒以内）。
    assert samples[20] == (0, 0, 4096, 1000, 0, 0)
    # 300ms後は静止へ戻る。
    assert samples[60] == REST_SAMPLE
    assert samples[-1] == (0, 0, 4096, 2000, 0, 0)


def test_unsorted_events_are_sorted() -> None:
    from core.motion_track import resample_to_grid

    offset_us, samples = resample_to_grid(
        [(1.010, (0, 0, 4096, 200, 0, 0)), (1.000, (0, 0, 4096, 100, 0, 0))],
        origin_s=1.0,
    )
    assert offset_us == 0
    assert samples[0] == (0, 0, 4096, 100, 0, 0)
    assert samples[-1] == (0, 0, 4096, 200, 0, 0)


def test_too_long_recording_raises() -> None:
    from core.motion_track import MAX_GRID_SAMPLES, resample_to_grid

    period_s = 0.005
    span = MAX_GRID_SAMPLES * period_s
    with pytest.raises(ValueError, match="長すぎ"):
        resample_to_grid([(0.0, (0, 0, 4096, 0, 0, 0)), (span, (0, 0, 4096, 0, 0, 0))])


def test_end_s_extends_track_with_rest() -> None:
    """マクロの終わりまで、最後の角速度を積分しないよう静止で延ばす。"""
    from core.motion_track import REST_SAMPLE, resample_to_grid

    events = [(2.000, (0, 0, 4096, 1000, 0, 0))]
    _, samples = resample_to_grid(events, origin_s=2.0, end_s=2.5)
    assert len(samples) == 101
    # 直後は前値（0.25秒以内）、その後は静止。
    assert samples[10] == (0, 0, 4096, 1000, 0, 0)
    assert samples[60] == REST_SAMPLE
    assert samples[-1] == REST_SAMPLE


def test_samples_before_origin_are_trimmed() -> None:
    """起点より前のサンプルは捨て、起点の値は直前のサンプルで始める。"""
    from core.motion_track import resample_to_grid

    events = [
        (1.000, (0, 0, 4096, 100, 0, 0)),
        (1.100, (0, 0, 4096, 200, 0, 0)),
    ]
    offset_us, samples = resample_to_grid(events, origin_s=1.050)
    assert offset_us == 0
    assert len(samples) == 11
    assert samples[0] == (0, 0, 4096, 100, 0, 0)
    assert samples[-1] == (0, 0, 4096, 200, 0, 0)
