#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""実行時引数・動的テンプレート・一覧ブロックの生成テスト。"""

from __future__ import annotations

import json
from typing import Any

import pytest
from blockly_node import NEEDS_NODE, run_blockly
from core import blockly_validate
from services import blockly_templates

pytestmark = NEEDS_NODE


def _block(
    block_type: str,
    block_id: str,
    fields: dict[str, Any] | None = None,
    inputs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    block: dict[str, Any] = {"type": block_type, "id": block_id}
    if fields:
        block["fields"] = fields
    if inputs:
        block["inputs"] = inputs
    return block


def _program(do: dict[str, Any]) -> dict[str, Any]:
    return {
        "blocks": {
            "languageVersion": 0,
            "blocks": [
                _block(
                    "pokecon_program",
                    "P",
                    {"NAME": "probe", "TAGS": "blockly"},
                    {"DO": {"block": do}},
                )
            ],
        }
    }


def _generate(workspace: dict[str, Any]) -> str:
    body = (
        "const ws = new Blockly.Workspace();"
        "Blockly.serialization.workspaces.load(%s, ws);"
        "done({code: Blockly.Python.workspaceToCode(ws)});"
    ) % json.dumps(workspace, ensure_ascii=False)
    out = run_blockly(body)
    return str(out["code"])


def _text(value: str) -> dict[str, Any]:
    return {"block": _block("text", "t", {"TEXT": value})}


@pytest.mark.parametrize(
    "block_type, statement, expected",
    [
        ("pokecon_vision_contains", False, 'self.isContainTemplate("tpl.png"'),
        ("pokecon_vision_wait_appear", True, 'self.waitTemplate("tpl.png"'),
        ("pokecon_vision_wait_gone", True, 'self.waitTemplateGone("tpl.png"'),
        ("pokecon_vision_press_until", True, 'self.press_until("tpl.png"'),
        (
            "pokecon_vision_press_until_gone",
            True,
            'self.press_until_gone("tpl.png"',
        ),
        ("pokecon_vision_wait_count", True, 'self.wait_count("tpl.png"'),
        ("pokecon_vision_count", False, 'self.countTemplate("tpl.png"'),
        ("pokecon_vision_position", False, 'self.getTemplatePosition("tpl.png"'),
    ],
)
def test_static_template_code_is_unchanged(
    block_type: str, statement: bool, expected: str
) -> None:
    """TEMPLATE_EXPR が未接続なら、従来どおり固定欄の文字列を埋め込む。"""
    fields: dict[str, Any] = {
        "TEMPLATE": "tpl.png",
        "THRESHOLD": 0.7,
        "CROP": "",
        "USE_GRAY": "FALSE",
    }
    if "press_until" in block_type:
        fields["TARGET"] = "Button.A"
        fields["TIMEOUT"] = 1
    if "wait" in block_type:
        fields["TIMEOUT"] = 1
    if "wait_count" in block_type:
        fields["COUNT"] = 2
    block = _block(block_type, "v", fields)
    if statement:
        code = _generate(_program(block))
    else:
        code = _generate(
            _program(
                _block(
                    "pokecon_print",
                    "p",
                    {"KIND": "print"},
                    {"TEXT": {"block": block}},
                )
            )
        )
    assert expected in code
    assert "TEMPLATE_EXPR" not in code


def test_dynamic_template_uses_expression_and_ignores_field() -> None:
    """TEMPLATE_EXPR がつながっていれば、固定欄ではなく式を使う。"""
    option = _block(
        "pokecon_option_template",
        "opt",
        {"SET": "blockly"},
        {"VALUE": _text("controller_verify_a")},
    )
    vision = _block(
        "pokecon_vision_contains",
        "v",
        {
            "TEMPLATE": "ignored.png",
            "THRESHOLD": 0.7,
            "CROP": "",
            "USE_GRAY": "FALSE",
        },
        {"TEMPLATE_EXPR": {"block": option}},
    )
    code = _generate(
        _program(
            _block("pokecon_print", "p", {"KIND": "print"}, {"TEXT": {"block": vision}})
        )
    )
    assert "self.option_template(\"blockly\", 'controller_verify_a')" in code
    assert "ignored.png" not in code


def test_linked_input_disables_template_field() -> None:
    """式がつながっている間は固定欄を無効にし、プレビューを隠す。"""
    option = _block(
        "pokecon_option_template",
        "opt",
        {"SET": "blockly"},
        {"VALUE": _text("controller_verify_a")},
    )
    vision = _block(
        "pokecon_vision_contains",
        "v",
        {"TEMPLATE": "ignored.png", "THRESHOLD": 0.7, "CROP": "", "USE_GRAY": "FALSE"},
        {"TEMPLATE_EXPR": {"block": option}},
    )
    workspace = _program(
        _block("pokecon_print", "p", {"KIND": "print"}, {"TEXT": {"block": vision}})
    )
    body = (
        "const ws = new Blockly.Workspace();"
        "Blockly.serialization.workspaces.load(%s, ws);"
        "Blockly.PokeconEditor.refreshTemplateFields(ws);"
        "const b = ws.getBlockById('v');"
        "const f = b.getField('TEMPLATE');"
        "const p = b.getField('PREVIEW');"
        "done({enabled: f.isEnabled(), visible: p.isVisible ? p.isVisible() : null});"
    ) % json.dumps(workspace, ensure_ascii=False)
    out = run_blockly(body)
    assert out["enabled"] is False
    assert out["visible"] is False


def test_input_declarations_become_class_attribute() -> None:
    """宣言ブロックは do() ではなくクラス属性にまとめる。"""
    choice = _block(
        "pokecon_input_choice",
        "in1",
        {
            "KEY": "targets",
            "TITLE": "対象",
            "SET": "blockly",
            "MULTI": "TRUE",
            "DEFAULT": "*",
            "LABELS": "controller_verify_a=A",
        },
    )
    integer = _block(
        "pokecon_input_int",
        "in2",
        {"KEY": "n", "TITLE": "回数", "MIN": 1, "MAX": 5, "DEFAULT": 2},
    )
    for_each = _block(
        "pokecon_for_each",
        "fe",
        {"VAR": {"id": "item", "name": "item"}},
        {
            "LIST": {"block": _block("pokecon_input_get", "g1", {"KEY": "targets"})},
            "DO": {
                "block": _block(
                    "pokecon_list_remove",
                    "rm",
                    None,
                    {
                        "LIST": {
                            "block": _block(
                                "pokecon_input_get", "g2", {"KEY": "targets"}
                            )
                        },
                        "VALUE": {
                            "block": _block(
                                "variables_get",
                                "v1",
                                {"VAR": {"id": "item", "name": "item"}},
                            )
                        },
                    },
                )
            },
        },
    )
    workspace = _program(for_each)
    workspace["blocks"]["blocks"].extend([choice, integer])
    code = _generate(workspace)
    assert "POKECON_INPUTS = [" in code
    assert '"key": "targets"' in code
    assert '"labels": {"controller_verify_a": "A"}' in code
    assert '"key": "n"' in code
    assert 'for item in list(self.pokecon_list(self.input_value("targets"))):' in code
    assert 'self.pokecon_list_remove(self.input_value("targets"), item)' in code


def test_option_template_warning_is_suppressed_only_for_direct_call() -> None:
    """直接指定は動的警告を出さず、文字列連結なら従来どおり警告する。"""
    direct = "self.isContainTemplate(self.option_template('blockly', x), threshold=0.7)"
    wrapped = "self.isContainTemplate('blockly/' + x + '.png', threshold=0.7)"
    assert blockly_templates.warn_template_refs(direct) == []
    assert blockly_templates.warn_template_refs(wrapped)


def test_validation_rejects_unknown_key_and_missing_set(tmp_path: Any) -> None:
    """宣言にない key と、実在しないセットを保存時に落とす。"""
    code = (
        "class C:\n"
        "    POKECON_INPUTS = [{'key': 'n', 'kind': 'int', 'min': 1, 'max': 2}]\n"
        "    def do(self):\n"
        "        self.input_value('missing')\n"
        "        self.option_template('nope', 'x')\n"
    )
    errors = blockly_validate.validate_input_code(code, tmp_path)
    assert any("key" in e for e in errors)
    assert any("セット" in e for e in errors)


def _repeat_limit(limit: dict[str, Any]) -> dict[str, Any]:
    return _block(
        "pokecon_repeat_limit",
        "rep",
        {"VAR": {"id": "i", "name": "i"}},
        {
            "LIMIT": {"block": limit},
            "DO": {
                "block": _block(
                    "pokecon_print",
                    "p",
                    {"KIND": "print"},
                    {"TEXT": _text("x")},
                )
            },
        },
    )


def test_repeat_limit_handles_an_unset_limit() -> None:
    """上限が None なら while で回り続け、数値ならその回数で止まること。"""
    integer = _block(
        "pokecon_input_int",
        "in1",
        {
            "KEY": "max_rounds",
            "TITLE": "上限周回",
            "MIN": 1,
            "MAX": 100,
            "DEFAULT": 10,
            "ALLOW_UNLIMITED": "TRUE",
            "DEFAULT_UNLIMITED": "FALSE",
        },
    )
    workspace = _program(
        _repeat_limit(_block("pokecon_input_get", "g", {"KEY": "max_rounds"}))
    )
    workspace["blocks"]["blocks"].append(integer)
    code = _generate(workspace)
    assert '"allow_unlimited": True' in code
    assert "while _pokecon_limit_rep is None or i < _pokecon_limit_rep:" in code
    assert "i += 1" in code
    assert 'self.input_value("max_rounds")' in code


def test_repeat_limit_accepts_a_literal_limit() -> None:
    """数値リテラルでも同じ形で回ること。"""
    code = _generate(
        _program(
            _repeat_limit(
                _block("math_number", "n", {"NUM": 3}),
            )
        )
    )
    assert "_pokecon_limit_rep = 3" in code
    assert "while _pokecon_limit_rep is None or i < _pokecon_limit_rep:" in code


def test_input_int_can_default_to_unlimited() -> None:
    """DEFAULT_UNLIMITED なら POKECON_INPUTS の default が None になること。"""
    integer = _block(
        "pokecon_input_int",
        "in1",
        {
            "KEY": "max_rounds",
            "TITLE": "上限周回",
            "MIN": 1,
            "MAX": 100,
            "DEFAULT": 10,
            "ALLOW_UNLIMITED": "TRUE",
            "DEFAULT_UNLIMITED": "TRUE",
        },
    )
    workspace = _program(
        _block("pokecon_print", "p", {"KIND": "print"}, {"TEXT": _text("x")})
    )
    workspace["blocks"]["blocks"].append(integer)
    code = _generate(workspace)
    assert '"allow_unlimited": True' in code
    assert '"default": None' in code
