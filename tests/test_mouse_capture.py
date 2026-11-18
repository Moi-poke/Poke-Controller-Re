# -*- coding: utf-8 -*-
"""Raw Inputキャプチャのライフサイクル（Windowsのみ）。

実際のマウス移動はCIで再現できないため、非表示ウィンドウの作成・
Raw Inputの登録・開始/停止の往復だけを確かめる。WM_INPUTの解釈は
実機（手で動かす）で確認する。
"""

from __future__ import annotations

import pytest
from pokecon.domain.mouse_capture import (
    IS_WINDOWS,
    RESET_HOTKEY_ID,
    WM_HOTKEY,
    RawMouseCapture,
)


@pytest.mark.skipif(not IS_WINDOWS, reason="Windows Raw Input")
def test_raw_capture_start_enable_stop_and_restart() -> None:
    logs: list[str] = []
    states: list[tuple[bool, str]] = []
    capture = RawMouseCapture(
        on_motion=lambda dx, dy, roll=False: None,
        on_state=lambda enabled, reason: states.append((enabled, reason)),
        on_log=logs.append,
        hotkey="",
    )
    try:
        assert capture.start() is True
        assert capture.enabled() is False
        first_hwnd = capture._hwnd
        assert first_hwnd

        capture.set_enabled(True, "test")
        assert capture.enabled() is True
        capture.set_enabled(False, "test")
        assert capture.enabled() is False
        assert states == [(True, "test"), (False, "test")]

        capture.stop()
        assert capture._thread is None
        assert capture.enabled() is False

        # クラス登録を残しても、2回目の窓作成ができる。
        assert capture.start() is True
        assert capture._hwnd
        capture.stop()
        assert capture._thread is None
    finally:
        capture.stop()
    assert logs == []


def test_reset_hotkey_message_calls_callback() -> None:
    """リセット用WM_HOTKEYは姿勢リセットのコールバックへ渡る。"""
    calls: list[str] = []
    capture = RawMouseCapture(
        on_motion=lambda dx, dy, roll=False: None,
        on_state=lambda enabled, reason: None,
        hotkey="",
        reset_hotkey="",
        on_reset=lambda: calls.append("reset"),
    )
    capture._handle_message(0, WM_HOTKEY, RESET_HOTKEY_ID, 0)
    assert calls == ["reset"]
