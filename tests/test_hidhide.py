"""HidHide の設定検出（実機不要）。"""

from __future__ import annotations

import sys

import pytest


def test_parse_ready_status() -> None:
    from core.hidhide import parse_cli_output

    out = "\n".join(
        [
            "--cloak-on",
            '--dev-hide "HID\\VID_057E&PID_2009&MI_00\\A&43A2154&0&0000"',
            f'--app-reg "{sys.executable}"',
        ]
    )
    status = parse_cli_output(out, (sys.executable,), cli_path="cli.exe")
    assert status.ready is True
    assert status.summary() == "HidHide準備完了"


def test_missing_cloak_or_device_is_not_ready() -> None:
    from core.hidhide import parse_cli_output

    out = f'--app-reg "{sys.executable}"'
    status = parse_cli_output(out, (sys.executable,), cli_path="cli.exe")
    assert status.installed is True
    assert status.ready is False
    assert "cloak" in status.summary()

    out2 = "\n".join(
        [
            "--cloak-on",
            '--dev-hide "HID\\VID_057E&PID_2009&MI_00\\A&43A2154&0&0000"',
        ]
    )
    status2 = parse_cli_output(out2, (sys.executable,), cli_path="cli.exe")
    assert status2.ready is False
    assert "許可アプリ" in status2.summary()


def test_detect_hidhide_uses_runner_and_caches() -> None:
    import core.hidhide as hh

    hh.clear_cache()
    out = "\n".join(
        [
            "--cloak-on",
            '--dev-hide "HID\\VID_057E&PID_2009&MI_00\\A&43A2154&0&0000"',
            f'--app-reg "{sys.executable}"',
        ]
    )
    calls: list[str] = []

    def runner(path: str):
        calls.append(path)
        return 0, out, ""

    status = hh.detect_hidhide(__file__, runner=runner)
    assert status.ready is True
    assert status.cli_path == __file__
    assert calls == [__file__]
    # キャッシュが効く（runnerを再度呼ばない）。
    assert hh.detect_hidhide(__file__, runner=runner).ready is True
    assert calls == [__file__]
    hh.clear_cache()


def test_detect_hidhide_missing_cli() -> None:
    import core.hidhide as hh

    hh.clear_cache()
    status = hh.detect_hidhide(
        "C:/does/not/exist/HidHideCLI.exe", runner=lambda _p: (0, "", "")
    )
    assert status.installed is False
    assert status.ready is False
    assert "インストール" in status.summary()


def test_detect_hidhide_does_not_cache_transient_failure() -> None:
    """CLIが一度失敗しても、次の呼び出しで読み直す（HID欄が消えたままにしない）。"""
    import core.hidhide as hh

    hh.clear_cache()
    out = "\n".join(
        [
            "--cloak-on",
            '--dev-hide "HID\\VID_057E&PID_2009&MI_00\\A&43A2154&0&0000"',
            f'--app-reg "{sys.executable}"',
        ]
    )
    results = [(1, "", "driver busy"), (0, out, "")]

    def runner(_path: str):
        return results.pop(0)

    first = hh.detect_hidhide(__file__, runner=runner)
    assert first.ready is False
    second = hh.detect_hidhide(__file__, runner=runner)
    assert second.ready is True
    hh.clear_cache()


def test_decode_output_falls_back_to_cp932() -> None:
    """CLIがcp932で書いた行を、復号失敗で落とさず読むこと。"""
    from core.hidhide import _decode_output

    text = '--dev-hide "HID\\VID_057E&PID_2009\\A&0&0000"  // ジャイロ'
    assert _decode_output(text.encode("cp932")).endswith("ジャイロ")
    # どちらでも読めないバイトは置換で返す（例外を投げない）。
    assert isinstance(_decode_output(b"\xff\xfe"), str)


def test_detect_hidhide_survives_cp932_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Given: CLIがcp932を返す / When: 状態を読む / Then: 読取スレッドを落とさず解析すること。"""
    import core.hidhide as hh

    hh.clear_cache()
    out = "\n".join(
        [
            "--cloak-on",
            '--dev-hide "HID\\VID_057E&PID_2009\\A&43A2154&0&0000"',
            f'--app-reg "{sys.executable}"',
            "// デバイス名: プロコントローラー",
        ]
    ).encode("cp932")

    class _Done:
        returncode = 0
        stdout = out
        stderr = b""

    monkeypatch.setattr(hh.subprocess, "run", lambda *a, **k: _Done())
    status = hh.detect_hidhide(__file__)
    assert status.ready is True
    hh.clear_cache()
