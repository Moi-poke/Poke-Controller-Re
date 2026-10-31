#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""collapsible.py - 詳細を 1 段だけ開閉する見出しと、枠線なしの見出し付きまとまり。

MS の SettingsExpander / NN/g の progressive disclosure と同じ考え方で、
日常使う項目を上に出したまま、詳細（診断・保守・調整値）を要求時だけ
見せる。見出しは枠線のない平たいボタン（Toolbutton）にし、箱を重ねない。
箱の入れ子と大きな余白は、どの項目が同じまとまりか分かりにくくするため。

ウィジェットは畳んでも破棄しない。開閉のたびに作り直すと、打ちかけの
入力・選択・ログが消えるため。開閉状態はこの部品が持ち、新しい永続設定は
足さない（T01: セッション中だけ保持する）。
"""

from __future__ import annotations

import tkinter.ttk as ttk
from collections.abc import Callable
from typing import Any

# 閉じた中身へ Tab が入らないようにする対象。
_FOCUS_CLASSES = frozenset(
    {
        "TButton",
        "TCheckbutton",
        "TRadiobutton",
        "TEntry",
        "TCombobox",
        "TSpinbox",
        "Entry",
        "Text",
        "Spinbox",
        "Button",
        "Checkbutton",
        "Radiobutton",
    }
)

# 閉 / 開 の印。文字だけでも状態が分かるようにする（色に頼らない）。
_MARK_CLOSED = "▶"
_MARK_OPEN = "▼"


class SectionFrame(ttk.Frame):
    """枠線なしの見出し（title）と中身（body）を縦に並べる。

    見出しを省く（title="")こともできる。枠線を引かず、余白と見出しだけで
    まとまりを示す（SmartHR の視覚的グルーピング: 余白 → 矩形 → 罫線の順）。
    """

    def __init__(self, master: Any, title: str = "", *, padding: Any = 0) -> None:
        super().__init__(master, padding=padding)
        self.title = str(title)
        self.header: Any = None
        if self.title:
            self.header = ttk.Label(self, text=self.title)
            self.header.pack(anchor="w")
        self.body = ttk.Frame(self)
        self.body.pack(fill="both", expand=True)


class CollapsibleSection(ttk.Frame):
    """見出しを押すと中身（body）を出す／畳む、枠線なしのまとまり。

    見出しは Toolbutton（平たいボタン）。Tab で到達でき、Enter / Space で
    開閉できる。閉じている間は中身を pack しない（高さを取らない）ため、
    縦に長くならない。中身のウィジェットは破棄しないので値は残る。
    """

    def __init__(
        self,
        master: Any,
        title: str,
        *,
        open: bool = False,
        on_toggle: Callable[[], None] | None = None,
        padding: Any = 0,
        expand: bool = False,
    ) -> None:
        super().__init__(master, padding=padding)
        self.title = str(title)
        self._is_open = bool(open)
        self._on_toggle = on_toggle
        self._expand = bool(expand)
        # 閉じている間に中身へ Tab が入らないよう、元の takefocus を覚えて戻す。
        self._focus_saved: dict[int, str] = {}
        self.header = ttk.Button(
            self, style="Toolbutton", command=self.toggle, takefocus=True
        )
        self.header.pack(fill="x")
        self.body = ttk.Frame(self)
        self._apply()
        # 中身はこの後に作られる（外側で body へ足す）。部品が揃った時点で
        # 閉じている間の Tab 順を整える。
        try:
            self.after_idle(self._apply_focus)
        except Exception:
            pass

    # -- 状態 ---------------------------------------------------------

    @property
    def open(self) -> bool:
        """いま中身を出しているか。"""
        return self._is_open

    def set_open(self, open: bool) -> None:
        """開閉を決め直す。変わらないときは何もしない（再描画を積まない）。"""
        wanted = bool(open)
        if wanted == self._is_open:
            return
        self._is_open = wanted
        self._apply()
        self._notify()

    def toggle(self) -> None:
        self.set_open(not self._is_open)

    def _apply(self) -> None:
        mark = _MARK_OPEN if self._is_open else _MARK_CLOSED
        try:
            self.header.configure(text=f"{mark} {self.title}")
        except Exception:
            pass
        try:
            if self._is_open:
                self.body.pack(
                    fill="both" if self._expand else "x", expand=self._expand
                )
            else:
                self.body.pack_forget()
        except Exception:
            pass
        self._apply_focus()

    def _walk(self, widget: Any) -> list[Any]:
        found: list[Any] = []
        try:
            children = widget.winfo_children()
        except Exception:
            return found
        for child in children:
            found.append(child)
            found.extend(self._walk(child))
        return found

    def _apply_focus(self) -> None:
        """閉じている間は中身の操作部品を Tab 順から外す（隠れた入口に届かせない）。"""
        for widget in self._walk(self.body):
            try:
                if widget.winfo_class() not in _FOCUS_CLASSES:
                    continue
            except Exception:
                continue
            key = id(widget)
            if not self._is_open:
                if key in self._focus_saved:
                    continue
                try:
                    self._focus_saved[key] = str(widget.cget("takefocus"))
                    widget.configure(takefocus="0")
                except Exception:
                    self._focus_saved.pop(key, None)
            else:
                saved = self._focus_saved.pop(key, None)
                if saved is not None:
                    try:
                        widget.configure(takefocus=saved)
                    except Exception:
                        pass

    def _notify(self) -> None:
        """開閉で要求サイズが変わったことを親へ知らせる（配置の測り直し用）。"""
        callback = self._on_toggle
        if callback is None:
            return
        try:
            callback()
        except Exception:
            pass
