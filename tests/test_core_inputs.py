#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`core/inputs.py` の単体テスト。"""

from __future__ import annotations

from pathlib import Path

import pytest
from core import inputs


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


def _spec(app_dir: Path) -> list[inputs.InputSpec]:
    parsed, errors = inputs.parse_spec(
        [
            {
                "key": "targets",
                "kind": "choice",
                "set": "mypack/items",
                "multi": True,
                "default": "*",
                "title": "対象",
            },
            {
                "key": "max_rounds",
                "kind": "int",
                "min": 1,
                "max": 100,
                "default": 10,
                "title": "上限",
            },
        ],
        app_dir,
    )
    assert errors == []
    return parsed


def test_parse_and_validate(app_dir: Path) -> None:
    parsed, errors = inputs.parse_spec(
        [
            {
                "key": "targets",
                "kind": "choice",
                "set": "mypack/items",
                "multi": True,
            }
        ],
        app_dir,
    )
    assert errors == []
    assert [spec.key for spec in parsed] == ["targets"]
    bad = [
        {"key": "Bad", "kind": "choice", "set": "mypack/items"},
        {"key": "bad", "kind": "choice", "set": "mypack/items"},
        {"key": "bad", "kind": "choice", "set": "mypack/items"},
    ]
    errors = inputs.validate_spec(bad, app_dir)
    assert any("key" in error for error in errors)
    assert any("重複" in error for error in errors)


def test_validate_set_labels_and_default(app_dir: Path) -> None:
    errors = inputs.validate_spec(
        [
            {
                "key": "targets",
                "kind": "choice",
                "set": "mypack/missing",
                "multi": True,
            }
        ],
        app_dir,
    )
    assert any("セット" in error for error in errors)

    errors = inputs.validate_spec(
        [
            {
                "key": "targets",
                "kind": "choice",
                "set": "mypack/items",
                "labels": {"nope": "x"},
                "default": "nope",
            }
        ],
        app_dir,
    )
    assert any("labels" in error for error in errors)
    assert any("既定値" in error for error in errors)


def test_validate_int_range(app_dir: Path) -> None:
    errors = inputs.validate_spec(
        [{"key": "n", "kind": "int", "min": 10, "max": 1, "default": 5}], app_dir
    )
    assert any("min" in error for error in errors)
    errors = inputs.validate_spec(
        [{"key": "n", "kind": "int", "min": 1, "max": 10, "default": 50}], app_dir
    )
    assert any("既定値" in error for error in errors)


def test_coerce_choice_and_int(app_dir: Path) -> None:
    targets = _spec(app_dir)[0]
    assert inputs.coerce_value(targets, "*", app_dir, allow_all=True) == [
        "ball",
        "potion",
        "revive",
    ]
    assert inputs.coerce_value(targets, "revive,potion", app_dir) == [
        "potion",
        "revive",
    ]
    assert inputs.coerce_value(targets, ["ball", "ball"], app_dir) == ["ball"]
    with pytest.raises(inputs.InputError):
        inputs.coerce_value(targets, [], app_dir)
    with pytest.raises(inputs.InputError):
        inputs.coerce_value(targets, "nope", app_dir)

    rounds = _spec(app_dir)[1]
    assert inputs.coerce_value(rounds, "7", app_dir) == 7
    assert inputs.coerce_value(rounds, 7, app_dir) == 7
    with pytest.raises(inputs.InputError):
        inputs.coerce_value(rounds, True, app_dir)
    with pytest.raises(inputs.InputError):
        inputs.coerce_value(rounds, 0, app_dir)


def test_resolve_answer_default_fail_dialog(app_dir: Path) -> None:
    specs = _spec(app_dir)

    res = inputs.resolve(
        specs, {"targets": ["potion"], "max_rounds": 5}, "fail", app_dir
    )
    assert res.ok
    assert res.values == {"targets": ["potion"], "max_rounds": 5}
    assert res.sources == {"targets": "answer", "max_rounds": "answer"}

    res = inputs.resolve(specs, {}, "default", app_dir)
    assert res.ok
    assert res.values == {"targets": ["ball", "potion", "revive"], "max_rounds": 10}
    assert res.sources == {"targets": "default", "max_rounds": "default"}

    res = inputs.resolve(specs, {}, "fail", app_dir)
    assert not res.ok
    assert [item["key"] for item in res.missing] == ["targets", "max_rounds"]

    res = inputs.resolve(
        specs,
        {},
        "dialog",
        app_dir,
        dialog_values={"targets": "ball", "max_rounds": 3},
    )
    assert res.ok
    assert res.values == {"targets": ["ball"], "max_rounds": 3}
    assert res.sources == {"targets": "dialog", "max_rounds": "dialog"}


def test_resolve_unknown_and_invalid(app_dir: Path) -> None:
    specs = _spec(app_dir)
    res = inputs.resolve(specs, {"nope": 1}, "fail", app_dir)
    assert any("宣言にない" in error for error in res.errors)

    res = inputs.resolve(specs, {"targets": ["nope"]}, "fail", app_dir)
    assert any("nope" in error for error in res.errors)

    res = inputs.resolve(
        specs, {"targets": "potion", "max_rounds": 101}, "fail", app_dir
    )
    assert any("範囲外" in error for error in res.errors)


def test_spec_from_code(app_dir: Path) -> None:
    code = """
class BlocklyCmd(ImageProcPythonCommand):
    NAME = "x"
    POKECON_INPUTS = [
        {"key": "targets", "kind": "choice", "set": "mypack/items",
         "multi": True, "default": "*"},
        {"key": "n", "kind": "int", "min": 1, "max": 3, "default": 2},
    ]
    def do(self) -> None:
        targets = self.input_value("targets")
        n = self.input_value("n")
"""
    specs, errors = inputs.spec_from_code(code, app_dir)
    assert errors == []
    assert specs is not None
    assert [spec.key for spec in specs] == ["targets", "n"]
    assert inputs.input_keys_in_code(code) == ["targets", "n"]

    specs, errors = inputs.spec_from_code('NAME = "x"', app_dir)
    assert specs == [] and errors == []

    _, errors = inputs.spec_from_code(
        "class A:\n    POKECON_INPUTS = make_spec()\n", app_dir
    )
    assert errors


def test_unlimited_int_allows_none(app_dir: Path) -> None:
    """allow_unlimited の int は None/空を「上限なし」として通すこと。"""
    specs, errors = inputs.parse_spec(
        [
            {
                "key": "max_rounds",
                "kind": "int",
                "min": 1,
                "max": 100,
                "default": None,
                "allow_unlimited": True,
            }
        ],
        app_dir,
    )
    assert errors == []
    spec = specs[0]
    assert spec.allow_unlimited is True
    assert spec.default_set is True
    assert inputs.coerce_value(spec, None, app_dir) is None
    assert inputs.coerce_value(spec, "", app_dir) is None
    res = inputs.resolve(specs, {}, "default", app_dir)
    assert res.values == {"max_rounds": None}
    assert res.sources == {"max_rounds": "default"}

    no_allow, errors = inputs.parse_spec(
        [
            {
                "key": "n",
                "kind": "int",
                "min": 1,
                "max": 100,
                "default": None,
            }
        ],
        app_dir,
    )
    assert errors
    assert no_allow == []
