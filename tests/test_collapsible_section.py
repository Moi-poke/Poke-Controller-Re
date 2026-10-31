"""折りたたみ（CollapsibleSection）の契約。実 Tk。

初期は閉じ、見出しは Tab で到達でき、開閉しても入力値を捨てない。
入れ子は 1 段だけ（T01 の共通レイアウト規則）。
"""

from __future__ import annotations

import tkinter as tk
import tkinter.ttk as ttk
from collections.abc import Iterator

import pytest
from ui.collapsible import CollapsibleSection


@pytest.fixture
def section(tk_root: tk.Tk) -> Iterator[tuple[CollapsibleSection, tk.Toplevel]]:
    window = tk.Toplevel(tk_root)
    try:
        window.geometry("360x300")
        section = CollapsibleSection(window, "通信の詳細設定")
        section.pack(fill="x")
        entry = ttk.Entry(section.body)
        entry.pack(fill="x")
        entry.insert(0, "COM7")
        window.update()
        yield section, window
    finally:
        window.destroy()


def test_a_section_starts_closed_and_shows_the_closed_mark(
    section: tuple[CollapsibleSection, tk.Toplevel],
) -> None:
    widget, _window = section

    # Then: 初期は閉じ、見出しに ▶ を出す。中身は見えない（高さ 0）。
    assert widget.open is False
    assert str(widget.header.cget("text")).startswith("▶")
    assert widget.body.winfo_height() <= 2
    # 見出しはボタンなので Tab で到達でき、Enter / Space で開閉できる。
    assert str(widget.header.cget("takefocus")) not in ("", "0")


def test_opening_reveals_the_body_and_keeps_the_draft(
    section: tuple[CollapsibleSection, tk.Toplevel],
) -> None:
    widget, window = section
    entry = next(
        child for child in widget.body.winfo_children() if isinstance(child, ttk.Entry)
    )

    # When: 開く。
    widget.set_open(True)
    window.update()

    # Then: 中身が見え、見出しは ▼。入力値はそのまま。
    assert widget.open is True
    assert str(widget.header.cget("text")).startswith("▼")
    assert entry.winfo_ismapped()
    assert entry.get() == "COM7"

    # When: 閉じてから、もう一度開く。
    widget.set_open(False)
    window.update()
    assert entry.get() == "COM7"
    widget.header.invoke()  # キーボード／マウスの入口と同じ
    window.update()

    # Then: 中身も値も残る（作り直さない）。
    assert widget.open is True
    assert entry.winfo_ismapped()
    assert entry.get() == "COM7"


def test_the_toggle_callback_fires_only_when_the_state_changes(
    tk_root: tk.Tk,
) -> None:
    calls: list[int] = []
    window = tk.Toplevel(tk_root)
    try:
        widget = CollapsibleSection(window, "診断", on_toggle=lambda: calls.append(1))
        widget.pack(fill="x")
        window.update()

        widget.set_open(False)  # 既に閉じている
        assert calls == []
        widget.set_open(True)
        widget.set_open(True)
        assert calls == [1]
        widget.toggle()
        assert calls == [1, 1]
        assert widget.open is False
    finally:
        window.destroy()


def test_the_group_title_stays_readable_for_screen_readers_and_tests(
    section: tuple[CollapsibleSection, tk.Toplevel],
) -> None:
    widget, _window = section

    # Then: まとまりの名前は部品にも残る（何のまとまりか分かる）。
    assert widget.title == "通信の詳細設定"
    assert widget.title in str(widget.header.cget("text"))
