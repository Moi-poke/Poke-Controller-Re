#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""hidhide.py - HidHide の導入・設定状態を調べる（Tkなし）。

Switch Pro コントローラの HID ジャイロとパッド入力を同じアプリで
安定して使うには、Steam / SDL に同じ HID を触らせない必要がある。
HidHide はそのためのカーネルフィルタだが、導入・cloak・アプリ許可・
対象デバイスの hide が揃っていないと意味がない。ここでは CLI の
読み取りコマンドだけを短時間で実行し、状態を返す。

自動インストールはしない。カーネルドライバの導入は管理者権限と
再起動を伴うため、利用者が公式インストーラで行う前提にする。
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass

_HIDHIDE_CLI_CANDIDATES = (
    r"%ProgramFiles%\Nefarius Software Solutions\HidHide\HidHideCLI.exe",
    r"%ProgramFiles%\Nefarius Software Solutions\HidHide\x64\HidHideCLI.exe",
)
_CACHE_TTL_S = 5.0
_cached: tuple[float, "HidHideStatus"] | None = None


@dataclass(frozen=True)
class HidHideStatus:
    """HidHide の現在の設定。ready が True のときだけ HID を使ってよい。"""

    installed: bool = False
    cli_path: str = ""
    cloak_on: bool = False
    app_whitelisted: bool = False
    hidden_nintendo: bool = False
    hidden_devices: tuple[str, ...] = ()
    error: str = ""

    @property
    def ready(self) -> bool:
        return (
            self.installed
            and self.cloak_on
            and self.app_whitelisted
            and self.hidden_nintendo
        )

    def summary(self) -> str:
        """利用者向けの短い状態説明。"""
        if not self.installed:
            return "HidHideがインストールされていません"
        if not self.cloak_on:
            return "HidHideのcloakがOFFです"
        if not self.hidden_nintendo:
            return "SwitchコントローラがHidHideのhide listにありません"
        if not self.app_whitelisted:
            return "Poke-ControllerがHidHideの許可アプリにありません"
        return "HidHide準備完了"


def _decode_output(data: bytes | str | None) -> str:
    """CLI の出力を読む。UTF-8 で読めなければ cp932、最後は置換。

    HidHideCLI は日本語環境で cp932 の行（デバイス名・エラー）を混ぜる
    ことがある。厳密な UTF-8 復号はサブプロセスの読み取りスレッドで
    UnicodeDecodeError を投げ、アプリの不具合報告ダイアログになる。
    """
    if data is None:
        return ""
    if isinstance(data, str):
        return data
    for encoding in ("utf-8", "cp932"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "replace")


def _expand(path: str) -> str:
    return os.path.expandvars(os.path.expanduser(path))


def find_cli() -> str:
    """HidHideCLI.exe の場所。見つからなければ空文字。"""
    for candidate in _HIDHIDE_CLI_CANDIDATES:
        path = _expand(candidate)
        if os.path.isfile(path):
            return path
    return ""


def _norm(path: str) -> str:
    return os.path.normcase(os.path.normpath(_expand(str(path))))


def executable_candidates() -> tuple[str, ...]:
    """HidHide の許可リストへ登録する実行ファイル候補。

    venv の python.exe は uv の base python と同一実体（ハードリンク）
    のことがあり、HidHide は実体で照合する。sys.executable だけでなく
    sys._base_executable も見る。

    返す表記は元のまま（ドライブ文字の大小を変えない）。HidHideCLI の
    ``--app-reg`` は「ボリューム名の大文字小文字」を厳密に見るため、
    照合用に normcase したパスをそのまま渡すと登録に失敗する。
    比較は parse_cli_output 側で _norm して行う。
    """
    values: dict[str, str] = {}
    for value in (
        getattr(sys, "executable", ""),
        getattr(sys, "_base_executable", ""),
    ):
        if value:
            values.setdefault(_norm(value), _expand(value))
    return tuple(values.values())


def parse_cli_output(
    output: str,
    executables: tuple[str, ...] | None = None,
    *,
    installed: bool = True,
    cli_path: str = "",
    error: str = "",
) -> HidHideStatus:
    """CLI の読み取り出力（--cloak-state --dev-list --app-list）を解析する。"""
    cloak_on = False
    hidden_nintendo = False
    hidden_devices: list[str] = []
    app_paths: set[str] = set()
    for raw in output.splitlines():
        line = raw.strip()
        low = line.lower()
        if low == "--cloak-on":
            cloak_on = True
        elif low.startswith("--app-reg "):
            value = line[len("--app-reg ") :].strip().strip('"')
            if value:
                app_paths.add(_norm(value))
        elif low.startswith("--dev-hide "):
            value = line[len("--dev-hide ") :].strip().strip('"')
            if value:
                hidden_devices.append(value)
            # Switch Pro / 互換機の HID、または USB 親。
            if "vid_057e" in value.lower() or "vid_2009" in value.lower():
                hidden_nintendo = True
    wanted = executable_candidates() if executables is None else executables
    app_whitelisted = any(_norm(p) in app_paths for p in wanted)
    return HidHideStatus(
        installed=installed,
        cli_path=cli_path,
        cloak_on=cloak_on,
        app_whitelisted=app_whitelisted,
        hidden_nintendo=hidden_nintendo,
        hidden_devices=tuple(dict.fromkeys(hidden_devices)),
        error=error,
    )


def detect_hidhide(
    cli_path: str | None = None,
    *,
    runner: Callable[[str], tuple[int, str, str]] | None = None,
    force: bool = False,
) -> HidHideStatus:
    """HidHide の状態を読む。結果は短時間キャッシュする。

    runner はテスト用。`(returncode, stdout, stderr)` を返す callable。
    CLI が無い・タイムアウト・異常終了でも例外を投げない。
    """
    global _cached
    now = time.monotonic()
    if not force and _cached is not None and now - _cached[0] <= _CACHE_TTL_S:
        return _cached[1]
    path = cli_path if cli_path is not None else find_cli()
    if not path or not os.path.isfile(path):
        status = HidHideStatus(installed=False)
        _cached = (now, status)
        return status
    try:
        if callable(runner):
            code, out, err = runner(path)
        else:
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            # text=True で読むと、CLI が cp932（日本語の Windows）で書いた
            # 行を UTF-8 で復号して _readerthread が落ちる。バイトで受けて
            # 自前で復号する（失敗しても置換で読む）。
            proc = subprocess.run(
                [path, "--cloak-state", "--dev-list", "--app-list"],
                capture_output=True,
                timeout=2.0,
                creationflags=flags,
                check=False,
            )
            code = proc.returncode
            out = _decode_output(proc.stdout)
            err = _decode_output(proc.stderr)
        error = "" if int(code) == 0 else str(err or f"exit {code}").strip()
        status = parse_cli_output(
            str(out or ""),
            installed=True,
            cli_path=path,
            error=error[:300],
        )
    except Exception as e:
        # 一時的な失敗（CLIの取り合い・タイムアウト）を覚えない。覚えると
        # 5秒間 HID が無いものとして扱われ、入力源の一覧から HID が消えた
        # ままになる（実際に踏んだ）。
        return HidHideStatus(
            installed=True,
            cli_path=path,
            error=f"{type(e).__name__}: {e}"[:300],
        )
    if status.error:
        _cached = None
    else:
        _cached = (now, status)
    return status


def hidhide_ready(*, force: bool = False) -> bool:
    """HID 直結を使ってよい状態か。"""
    return detect_hidhide(force=force).ready


def clear_cache() -> None:
    """テスト・手動再検出用。"""
    global _cached
    _cached = None
