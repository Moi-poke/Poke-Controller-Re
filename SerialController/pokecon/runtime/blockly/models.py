#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""コンパクト仕様から Blockly の保存物を作る（GUI非依存）。

エージェント向けの小さな仕様（`docs/design-blockly-agent.md`）を、
決定的に Blockly の workspace JSON へ組み、本物の Blockly（node）で
Python を生成し、保存前検査を通してから `.blockly.json` / `.py` の対を
保存する。例外は外へ投げず、日本語のエラー一覧で返す。
"""

from __future__ import annotations

import copy
import difflib
import json
import keyword
import re
from dataclasses import dataclass, field as dataclass_field
from typing import Any

_STEM_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_CROP_RE = re.compile(r"^\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*$")
_DYNAMIC_IF_INPUT_RE = re.compile(r"^(IF|DO)(\d+)$")

#: 仕様のトップレベルで許す鍵。
_TOP_KEYS = frozenset({"stem", "name", "tags", "do", "subs"})

#: 短縮形（文）の一覧。
_SHORT_FORMS = frozenset(
    {
        "press",
        "hold",
        "release",
        "stick",
        "stick_off",
        "wait",
        "press_rep",
        "repeat",
        "while",
        "if",
        "break",
        "continue",
        "print",
        "comment",
        "finish",
        "set",
        "change",
        "call",
        "call_value",
        "screenshot",
        "discord",
        "dialog_choice",
        "dialog_number",
        "dialog_check",
    }
)

#: 短縮形ごとに許す追加項目。`press` の `wait` のように短縮形名と
#: パラメータ名が衝突するため、残りの項目を説明できる形で主キーを選ぶ。
_SHORT_PARAMS: dict[str, frozenset[str]] = {
    "press": frozenset({"duration", "wait"}),
    "hold": frozenset({"wait"}),
    "release": frozenset(),
    "stick": frozenset({"angle", "duration", "wait"}),
    "stick_off": frozenset(),
    "wait": frozenset(),
    "press_rep": frozenset({"count", "duration", "interval", "wait"}),
    "repeat": frozenset({"do"}),
    "while": frozenset({"do"}),
    "if": frozenset({"do", "else"}),
    "break": frozenset(),
    "continue": frozenset(),
    "print": frozenset({"kind", "result"}),
    "comment": frozenset(),
    "finish": frozenset(),
    "set": frozenset({"to"}),
    "change": frozenset({"by"}),
    "call": frozenset({"args"}),
    "call_value": frozenset({"args"}),
    "screenshot": frozenset(),
    "discord": frozenset({"kind"}),
    "dialog_choice": frozenset({"var", "title", "label", "options", "default"}),
    "dialog_number": frozenset({"var", "title", "label", "min", "max", "default"}),
    "dialog_check": frozenset({"var", "title", "label", "default"}),
}

#: 設定ダイアログのブロック種。VAR は Blockly 変数ではなく Python の文字列。
_DIALOG_BLOCKS = frozenset(
    {"pokecon_dialog_choice", "pokecon_dialog_number", "pokecon_dialog_check"}
)

#: 表示専用で、仕様に書かれても無視するフィールド。
_IGNORED_FIELDS = frozenset({"PREVIEW", "CAPOPEN"})

#: サブルーチン呼び出しブロックの最大引数（Blockly ブロックの制約）。
_MAX_SUB_ARGS = 3

#: サブルーチン名に使えない名前（生成コードの構造と衝突する）。
_RESERVED_SUB_NAMES = frozenset({"do", "__init__", "self", "NAME", "TAGS"})

#: 設定ダイアログの受け変数に使えない名前（core.blockly_validate と同方向）。
_DIALOG_RESERVED = frozenset({"self", "do", "NAME", "True", "False", "None"})


@dataclass
class BuildResult:
    """仕様→workspace JSON の結果。"""

    workspace: dict[str, Any] | None
    errors: list[str] = dataclass_field(default_factory=list)
    warnings: list[str] = dataclass_field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.workspace is not None and not self.errors


@dataclass
class CheckResult:
    """保存前検査の結果。"""

    ok: bool
    errors: list[str] = dataclass_field(default_factory=list)
    warnings: list[str] = dataclass_field(default_factory=list)
    workspace: dict[str, Any] | None = None
    code: str = ""
    stem: str | None = None


@dataclass
class RenderResult:
    """workspace JSON → Python の結果。"""

    ok: bool
    code: str = ""
    errors: list[str] = dataclass_field(default_factory=list)
    warnings: list[str] = dataclass_field(default_factory=list)


@dataclass
class PatchResult:
    """workspace の部分編集の結果。"""

    workspace: dict[str, Any] | None
    errors: list[str] = dataclass_field(default_factory=list)
    changes: list[dict[str, Any]] = dataclass_field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.workspace is not None and not self.errors


def _workspace_object(
    workspace_json: Any,
) -> tuple[dict[str, Any] | None, list[str]]:
    """workspace JSON（dict か JSON 文字列）を dict にする。"""
    if isinstance(workspace_json, str):
        try:
            workspace = json.loads(workspace_json)
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            return None, [f"ワークスペースJSONが壊れています（{e}）"]
    elif isinstance(workspace_json, dict):
        workspace = copy.deepcopy(workspace_json)
    else:
        return None, ["ワークスペースJSONはオブジェクトで書いてください"]
    if not isinstance(workspace, dict):
        return None, ["ワークスペースJSONはオブジェクトで書いてください"]
    return workspace, []


def _iter_blocks(workspace: dict[str, Any]) -> list[dict[str, Any]]:
    """workspace 内の実ブロックを文書順に返す（影は含めない）。"""
    tops = workspace.get("blocks")
    if not isinstance(tops, dict) or not isinstance(tops.get("blocks"), list):
        return []
    found: list[dict[str, Any]] = []
    stack: list[Any] = list(reversed(tops["blocks"]))
    while stack:
        block = stack.pop()
        if not isinstance(block, dict):
            continue
        found.append(block)
        children: list[dict[str, Any]] = []
        inputs = block.get("inputs")
        if isinstance(inputs, dict):
            for slot in inputs.values():
                if isinstance(slot, dict):
                    inner = slot.get("block")
                    if isinstance(inner, dict):
                        children.append(inner)
        nxt = block.get("next")
        if isinstance(nxt, dict) and isinstance(nxt.get("block"), dict):
            children.append(nxt["block"])
        stack.extend(reversed(children))
    return found


def _dedupe(items: list[str]) -> list[str]:
    return list(dict.fromkeys(items))


def _spec_stem(spec: Any) -> str | None:
    if isinstance(spec, dict):
        value = spec.get("stem")
        if isinstance(value, str):
            return value
    return None


def _catalog_blocks(catalog: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(catalog, dict):
        return {}
    blocks = catalog.get("blocks")
    if not isinstance(blocks, dict):
        return {}
    return {str(name): info for name, info in blocks.items() if isinstance(info, dict)}


def _suggest_hint(word: str, candidates: set[str]) -> str:
    matches = difflib.get_close_matches(word, sorted(candidates), n=3, cutoff=0.5)
    if not matches:
        return ""
    return "（似た名前: " + ", ".join(f"`{m}`" for m in matches) + "）"


def _is_identifier(name: str) -> bool:
    """Python の識別子として使えるか（予約語は除く）。"""
    return bool(name) and name.isidentifier() and not keyword.iskeyword(name)
