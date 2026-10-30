"""HidHide 設定の組み立て・実行・画面（実機不要）。"""

from __future__ import annotations

import json
from typing import Any

import pytest

_SWITCH_HID = r"HID\VID_057E&PID_2009&MI_00\A&1&0&0000"
_SWITCH_USB = r"USB\VID_057E&PID_2009\5&2&0&1"
_SWITCH_XUSB = r"USB\VID_057E&PID_2009&IG_00\6&3&0&2"
_SONY_HID = r"HID\VID_054C&PID_0CE6&MI_00\B&1&0&0000"
_SONY_USB = r"USB\VID_054C&PID_0CE6\5&4&0&1"

_INVENTORY = json.dumps(
    [
        {
            "friendlyName": "Pro Controller",
            "devices": [
                {
                    "present": True,
                    "vendor": "Nintendo Co., Ltd.",
                    "product": "Pro Controller",
                    "deviceInstancePath": _SWITCH_HID,
                    "baseContainerDeviceInstancePath": _SWITCH_USB,
                    "xusbDeviceInstancePath": _SWITCH_XUSB,
                }
            ],
        },
        {
            "friendlyName": "Wireless Controller",
            "devices": [
                {
                    "present": True,
                    "vendor": "Sony Interactive Entertainment",
                    "product": "DualSense",
                    "deviceInstancePath": _SONY_HID,
                    "baseContainerDeviceInstancePath": _SONY_USB,
                    "xusbDeviceInstancePath": "",
                }
            ],
        },
    ]
)


def _runner(
    calls: list[list[str]], *, code: int = 0, out: str = "", err: str = ""
) -> Any:
    def run(_cli: str, args: list[str]) -> tuple[int, str, str]:
        calls.append(list(args))
        return code, out, err

    return run


def test_inventory_finds_switch_paths_and_ignores_other_pads() -> None:
    from core.hidhide_setup import parse_inventory, switch_devices, switch_hide_paths

    devices = parse_inventory(_INVENTORY)

    # Given: Switch と Sony の 2 台。When: Switch だけを絞る。
    assert [d.device_instance_path for d in switch_devices(devices)] == [_SWITCH_HID]
    # Then: HID 子・USB 親・XUSB の順で、重複なく hide 対象になる。
    assert switch_hide_paths(devices) == (_SWITCH_HID, _SWITCH_USB, _SWITCH_XUSB)


def test_inventory_matches_compatible_vid_without_vendor_name() -> None:
    from core.hidhide_setup import parse_inventory, switch_hide_paths

    text = json.dumps(
        [
            {
                "deviceInstancePath": r"HID\VID_2009&PID_0001\C&1&0&0000",
                "baseContainerDeviceInstancePath": "",
            }
        ]
    )
    assert switch_hide_paths(parse_inventory(text)) == (
        r"HID\VID_2009&PID_0001\C&1&0&0000",
    )


def test_inventory_survives_broken_or_prefixed_output() -> None:
    from core.hidhide_setup import parse_inventory

    assert parse_inventory("") == ()
    assert parse_inventory("not json") == ()
    assert parse_inventory("[]") == ()
    # 案内文が前に混ざっても JSON 部分だけを読む。
    assert len(parse_inventory(f"HidHide CLIENT\n{_INVENTORY}")) == 2


def test_apply_registers_app_hides_devices_and_turns_cloak_on() -> None:
    from core.hidhide_setup import apply_recommended

    calls: list[list[str]] = []

    run = apply_recommended(
        "/cli/HidHideCLI.exe",
        executables=("/app/python.exe", "/uv/python.exe"),
        hide_paths=(_SWITCH_HID, _SWITCH_USB),
        runner=_runner(calls),
    )

    assert run.ok is True
    # Given: 許可 2 件・hide 2 件。Then: 1 回の呼び出しにまとめる
    #       （CLI は正常終了時にまとめて書き込む）。
    assert calls == [
        [
            "--app-reg",
            "/app/python.exe",
            "--app-reg",
            "/uv/python.exe",
            "--dev-hide",
            _SWITCH_HID,
            "--dev-hide",
            _SWITCH_USB,
            "--cloak-on",
            "--inv-off",
        ]
    ]


def test_read_game_devices_returns_run_failure() -> None:
    from core.hidhide_setup import read_game_devices

    calls: list[list[str]] = []

    devices, run = read_game_devices(
        "/cli/HidHideCLI.exe",
        runner=_runner(calls, code=1, err="cannot open \\\\.\\HidHide"),
    )

    assert devices == ()
    assert run.ok is False
    assert "cannot open" in run.error
    assert calls == [["--dev-gaming"]]


def test_read_game_devices_parses_inventory() -> None:
    from core.hidhide_setup import read_game_devices

    devices, run = read_game_devices(
        "/cli/HidHideCLI.exe", runner=_runner([], out=_INVENTORY)
    )

    assert run.ok is True
    assert len(devices) == 2


def test_cloak_is_toggled_without_touching_lists() -> None:
    from core.hidhide_setup import set_cloak

    calls: list[list[str]] = []

    set_cloak("/cli/HidHideCLI.exe", True, runner=_runner(calls))
    set_cloak("/cli/HidHideCLI.exe", False, runner=_runner(calls))

    assert calls == [["--cloak-on"], ["--cloak-off"]]


def test_release_only_unhides_switch_devices_and_unregisters_app() -> None:
    from core.hidhide_setup import release_setup

    calls: list[list[str]] = []

    run = release_setup(
        "/cli/HidHideCLI.exe",
        # Given: Switch 2 件と他社パッド 1 件が hide 済み。
        hide_paths=(_SWITCH_HID, _SONY_HID, _SWITCH_USB),
        executables=("/app/python.exe",),
        runner=_runner(calls),
    )

    assert run.ok is True
    # Then: 他社パッドの hide は解除しない（他のアプリの設定を壊さない）。
    assert calls == [
        [
            "--cloak-off",
            "--dev-unhide",
            _SWITCH_HID,
            "--dev-unhide",
            _SWITCH_USB,
            "--app-unreg",
            "/app/python.exe",
        ]
    ]


def test_missing_cli_is_reported_without_running_anything() -> None:
    from core.hidhide_setup import failure_message, run_cli

    run = run_cli("/does/not/exist/HidHideCLI.exe", ["--cloak-on"])

    assert run.ok is False
    assert "見つかりません" in run.error
    assert "設定アプリを開いている" in failure_message(run)


def test_failed_run_keeps_the_cli_message_and_hint() -> None:
    from core.hidhide_setup import failure_message

    calls: list[list[str]] = []
    from core.hidhide_setup import run_cli

    run = run_cli(
        __file__, ["--cloak-on"], runner=_runner(calls, code=2, err="access denied")
    )

    message = failure_message(run)
    assert "access denied" in message
    assert "設定アプリを開いている" in message


def test_inventory_falls_back_to_os_enumeration_when_json_is_truncated() -> None:
    from core.hidhide_setup import read_game_devices

    # Given: HidHideCLI 1.5.230 の実出力のように JSON が途中で止まる。
    truncated = (
        ' [ { "friendlyName" : "Nintendo Co., Ltd. Pro Controller" , '
        '"devices" : [\n{ "present" : true ,\n"description" : "HID '
    )
    devices, run = read_game_devices(
        "/cli/HidHideCLI.exe",
        runner=_runner([], out=truncated),
        enumerator=lambda: (_SWITCH_HID,),
    )

    assert run.ok is True
    assert [d.device_instance_path for d in devices] == [_SWITCH_HID]


def test_complete_inventory_does_not_use_the_fallback() -> None:
    from core.hidhide_setup import read_game_devices

    def fail() -> tuple[str, ...]:
        raise AssertionError("完全な JSON で OS 列挙を呼んではいけない")

    devices, _run = read_game_devices(
        "/cli/HidHideCLI.exe",
        runner=_runner([], out=_INVENTORY),
        enumerator=fail,
    )

    assert len(devices) == 2


def test_current_device_must_be_in_the_hide_list() -> None:
    from core.hidhide import HidHideStatus
    from core.hidhide_setup import GamingDevice, switch_hidden_in_status

    current = GamingDevice(device_instance_path=_SWITCH_HID)
    stale = HidHideStatus(
        installed=True,
        cloak_on=True,
        app_whitelisted=True,
        hidden_nintendo=True,
        hidden_devices=(r"HID\VID_057E&PID_2009&MI_00\OLD&0&0000",),
    )
    same = HidHideStatus(
        installed=True,
        cloak_on=True,
        app_whitelisted=True,
        hidden_nintendo=True,
        hidden_devices=(_SWITCH_HID,),
    )

    # Then: 古い接続の entry だけでは「hide済み」にしない。
    assert switch_hidden_in_status(stale, (current,)) is False
    assert switch_hidden_in_status(same, (current,)) is True
    # コントローラが見つからないときは従来どおり hidden_nintendo に従う。
    assert switch_hidden_in_status(stale, ()) is True


def test_status_parse_records_hidden_paths() -> None:
    from core.hidhide import parse_cli_output

    text = "\n".join(
        [
            "--cloak-on",
            f'--dev-hide "{_SWITCH_HID}"',
            f'--dev-hide "{_SONY_HID}"',
        ]
    )

    status = parse_cli_output(text, ())

    assert status.hidden_devices == (_SWITCH_HID, _SONY_HID)
    assert status.hidden_nintendo is True


def test_dialog_reflects_detected_status(
    tk_root: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from core.hidhide import HidHideStatus
    from core.hidhide_setup import CliRun
    from ui import hidhide_setup

    status = HidHideStatus(
        installed=True,
        cli_path=__file__,
        cloak_on=True,
        app_whitelisted=True,
        hidden_nintendo=True,
        hidden_devices=(_SWITCH_HID,),
    )
    monkeypatch.setattr(hidhide_setup, "detect_hidhide", lambda **_kw: status)
    monkeypatch.setattr(
        hidhide_setup, "read_game_devices", lambda _path: ((), CliRun())
    )

    window = hidhide_setup.HidHideSetupDialog(tk_root)
    try:
        from core.hidhide_setup import GamingDevice

        window._update_status(
            status, (GamingDevice(device_instance_path=_SWITCH_HID),), ""
        )
        assert window._values["install"].cget("text") == "済み"
        assert window._values["app"].cget("text") == "許可済み"
        assert window._values["cloak"].cget("text") == "ON"
        assert window._values["device"].cget("text") == "hide済み"
        assert str(window.apply_button.cget("state")) == "normal"

        # Given: hide リストには古い接続の entry だけがある。
        stale = HidHideStatus(
            installed=True,
            cli_path=__file__,
            cloak_on=True,
            app_whitelisted=True,
            hidden_nintendo=True,
            hidden_devices=(r"HID\VID_057E&PID_2009&MI_00\OLD&0&0000",),
        )
        window._update_status(
            stale, (GamingDevice(device_instance_path=_SWITCH_HID),), ""
        )
        # Then: 接続中の実機が hide 済みに見えない。
        assert window._values["device"].cget("text") == "未hide"
        assert window._markers["device"].cget("text") == "!"

        # Given: 未導入。Then: 適用できない。
        window._update_status(HidHideStatus(), (), "")
        assert str(window.apply_button.cget("state")) == "disabled"
        assert window._values["app"].cget("text") == "—"
    finally:
        window.destroy()


def test_dialog_notifies_the_app_after_a_successful_setup(tk_root: Any) -> None:
    from ui import hidhide_setup

    called: list[bool] = []

    def on_applied() -> bool:
        called.append(True)
        return True

    window = hidhide_setup.HidHideSetupDialog(tk_root, on_applied=on_applied)
    try:
        assert window._notify_applied() is True
        assert called == [True]
    finally:
        window.destroy()


def test_cli_stderr_is_a_failure_even_with_exit_code_zero() -> None:
    """HidHideCLI は検証エラーを stderr に出して 0 で終わることがある。"""
    from core.hidhide_setup import run_cli

    run = run_cli(
        __file__,
        ["--app-reg", r"C:\app\python.exe"],
        runner=lambda _cli, _args: (
            0,
            "",
            "The file name should be located on a storage volume "
            "(volume is case sensitive).\n",
        ),
    )

    assert run.ok is False
    assert "storage volume" in run.error


def test_executable_candidates_keep_the_path_casing() -> None:
    """--app-reg はドライブ文字の大小を厳密に見る（normcase を渡さない）。"""
    import sys

    from core.hidhide import executable_candidates

    original = sys.executable
    original_base = getattr(sys, "_base_executable", None)
    try:
        sys.executable = r"C:\Apps\Python312\python.exe"  # type: ignore[misc]
        sys._base_executable = r"C:\Apps\Python312\python.exe"  # type: ignore[attr-defined]
        assert executable_candidates() == (r"C:\Apps\Python312\python.exe",)
    finally:
        sys.executable = original  # type: ignore[misc]
        if original_base is None:
            del sys._base_executable  # type: ignore[attr-defined]
        else:
            sys._base_executable = original_base  # type: ignore[attr-defined]
