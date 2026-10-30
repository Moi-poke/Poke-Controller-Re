"""DirectShow デバイス列挙の失敗時の契約（COM は呼ばない）。

一覧は起動時に呼ばれる。列挙に失敗しても GUI を止めず、空リストで
「カメラ無し」として続行する。Windows 以外では COM を触らない。
"""

from __future__ import annotations

import pytest
from core import dshow_devices


def test_non_windows_never_touches_com(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dshow_devices, "_IS_WINDOWS", False)

    assert dshow_devices.list_video_input_devices() == []


def test_enumeration_failure_returns_empty_instead_of_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom() -> list[dshow_devices.VideoInputDevice]:
        raise OSError("CoCreateInstance failed: 0x80070005")

    monkeypatch.setattr(dshow_devices, "_IS_WINDOWS", True)
    monkeypatch.setattr(dshow_devices, "_enumerate_windows", boom)

    assert dshow_devices.list_video_input_devices() == []
