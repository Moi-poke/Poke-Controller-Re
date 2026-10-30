#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""hidhide_setup.py - HidHide の設定を書き換える（Tkなし）。

HidHideCLI.exe は管理者権限を必要としない。変更は 1 回の呼び出しに
まとめて渡し、プロセスが正常終了した時点でまとめて書き込まれる
（失敗すると後続のコマンドは適用されない）。ここでは引数の組み立て・
実行・結果の解釈だけを持ち、状態検出は core.hidhide、画面は
ui.hidhide_setup が持つ。

自動インストールはしない。ドライバの導入は利用者が公式インストーラで
行う前提（このモジュールはインストール先の案内まで）。
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from core.hidhide import HidHideStatus, clear_cache, executable_candidates
from loguru import logger

#: HidHide の公式配布。GUI の「インストール」はここを開くだけ。
INSTALL_URL = "https://github.com/nefarius/HidHide/releases/latest"

#: CLI が失敗する主な原因。設定アプリがドライバを掴んでいると変更できない。
CLIENT_HINT = (
    "HidHide の設定アプリを開いていると変更できません。閉じてから再試行してください。"
)

_TIMEOUT_S = 10.0

#: Switch Pro コントローラ（互換機を含む）の判定語。
#: 0x057E は任天堂、0x2009 は一部の互換機が使う VID。
_NINTENDO_MARKERS = ("vid_057e", "vid_2009", "nintendo")

#: runner はテスト用。(cli_path, args) を受けて (code, stdout, stderr) を返す。
Runner = Callable[[str, list[str]], tuple[int, str, str]]


@dataclass(frozen=True)
class GamingDevice:
    """``HidHideCLI.exe --dev-gaming`` が返すデバイス 1 台。"""

    device_instance_path: str
    base_container: str = ""
    xusb: str = ""
    friendly_name: str = ""
    vendor: str = ""
    product: str = ""
    present: bool = True

    def hide_paths(self) -> tuple[str, ...]:
        """hide すべき instance path。

        XInput の回り込みを防ぐため、HID 子だけでなく USB 親と
        XUSB ノード（ある場合）も含める。空・重複は落とす。
        """
        values = (self.device_instance_path, self.base_container, self.xusb)
        return tuple(dict.fromkeys(v for v in values if v))


@dataclass(frozen=True)
class CliRun:
    """CLI 1 回分の実行結果。``ok`` が True のときだけ設定が書かれている。"""

    code: int = 0
    stdout: str = ""
    stderr: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.code == 0 and not self.error


def _as_text(value: Any) -> str:
    """JSON の値を表示用の文字列へ。None / bool も落とさない。"""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value).strip()


def _as_present(value: Any) -> bool:
    """``present`` を bool へ。無い場合は「接続中」とみなす。"""
    if isinstance(value, bool):
        return value
    return _as_text(value).casefold() not in ("false", "0", "no")


def _device_from(obj: dict[str, Any], container_name: str) -> GamingDevice | None:
    """JSON の 1 要素を GamingDevice へ。path が無い要素は捨てる。"""
    path = _as_text(obj.get("deviceInstancePath"))
    if not path:
        return None
    name = _as_text(obj.get("friendlyName")) or container_name
    return GamingDevice(
        device_instance_path=path,
        base_container=_as_text(obj.get("baseContainerDeviceInstancePath")),
        xusb=_as_text(obj.get("xusbDeviceInstancePath")),
        friendly_name=name,
        vendor=_as_text(obj.get("vendor")),
        product=_as_text(obj.get("product")),
        present=_as_present(obj.get("present", True)),
    )


def parse_inventory(text: str) -> tuple[GamingDevice, ...]:
    """``--dev-gaming`` / ``--dev-all`` の JSON を読む。

    先頭に案内文が混ざっていても読めるよう、最初の ``[`` から最後の
    ``]`` までを JSON として解釈する。壊れていれば空を返す（落とさない）。
    """
    start = text.find("[")
    end = text.rfind("]")
    if start < 0 or end <= start:
        return ()
    try:
        data: Any = json.loads(text[start : end + 1])
    except (ValueError, TypeError):
        return ()
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        return ()

    devices: list[GamingDevice] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        container_name = _as_text(entry.get("friendlyName")) or _as_text(
            entry.get("name")
        )
        children = entry.get("devices")
        if isinstance(children, list):
            for child in children:
                if isinstance(child, dict):
                    device = _device_from(child, container_name)
                    if device is not None:
                        devices.append(device)
            continue
        device = _device_from(entry, container_name)
        if device is not None:
            devices.append(device)
    return tuple(devices)


def is_switch_device(device: GamingDevice) -> bool:
    """Switch Pro / 互換機か。VID か名前のどちらかで判定する。"""
    text = " ".join(
        (
            device.device_instance_path,
            device.base_container,
            device.xusb,
            device.friendly_name,
            device.vendor,
            device.product,
        )
    ).casefold()
    return any(marker in text for marker in _NINTENDO_MARKERS)


def is_switch_path(path: str) -> bool:
    """instance path 単体が Switch 系か（解除時に他デバイスへ触れないため）。"""
    low = str(path).casefold()
    return "vid_057e" in low or "vid_2009" in low


def enumerate_present_hid_paths() -> tuple[str, ...]:
    """接続中の Switch 系 HID の instance path を Windows から列挙する。

    HidHideCLI 1.5.230 の ``--dev-gaming`` は、デバイス名に非 ASCII 文字が
    含まれると wide stream の変換で JSON が途中で止まる（日本語環境で
    実測）。JSON が読めないときでも hide 対象を決められるよう、CFGMGR32
    のデバイス一覧を直接読む。失敗・非 Windows は空。
    """
    if os.name != "nt":
        return ()
    try:
        import ctypes

        cfgmgr32 = ctypes.WinDLL("cfgmgr32")
        configret = ctypes.c_ulong
        # CM_GETIDLIST_FILTER_ENUMERATOR | CM_GETIDLIST_FILTER_PRESENT
        flags = 0x00000001 | 0x00000100
        size = ctypes.c_ulong()

        list_size = cfgmgr32.CM_Get_Device_ID_List_SizeW
        list_size.argtypes = [
            ctypes.POINTER(ctypes.c_ulong),
            ctypes.c_wchar_p,
            ctypes.c_ulong,
        ]
        list_size.restype = configret
        list_ids = cfgmgr32.CM_Get_Device_ID_ListW
        list_ids.argtypes = [
            ctypes.c_wchar_p,
            ctypes.POINTER(ctypes.c_wchar),
            ctypes.c_ulong,
            ctypes.c_ulong,
        ]
        list_ids.restype = configret

        if list_size(ctypes.byref(size), "HID", flags) != 0 or size.value == 0:
            return ()
        buffer = ctypes.create_unicode_buffer(size.value)
        if list_ids("HID", buffer, size.value, flags) != 0:
            return ()
        raw = "".join(buffer[:])
        paths = [part for part in raw.split("\0") if part]
    except Exception as e:
        logger.warning(f"HIDデバイスを列挙できませんでした: {e}")
        return ()
    return tuple(dict.fromkeys(path for path in paths if is_switch_path(path)))


def switch_hidden_in_status(
    status: HidHideStatus, devices: Iterable[GamingDevice]
) -> bool:
    """接続中の Switch HID 子が hide リストに入っているか。

    1 台も見つからないときは、hide リストに Switch 系があるか
    （``status.hidden_nintendo``）をそのまま使う。古い接続の instance path
    だけが残っている場合を「準備完了」と誤表示しないための判定。
    """
    switch = switch_devices(devices)
    if not switch:
        return status.hidden_nintendo
    hidden = {os.path.normcase(path) for path in status.hidden_devices}
    return any(
        os.path.normcase(device.device_instance_path) in hidden for device in switch
    )


def switch_devices(
    devices: Iterable[GamingDevice],
) -> tuple[GamingDevice, ...]:
    """接続中かどうかに関係なく、Switch 系の要素だけを返す。"""
    return tuple(device for device in devices if is_switch_device(device))


def switch_hide_paths(devices: Iterable[GamingDevice]) -> tuple[str, ...]:
    """Switch 系デバイスから hide する path を重複なく集める。"""
    paths: list[str] = []
    for device in switch_devices(devices):
        paths.extend(device.hide_paths())
    return tuple(dict.fromkeys(paths))


def run_cli(
    cli_path: str,
    args: Iterable[str],
    *,
    runner: Runner | None = None,
    timeout: float = _TIMEOUT_S,
) -> CliRun:
    """HidHideCLI.exe を 1 回だけ実行する。例外は結果に畳む。"""
    command = [str(arg) for arg in args]
    if not cli_path or (not callable(runner) and not os.path.isfile(cli_path)):
        return CliRun(code=-1, error="HidHideCLI.exe が見つかりません")
    try:
        if callable(runner):
            code, out, err = runner(cli_path, command)
        else:
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            proc = subprocess.run(
                [cli_path, *command],
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                creationflags=flags,
                check=False,
            )
            code, out, err = proc.returncode, proc.stdout, proc.stderr
    except Exception as e:
        return CliRun(code=-1, error=f"{type(e).__name__}: {e}"[:300])

    code = int(code)
    out_text = str(out or "")
    err_text = str(err or "")
    # HidHideCLI は引数の検証エラーを stderr に出して **終了コード 0** で
    # 止まることがある（以降のコマンドは適用されない）。書き込みが
    # 黙って一部だけ成功したように見えるため、stderr があれば失敗とする。
    error = err_text.strip()
    if not error and code != 0:
        error = out_text.strip() or f"exit {code}"
    return CliRun(code=code, stdout=out_text, stderr=err_text, error=error[:500])


def _inventory_looks_complete(text: str) -> bool:
    """JSON が最後まで出ているか（途中で止まった出力を見分ける）。"""
    start = text.find("[")
    end = text.rfind("]")
    return start >= 0 and end > start


def read_game_devices(
    cli_path: str,
    *,
    runner: Runner | None = None,
    enumerator: Callable[[], tuple[str, ...]] | None = None,
) -> tuple[tuple[GamingDevice, ...], CliRun]:
    """``--dev-gaming`` を読み、デバイス一覧と実行結果を返す。

    CLI の JSON が途中で切れている・Switch が載っていないときは、
    OS のデバイス一覧（``enumerator``）から hide 対象を補う。runner を
    渡したテストでは、明示的に enumerator を渡さない限り実機を見ない。
    """
    run = run_cli(cli_path, ["--dev-gaming"], runner=runner)
    if not run.ok:
        return ((), run)
    devices = parse_inventory(run.stdout)
    if _inventory_looks_complete(run.stdout) and switch_devices(devices):
        return (devices, run)

    if runner is None or enumerator is not None:
        find = enumerator if enumerator is not None else enumerate_present_hid_paths
        try:
            paths = find()
        except Exception as e:
            logger.warning(f"デバイス一覧の補完に失敗しました: {e}")
            paths = ()
        known = {device.device_instance_path.casefold() for device in devices}
        extra = tuple(
            GamingDevice(path) for path in paths if path.casefold() not in known
        )
        if extra:
            logger.debug(
                f"CLIの一覧を補完しました: {[d.device_instance_path for d in extra]}"
            )
            devices = devices + extra
    return (devices, run)


def apply_recommended(
    cli_path: str,
    *,
    executables: Iterable[str] | None = None,
    hide_paths: Iterable[str] = (),
    runner: Runner | None = None,
) -> CliRun:
    """このアプリの許可・対象デバイスの hide・cloak ON を 1 回で書く。

    ``--inv-off`` も明示する（逆転モードが残っていると hide の意味が
    反転し、「何も隠れていない」ように見えるため）。
    """
    args: list[str] = []
    for exe in executable_candidates() if executables is None else executables:
        if exe:
            args.extend(("--app-reg", str(exe)))
    for path in hide_paths:
        if path:
            args.extend(("--dev-hide", str(path)))
    args.extend(("--cloak-on", "--inv-off"))
    run = run_cli(cli_path, args, runner=runner)
    clear_cache()
    return run


def set_cloak(cli_path: str, on: bool, *, runner: Runner | None = None) -> CliRun:
    """cloak（非表示の入/切）だけを変える。リストは保持される。"""
    run = run_cli(cli_path, ["--cloak-on" if on else "--cloak-off"], runner=runner)
    clear_cache()
    return run


def release_setup(
    cli_path: str,
    *,
    hide_paths: Iterable[str] = (),
    executables: Iterable[str] | None = None,
    runner: Runner | None = None,
) -> CliRun:
    """cloak を止め、Switch の hide とこのアプリの許可を解除する。

    他のアプリが hide したデバイスや許可は触らない（Switch 系だけを
    解除する）。許可の解除は HidHide の許可リストからこの実行ファイルを
    外すだけで、他が同じ Python を使っている場合も外れる。
    """
    args: list[str] = ["--cloak-off"]
    for path in hide_paths:
        if path and is_switch_path(path):
            args.extend(("--dev-unhide", str(path)))
    for exe in executable_candidates() if executables is None else executables:
        if exe:
            args.extend(("--app-unreg", str(exe)))
    run = run_cli(cli_path, args, runner=runner)
    clear_cache()
    return run


def failure_message(run: CliRun) -> str:
    """失敗を利用者向けの 2 行へ。成功時は空文字。"""
    if run.ok:
        return ""
    return f"{run.error or 'HidHideCLI.exe が失敗しました'}\n{CLIENT_HINT}"
