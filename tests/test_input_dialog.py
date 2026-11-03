#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""実行時引数の読み口と、開始前ダイアログの組み立て。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from core import inputs
from ui import input_dialog

pytest.importorskip("tkinter")


def _touch(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"image")


@pytest.fixture()
def app_dir(tmp_path: Path) -> Path:
    root = tmp_path / "SerialController"
    for name in ("potion", "ball", "revive"):
        _touch(root / "Template" / "mypack" / "items" / f"{name}.png")
    _touch(root / "Template" / "mypack" / "items" / "icons" / "potion.png")
    return root


def _raw_spec() -> list[dict[str, Any]]:
    return [
        {
            "key": "targets",
            "kind": "choice",
            "set": "mypack/items",
            "multi": True,
            "default": "*",
            "title": "集める対象",
            "labels": {"potion": "ポーション"},
        },
        {"key": "n", "kind": "int", "min": 1, "max": 5, "default": 2, "title": "回数"},
    ]


def test_widgets_and_coerce(app_dir: Path) -> None:
    specs, errors = inputs.parse_spec(_raw_spec(), app_dir)
    assert errors == []
    widgets, meta = input_dialog._widgets(specs, str(app_dir))
    assert widgets[0][0] == "choice"
    assert widgets[1][0] == "int_limit"
    values, error = input_dialog._coerce([["potion", "ball"], "3"], meta, str(app_dir))
    assert error is None
    assert values == {
        "targets": ["ball", "potion"],
        "n": 3,
    }


def test_coerce_reports_invalid(app_dir: Path) -> None:
    specs, _ = inputs.parse_spec(_raw_spec(), app_dir)
    _, meta = input_dialog._widgets(specs, str(app_dir))
    values, error = input_dialog._coerce([["nope"], "9"], meta, str(app_dir))
    assert values is None
    assert error and "選択肢" in error


def test_option_sets_are_used_for_labels(app_dir: Path) -> None:
    specs, _ = inputs.parse_spec(_raw_spec(), app_dir)
    widgets, _ = input_dialog._widgets(specs, str(app_dir))
    entries = widgets[0][2]
    assert [entry[0] for entry in entries] == ["ball", "potion", "revive"]
    assert any(entry[1] == "ポーション" for entry in entries)
    assert all(entry[2] for entry in entries)


def test_input_value() -> None:
    from Commands.PythonCommandBase import PythonCommand

    class Cmd(PythonCommand):
        def do(self) -> None:
            pass

    cmd = Cmd()
    assert cmd.input_value("missing", 5) == 5
    assert cmd.input_value("missing", None) is None
    with pytest.raises(KeyError):
        cmd.input_value("missing")
    cmd.pokecon_input_values = {"x": [1, 2]}
    assert cmd.input_value("x") == [1, 2]


def test_int_dialog_prefills_explicit_unlimited(app_dir: Path) -> None:
    """答えとして None が来ていれば「上限なし」をチェック状態で開くこと。"""
    specs, errors = inputs.parse_spec(
        [
            {
                "key": "n",
                "kind": "int",
                "min": 1,
                "max": 100,
                "default": 10,
                "allow_unlimited": True,
            }
        ],
        app_dir,
    )
    assert errors == []
    widgets, _ = input_dialog._widgets(specs, str(app_dir), {"n": None})
    assert widgets[0][0] == "int_limit"
    assert widgets[0][3]["unlimited"] is True
