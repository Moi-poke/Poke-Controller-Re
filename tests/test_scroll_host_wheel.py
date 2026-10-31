"""スクロール欄のマウスホイール契約。実 Tk。

設定タブ・ログなどは ScrollHost の中にあり、縦に長いとスクロールが必要になる。
ホイールはその欄の上でだけ効かせ、Text / Listbox など自前でスクロールする
部品の邪魔をしない。
"""

from __future__ import annotations

import tkinter as tk

import pytest
from ui.scroll_host import ScrollHost


@pytest.fixture
def host(tk_root: tk.Tk) -> tuple[ScrollHost, tk.Toplevel]:
    window = tk.Toplevel(tk_root)
    window.geometry("260x120")
    scroll = ScrollHost(window)
    scroll.pack(fill="both", expand=True)
    tall = tk.Frame(scroll.inner, height=900)
    tall.pack(fill="both", expand=True)
    window.update()
    return scroll, window


def test_wheel_over_the_content_scrolls_the_host(
    host: tuple[ScrollHost, tk.Toplevel],
) -> None:
    scroll, window = host
    child = tk.Label(scroll.inner)
    child.place(x=10, y=10, width=40, height=20)
    window.update()
    before = scroll.canvas.yview()[0]

    child.event_generate("<MouseWheel>", delta=-120, x=5, y=5)
    window.update()

    assert scroll.canvas.yview()[0] > before


def test_wheel_over_a_text_widget_keeps_text_scrolling(
    host: tuple[ScrollHost, tk.Toplevel],
) -> None:
    scroll, window = host
    text = tk.Text(scroll.inner, height=4)
    text.place(x=10, y=10, width=120, height=60)
    window.update()
    before = scroll.canvas.yview()[0]

    text.event_generate("<MouseWheel>", delta=-120, x=5, y=5)
    window.update()

    # Text 自身のスクロールに任せ、外側の欄は動かさない。
    assert scroll.canvas.yview()[0] == before
