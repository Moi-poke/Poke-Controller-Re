#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""サムネイル生成と choice グリッドの単体テスト。"""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
import pytest
from pokecon.domain import dialog_gallery


def _write(path: Path, image: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    ok, encoded = cv2.imencode(path.suffix or ".png", image)
    assert ok
    path.write_bytes(encoded.tobytes())


def test_thumbnail_fits_and_keeps_aspect(tmp_path: Path) -> None:
    image = np.zeros((100, 200, 3), dtype=np.uint8)
    image[:, :] = (0, 255, 0)  # BGR
    path = tmp_path / "wide.png"
    _write(path, image)
    png = dialog_gallery.thumbnail_png(path, size=40)
    assert png is not None
    decoded = cv2.imdecode(np.frombuffer(png, dtype=np.uint8), cv2.IMREAD_UNCHANGED)
    assert decoded.shape[:2] == (40, 40)
    # 横長なので上下に背景の余白が入る。
    assert tuple(decoded[0, 20]) == dialog_gallery.DEFAULT_BACKGROUND[::-1]


def test_thumbnail_composites_alpha(tmp_path: Path) -> None:
    image = np.zeros((20, 20, 4), dtype=np.uint8)
    image[:, :] = (0, 0, 255, 128)  # 半透明の赤
    path = tmp_path / "半透明.png"
    _write(path, image)
    png = dialog_gallery.thumbnail_png(path, size=20)
    assert png is not None
    decoded = cv2.imdecode(np.frombuffer(png, dtype=np.uint8), cv2.IMREAD_UNCHANGED)
    pixel = decoded[10, 10]
    # 赤と背景の合成。赤成分が背景より強く、青成分は背景に寄る。
    assert int(pixel[2]) > int(pixel[0])


def test_thumbnail_returns_none_for_missing(tmp_path: Path) -> None:
    assert dialog_gallery.thumbnail_png(tmp_path / "nope.png") is None
    bad = tmp_path / "bad.png"
    bad.write_bytes(b"not an image")
    assert dialog_gallery.thumbnail_png(bad) is None


def test_choice_grid_selection_and_filter() -> None:
    import tkinter as tk

    pytest.importorskip("tkinter")
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk unavailable: {exc}")
    root.withdraw()
    try:
        from Commands.CommandDialog import _ChoiceGrid

        top = tk.Toplevel(root)
        grid = _ChoiceGrid(
            top,
            [("a", "ポーション", ""), ("b", "ボール", "")],
            ["a"],
            multi=True,
            thumb=24,
        )
        grid.frame.pack()
        top.update_idletasks()
        assert grid.value() == ["a"]
        assert grid.valid() is True

        grid._toggle("b")
        top.update_idletasks()
        assert grid.value() == ["a", "b"]

        grid._query.set("ポー")
        grid._apply_filter()
        top.update_idletasks()
        assert grid._tiles["a"].winfo_manager() == "grid"
        assert grid._tiles["b"].winfo_manager() == ""
        # 検索中も選択は保持される。
        assert grid.value() == ["a", "b"]

        grid._query.set("")
        grid._apply_filter()
        grid._clear_all()
        top.update_idletasks()
        assert grid.value() == []
        assert grid.valid() is False
        grid._select_all()
        assert grid.value() == ["a", "b"]
        top.destroy()
    finally:
        root.destroy()


def test_choice_grid_single_keeps_one() -> None:
    import tkinter as tk

    pytest.importorskip("tkinter")
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk unavailable: {exc}")
    root.withdraw()
    try:
        from Commands.CommandDialog import _ChoiceGrid

        top = tk.Toplevel(root)
        grid = _ChoiceGrid(
            top,
            [("a", "A", ""), ("b", "B", "")],
            "b",
            multi=False,
            thumb=24,
        )
        grid.frame.pack()
        top.update_idletasks()
        assert grid.value() == "b"
        grid._toggle("a")
        assert grid.value() == "a"
        assert grid.valid() is True
        top.destroy()
    finally:
        root.destroy()


def test_int_limit_value_handles_unlimited() -> None:
    """「上限なし」チェック時は None、数値時は範囲を検査すること。"""
    import tkinter as tk

    pytest.importorskip("tkinter")
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk unavailable: {exc}")
    root.withdraw()
    try:
        from Commands.CommandDialog import _IntLimitValue

        check = tk.BooleanVar(value=False)
        entry = tk.StringVar(value="5")
        value = _IntLimitValue(check, entry, 1, 10, True)
        assert value.valid() is True
        assert value.get() == "5"
        entry.set("0")
        assert value.valid() is False
        entry.set("")
        assert value.valid() is False
        check.set(True)
        assert value.valid() is True
        assert value.get() is None
    finally:
        root.destroy()


def test_dialog_with_int_limit_can_be_built() -> None:
    """int_limit を含むダイアログが Tk のオプション不正で落ちないこと。"""
    import tkinter as tk

    pytest.importorskip("tkinter")
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk unavailable: {exc}")
    root.withdraw()
    try:
        from Commands.CommandDialog import PokeConDialogue

        top = tk.Toplevel(root)
        root.after(100, top.destroy)
        dialogue = PokeConDialogue(
            top,
            "test",
            [
                [
                    "int_limit",
                    "上限周回",
                    "10",
                    {"min": 1, "max": 100, "allow_unlimited": True, "unlimited": False},
                ]
            ],
            mode=1,
        )
        assert dialogue.ret_value(list) is None
    finally:
        root.destroy()
