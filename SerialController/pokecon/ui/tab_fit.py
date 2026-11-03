#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tab_fit.py - 設定タブの中身を「実際に見えている幅」へ合わせる。

ttk.Notebook の内側は全タブ共通の幅（一番広いタブの要求幅）で、スクロール欄で
見えている幅より広いことがある。中身をそのまま fill で置くと、右側のボタンや
表の列が見えている範囲の外へ出て、横スクロールしないと触れなくなる。

中身の枠を place し、スクロール欄の Canvas で見えている幅に合わせる。
タブの要求高さも中身に合わせて返す（SelectedTabHeight が Notebook の高さに
使う）。幅の計算にタブ自身の幅を使わないので、幅→折り返し→要求幅の循環が
起きない。
"""

from __future__ import annotations

import tkinter as tk
from typing import Any

#: タブの最低の高さ（px）。中身が短くても、数行しか見えない高さまで縮めない。
MIN_TAB_HEIGHT = 280


class TabFit:
    """タブの中身を可視幅・可視高さへ合わせる。"""

    def __init__(
        self,
        tab: Any,
        content: Any,
        viewport: Any = None,
        *,
        min_height: int = MIN_TAB_HEIGHT,
        expand_height: bool = False,
        padding: int = 8,
    ) -> None:
        self.tab = tab
        self.content = content
        self.viewport = viewport
        self.min_height = int(min_height)
        self.expand_height = bool(expand_height)
        self.padding = int(padding)
        self._last: tuple[int, int] | None = None
        self._fitting = False
        content.place(x=0, y=0)
        content.bind("<Configure>", self._on_content_configure, add="+")
        tab.bind("<Configure>", self.fit, add="+")
        tab.bind("<Map>", self.fit, add="+")
        canvas = getattr(viewport, "canvas", None)
        if canvas is not None:
            canvas.bind("<Configure>", self.fit, add="+")
        tab.after_idle(self.fit)

    def _on_content_configure(self, _event: Any = None) -> None:
        # 中身の要求の高さが変わったら、タブの高さを測り直す。
        # 幅は可視幅から決まるので、中身の Configure では変えない。
        self.fit()

    def available_width(self) -> int:
        canvas = getattr(self.viewport, "canvas", None)
        if canvas is not None:
            try:
                width = int(canvas.winfo_width())
                if width > 1:
                    try:
                        mapped = bool(self.tab.winfo_ismapped())
                    except tk.TclError:
                        mapped = False
                    if not mapped:
                        # 非表示のタブは rootx が当てにならない。見えている幅に
                        # 合わせておかないと、その要求幅が Notebook 全体を
                        # 広げ、選択中のタブに横スクロールが出る。
                        return max(160, width - self.padding)
                    right = canvas.winfo_rootx() + width
                    avail = right - self.tab.winfo_rootx() - self.padding
                    return max(160, min(width, avail))
            except (tk.TclError, AttributeError, TypeError):
                pass
        try:
            width = int(self.tab.winfo_width())
            if width > 1:
                return max(160, width - 4)
        except (tk.TclError, AttributeError):
            pass
        try:
            return max(160, int(self.tab.winfo_toplevel().winfo_width()) - 40)
        except (tk.TclError, AttributeError):
            return 320

    def available_height(self) -> int:
        canvas = getattr(self.viewport, "canvas", None)
        if canvas is not None:
            try:
                height = int(canvas.winfo_height())
                if height > 1:
                    return height
            except (tk.TclError, AttributeError, TypeError):
                pass
        try:
            return max(1, int(self.tab.winfo_height()))
        except (tk.TclError, AttributeError):
            return self.min_height

    def fit(self, _event: Any = None) -> None:
        if self._fitting:
            return
        self._fitting = True
        try:
            self._fit_once()
        finally:
            self._fitting = False

    def _fit_once(self) -> None:
        try:
            if not self.tab.winfo_exists() or not self.content.winfo_exists():
                return
        except tk.TclError:
            return
        width = self.available_width()
        try:
            if int(float(self.content.place_info().get("width") or 0)) != width:
                self.content.place_configure(width=width)
        except (tk.TclError, ValueError, TypeError):
            pass
        try:
            wanted = self.content.winfo_reqheight() + self.padding
        except tk.TclError:
            return
        if self.expand_height:
            total = max(wanted, self.available_height())
            height = max(self.min_height, total - self.padding)
            try:
                if int(float(self.content.place_info().get("height") or 0)) != height:
                    self.content.place_configure(height=height)
            except (tk.TclError, ValueError, TypeError):
                pass
            total = height + self.padding
        else:
            height = max(wanted, self.min_height)
            total = height
        size = (width, total)
        if self._last == size:
            return
        self._last = size
        try:
            self.tab.configure(width=width, height=total)
        except tk.TclError:
            pass
