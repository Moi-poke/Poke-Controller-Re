#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`core/option_sets.py` の単体テスト。"""

from __future__ import annotations

from pathlib import Path

import pytest
from core import option_sets


def _touch(path: Path, data: bytes = b"not-a-real-image") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


@pytest.fixture()
def app_dir(tmp_path: Path) -> Path:
    root = tmp_path / "SerialController"
    _touch(root / "Template" / "mypack" / "items" / "potion.png")
    _touch(root / "Template" / "mypack" / "items" / "ball.jpg")
    _touch(root / "Template" / "mypack" / "items" / "icons" / "potion.png")
    return root


def test_normalize_set_name() -> None:
    assert option_sets.normalize_set_name("mypack/items") == "mypack/items"
    assert option_sets.normalize_set_name("mypack\\items") == "mypack/items"
    assert option_sets.normalize_set_name("./mypack/./items") == "mypack/items"
    for bad in ("", "/abs", "C:/x", "../x", "a/../../b", None, 3):
        assert option_sets.normalize_set_name(bad) is None


def test_list_items_sorted_labels_and_icons(app_dir: Path) -> None:
    items = option_sets.list_items(
        app_dir, "mypack/items", labels={"potion": "ポーション"}
    )
    assert [item.value for item in items] == ["ball", "potion"]
    potion = next(item for item in items if item.value == "potion")
    ball = next(item for item in items if item.value == "ball")
    assert potion.label == "ポーション"
    assert ball.label == "ball"
    assert potion.icon == "mypack/items/icons/potion.png"
    assert ball.icon == "mypack/items/ball.jpg"


def test_list_items_rejects_bad_names_and_duplicates(app_dir: Path) -> None:
    _touch(app_dir / "Template" / "mypack" / "items" / "bad name.png")
    with pytest.raises(option_sets.OptionSetError):
        option_sets.list_items(app_dir, "mypack/items")
    (app_dir / "Template" / "mypack" / "items" / "bad name.png").unlink()

    _touch(app_dir / "Template" / "mypack" / "items" / "potion.jpg")
    with pytest.raises(option_sets.OptionSetError):
        option_sets.list_items(app_dir, "mypack/items")


def test_option_template_returns_extension(app_dir: Path) -> None:
    assert (
        option_sets.option_template(app_dir, "mypack/items", "potion")
        == "mypack/items/potion.png"
    )
    assert (
        option_sets.option_template(app_dir, "mypack/items", "ball")
        == "mypack/items/ball.jpg"
    )
    with pytest.raises(option_sets.OptionSetError):
        option_sets.option_template(app_dir, "mypack/items", "unknown")


def test_missing_set_and_empty_set(app_dir: Path) -> None:
    with pytest.raises(option_sets.OptionSetError):
        option_sets.list_items(app_dir, "mypack/missing")
    empty = app_dir / "Template" / "mypack" / "empty"
    empty.mkdir(parents=True)
    with pytest.raises(option_sets.OptionSetError):
        option_sets.list_items(app_dir, "mypack/empty")


def test_icons_excluded_from_items(app_dir: Path) -> None:
    values = [item.value for item in option_sets.list_items(app_dir, "mypack/items")]
    assert "icons" not in values


def test_command_option_template_method() -> None:
    """コマンドの option_template が Template/ 相対の実パスを返すこと。"""
    from Commands.PythonCommandBase import PythonCommand

    class Cmd(PythonCommand):
        def do(self) -> None:
            pass

    cmd = Cmd()
    assert cmd.option_template("controller_check", "btn_a") == (
        "controller_check/btn_a.png"
    )
    with pytest.raises(option_sets.OptionSetError):
        cmd.option_template("controller_check", "nope")
    with pytest.raises(option_sets.OptionSetError):
        cmd.option_template("nope", "btn_a")


def test_pokecon_list_helpers() -> None:
    from Commands.PythonCommandBase import PythonCommand

    class Cmd(PythonCommand):
        def do(self) -> None:
            pass

    cmd = Cmd()
    values = ["a", "b"]
    cmd.pokecon_list_remove(values, "a")
    cmd.pokecon_list_remove(values, "missing")
    assert values == ["b"]
    with pytest.raises(TypeError):
        cmd.pokecon_list("abc")


def test_list_image_templates_excludes_set_icons_only(tmp_path: Path) -> None:
    """セット直下の icons だけを候補から外し、他の icons は残す。"""
    from services import blockly_templates

    root = Path(tmp_path) / "SerialController"
    for rel in (
        "Template/pkg/set/a.png",
        "Template/pkg/set/icons/a.png",
        "Template/icons/keep.png",
        "Template/pkg/icons/b.png",
    ):
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"image")
    names = blockly_templates.list_image_templates(root)
    assert "pkg/set/a.png" in names
    assert "pkg/set/icons/a.png" not in names
    assert "icons/keep.png" in names
    assert "pkg/icons/b.png" in names
