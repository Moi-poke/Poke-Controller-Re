"""テンプレート読み込みエラーの案内（パス・コピー・フォルダを開く）の検証。

利用者は「画像が読めない」としか言えないので、ダイアログは
  - パスを本文と混ぜず独立した行に出す（選択してコピーできる）
  - 開発者へ貼れる診断テキストを持つ
  - 対象フォルダを開ける
を満たす。ここでは Tk を実際に組んで、その形を確かめる。
"""

from __future__ import annotations

import tkinter as tk
import tkinter.ttk as ttk
from pathlib import Path

import pytest
from Commands.CommandDialog import _copy_to_clipboard, build_recovery_panel
from pokecon.domain.recovery import RecoveryAdvice, TemplateImageError


def _advice(path: str) -> RecoveryAdvice:
    return TemplateImageError(
        f"ファイルがありません: {path}",
        hint="画像を Template 配下に置いてください",
        path=path,
        root="TEMPLATE_ROOT",
        reason="ファイルがありません",
    ).advice()


def test_advice_keeps_the_path_out_of_the_body(tmp_path: Path) -> None:
    target = str(tmp_path / "日本語" / "a.png")
    advice = _advice(target)

    assert advice.path == target
    assert target not in advice.body()
    assert advice.open_path == str(tmp_path / "日本語")


def test_advice_builds_a_report_for_developers(tmp_path: Path) -> None:
    target = str(tmp_path / "a.png")
    advice = _advice(target)

    report = advice.copy_text
    assert target in report
    assert "TEMPLATE_ROOT" in report
    assert "cwd:" in report
    assert "python:" in report
    assert "reason: ファイルがありません" in report


def test_missing_target_leaves_no_path_or_open_button() -> None:
    advice = TemplateImageError("a.png がありません").advice()

    assert advice.path == ""
    assert advice.open_path == ""


def test_panel_shows_the_path_in_a_readonly_row(tk_root: tk.Tk) -> None:
    target = r"C:\work\Template\my-pack\a.png"
    advice = _advice(target)
    top = tk.Toplevel(tk_root)
    frame = ttk.Frame(top)
    called: list[str] = []
    try:
        panel = build_recovery_panel(
            frame,
            advice,
            on_retry=lambda: called.append("retry"),
            on_abort=lambda: called.append("abort"),
            on_copy=lambda: called.append("copy"),
            on_open=lambda: called.append("open"),
        )
        top.update_idletasks()

        entry = panel["path_entry"]
        assert entry is not None
        # 長いパスは折り返して全部見えること（Entry だと右で切れる）。
        assert entry.get("1.0", "end-1c") == target
        assert str(entry.cget("wrap")) == "char"
        assert entry.bind("<Key>") != ""
        assert panel["copy_button"].cget("text") == "エラー内容をコピー"
        assert panel["open_button"].cget("text") == "フォルダを開く"

        panel["copy_button"].invoke()
        panel["open_button"].invoke()
        assert called == ["copy", "open"]
    finally:
        top.destroy()


def test_copy_puts_the_report_on_the_clipboard(tk_root: tk.Tk) -> None:
    advice = _advice(r"C:\work\a.png")
    try:
        _copy_to_clipboard(tk_root, advice.copy_text)
        assert tk_root.clipboard_get() == advice.copy_text
    except tk.TclError as exc:  # クリップボードを使えない環境
        pytest.skip(f"clipboard unavailable: {exc}")
