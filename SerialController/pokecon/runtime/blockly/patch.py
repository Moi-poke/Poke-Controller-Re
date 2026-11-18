"""Blockly workspace JSON の部分編集（patch_workspace）。"""

from __future__ import annotations

import copy
from typing import Any

from pokecon.runtime.blockly.models import (
    _CROP_RE,
    PatchResult,
    _catalog_blocks,
    _iter_blocks,
    _workspace_object,
)

#: `patch_workspace` が受ける操作。
_PATCH_OPS = frozenset(
    {"set_field", "set_extra_state", "create_block", "connect", "delete_block"}
)


def patch_workspace(workspace_json: Any, operations: Any, catalog: Any) -> PatchResult:
    """workspace を部分編集する（保存はしない）。

    操作は全件を検証してから適用し、1件でも不正なら何も変えない。フィールドは
    カタログの型・選択肢に合わせて検査する。ブロックIDは変更しない。
    対象指定は `blockId` か `selector`（`type` + 0始まりの `occurrence`）の
    どちらか一方だけ。
    """
    workspace, errors = _workspace_object(workspace_json)
    if errors or workspace is None:
        return PatchResult(None, errors)
    ops, op_errors = _patch_operations(operations)
    if op_errors or ops is None:
        return PatchResult(None, op_errors)
    catalog_blocks = _catalog_blocks(catalog)
    changes: list[dict[str, Any]] = []
    for index, op in enumerate(ops, 1):
        where = f"操作{index}"
        if not isinstance(op, dict):
            return PatchResult(None, [f"{where}: オブジェクトで書いてください"])
        name = op.get("op")
        if name not in _PATCH_OPS:
            return PatchResult(
                None,
                [
                    f"{where}: 知らない操作です: {name!r}"
                    "（set_field / set_extra_state / create_block / connect / delete_block）"
                ],
            )
        blocks = _iter_blocks(workspace)
        by_id, id_errors = _blocks_by_id(blocks)
        if id_errors:
            return PatchResult(None, id_errors)
        if name in ("set_field", "set_extra_state"):
            result = _apply_field_op(
                workspace, op, str(name), blocks, by_id, catalog_blocks, where
            )
        elif name == "create_block":
            result = _apply_create_block(
                workspace, op, blocks, by_id, catalog_blocks, where
            )
        elif name == "connect":
            result = _apply_connect(workspace, op, blocks, by_id, catalog_blocks, where)
        else:
            result = _apply_delete_block(workspace, op, blocks, by_id, where)
        if isinstance(result, str):
            return PatchResult(None, [result])
        changes.extend(result)
    return PatchResult(workspace, [], changes)


def _apply_field_op(
    workspace: dict[str, Any],
    op: dict[str, Any],
    name: str,
    blocks: list[dict[str, Any]],
    by_id: dict[str, dict[str, Any]],
    catalog_blocks: dict[str, dict[str, Any]],
    where: str,
) -> list[dict[str, Any]] | str:
    target, target_error = _patch_target(op, blocks, by_id, where)
    if target_error is not None or target is None:
        return target_error or f"{where}: 対象がありません"
    block_type = str(target.get("type", ""))
    if name == "set_field":
        field = op.get("field")
        if not isinstance(field, str) or not field:
            return f"{where}: field を文字列で書いてください"
        info = catalog_blocks.get(block_type)
        if info is None:
            return f"{where}: ブロック種 {block_type!r} はカタログにありません"
        spec = _field_spec(info, field)
        if spec is None:
            return f"{where}: {block_type} にフィールド {field!r} はありません"
        value, value_error = _coerce_field_value(spec, op.get("value"), where, field)
        if value_error is not None:
            return value_error
        fields = target.get("fields")
        if not isinstance(fields, dict):
            fields = {}
            target["fields"] = fields
        old = fields.get(field)
        fields[field] = value
        kind = spec.get("kind")
        live_value: Any = value
        if kind == "checkbox":
            live_value = value == "TRUE"
        return [
            {
                "op": "set_field",
                "blockId": target.get("id"),
                "type": block_type,
                "field": field,
                "kind": kind,
                "old": old,
                "new": value,
                "liveValue": live_value,
            }
        ]
    value = op.get("extraState")
    if value is not None and not isinstance(value, dict):
        return f"{where}: extraState はオブジェクトか null で書いてください"
    old_state = target.get("extraState")
    if value is None:
        target.pop("extraState", None)
    else:
        target["extraState"] = copy.deepcopy(value)
    return [
        {
            "op": "set_extra_state",
            "blockId": target.get("id"),
            "type": block_type,
            "old": old_state,
            "new": copy.deepcopy(value),
        }
    ]


def _apply_create_block(
    workspace: dict[str, Any],
    op: dict[str, Any],
    blocks: list[dict[str, Any]],
    by_id: dict[str, dict[str, Any]],
    catalog_blocks: dict[str, dict[str, Any]],
    where: str,
) -> list[dict[str, Any]] | str:
    block_type = op.get("type")
    if not isinstance(block_type, str) or not block_type:
        return f"{where}: type を文字列で書いてください"
    info = catalog_blocks.get(block_type)
    if info is None:
        return f"{where}: ブロック種 {block_type!r} はカタログにありません"
    block_id = op.get("id")
    if block_id is None:
        block_id = _new_block_id(workspace)
    if not isinstance(block_id, str) or not block_id:
        return f"{where}: id は空でない文字列で書いてください"
    if block_id in by_id:
        return f"{where}: ブロックIDが重複しています: {block_id!r}"
    fields = op.get("fields")
    if fields is not None and not isinstance(fields, dict):
        return f"{where}: fields はオブジェクトで書いてください"
    block: dict[str, Any] = {"type": block_type, "id": block_id}
    if isinstance(fields, dict) and fields:
        applied: dict[str, Any] = {}
        for field_name, raw in fields.items():
            if not isinstance(field_name, str) or not field_name:
                return f"{where}: fields のキーは文字列で書いてください"
            spec = _field_spec(info, field_name)
            if spec is None:
                return f"{where}: {block_type} にフィールド {field_name!r} はありません"
            value, value_error = _coerce_field_value(spec, raw, where, field_name)
            if value_error is not None:
                return value_error
            applied[field_name] = value
        block["fields"] = applied
    parent_ref = op.get("parent")
    after_ref = op.get("after")
    if parent_ref is not None and after_ref is not None:
        return f"{where}: parent と after は同時に使えません"
    if parent_ref is not None:
        input_name = op.get("input")
        if not isinstance(input_name, str) or not input_name:
            return f"{where}: parent を使うときは input も書いてください"
        parent, ref_error = _resolve_ref(parent_ref, blocks, by_id, where, "parent")
        if ref_error is not None or parent is None:
            return ref_error or f"{where}: parent がありません"
        insert = op.get("insert") is True
        problem = _connection_problem(
            catalog_blocks,
            parent,
            input_name,
            block,
            where,
            allow_occupied=insert,
        )
        if problem is not None:
            return problem
        inputs = parent.setdefault("inputs", {})
        slot = inputs.get(input_name)
        if isinstance(slot, dict) and isinstance(slot.get("block"), dict):
            # insert: 既存の並びを新しいブロックの後ろへつなぐ。
            block["next"] = {"block": slot["block"]}
        inputs[input_name] = {"block": block}
        target = f"{parent.get('id')}:{input_name}"
    elif after_ref is not None:
        after, ref_error = _resolve_ref(after_ref, blocks, by_id, where, "after")
        if ref_error is not None or after is None:
            return ref_error or f"{where}: after がありません"
        existing = None
        nxt = after.get("next")
        if isinstance(nxt, dict) and isinstance(nxt.get("block"), dict):
            if op.get("insert") is not True:
                return (
                    f"{where}: after の後ろは既に埋まっています（insert: true で挿入）"
                )
            existing = nxt["block"]
        if not info.get("previous"):
            return f"{where}: {block_type} は後ろへ接続できません"
        if existing is not None:
            block["next"] = {"block": existing}
        after["next"] = {"block": block}
        target = f"{after.get('id')}:next"
    else:
        x = op.get("x", 40)
        y = op.get("y", 40)
        if isinstance(x, bool) or not isinstance(x, (int, float)):
            return f"{where}: x は数値で書いてください"
        if isinstance(y, bool) or not isinstance(y, (int, float)):
            return f"{where}: y は数値で書いてください"
        block["x"] = x
        block["y"] = y
        workspace.setdefault("blocks", {}).setdefault("blocks", []).append(block)
        target = "top"
    return [
        {
            "op": "create_block",
            "blockId": block_id,
            "type": block_type,
            "target": target,
        }
    ]


def _apply_connect(
    workspace: dict[str, Any],
    op: dict[str, Any],
    blocks: list[dict[str, Any]],
    by_id: dict[str, dict[str, Any]],
    catalog_blocks: dict[str, dict[str, Any]],
    where: str,
) -> list[dict[str, Any]] | str:
    child, child_error = _resolve_ref(op.get("child"), blocks, by_id, where, "child")
    if child_error is not None or child is None:
        return child_error or f"{where}: child がありません"
    child_id = child.get("id")
    if not isinstance(child_id, str) or not child_id:
        return f"{where}: child にブロックIDがありません"
    parent_ref = op.get("parent")
    after_ref = op.get("after")
    if (parent_ref is None) == (after_ref is None):
        return f"{where}: parent か after のどちらか一方を書いてください"
    descendants = _subtree_ids(child)
    if parent_ref is not None:
        input_name = op.get("input")
        if not isinstance(input_name, str) or not input_name:
            return f"{where}: parent を使うときは input も書いてください"
        parent, ref_error = _resolve_ref(parent_ref, blocks, by_id, where, "parent")
        if ref_error is not None or parent is None:
            return ref_error or f"{where}: parent がありません"
        if parent.get("id") in descendants:
            return f"{where}: 自分の子孫へは接続できません"
        insert = op.get("insert") is True
        problem = _connection_problem(
            catalog_blocks,
            parent,
            input_name,
            child,
            where,
            allow_occupied=insert,
        )
        if problem is not None:
            return problem
        existing = None
        slot = parent.get("inputs", {}).get(input_name)
        if isinstance(slot, dict) and isinstance(slot.get("block"), dict):
            existing = slot["block"]
            if existing.get("id") in descendants:
                return f"{where}: 自分自身の並びへは挿入できません"
        detach_error = _detach_block(workspace, child)
        if detach_error is not None:
            return detach_error
        if existing is not None:
            _last_in_chain(child)["next"] = {"block": existing}
        parent.setdefault("inputs", {})[input_name] = {"block": child}
        target = f"{parent.get('id')}:{input_name}"
    else:
        after, ref_error = _resolve_ref(after_ref, blocks, by_id, where, "after")
        if ref_error is not None or after is None:
            return ref_error or f"{where}: after がありません"
        if after.get("id") in descendants:
            return f"{where}: 自分の子孫へは接続できません"
        existing = None
        nxt = after.get("next")
        if isinstance(nxt, dict) and isinstance(nxt.get("block"), dict):
            if op.get("insert") is not True:
                return (
                    f"{where}: after の後ろは既に埋まっています（insert: true で挿入）"
                )
            existing = nxt["block"]
            if existing.get("id") in descendants:
                return f"{where}: 自分自身の並びへは挿入できません"
        child_info = catalog_blocks.get(str(child.get("type")))
        if child_info is None or not child_info.get("previous"):
            return f"{where}: このブロックは後ろへ接続できません"
        detach_error = _detach_block(workspace, child)
        if detach_error is not None:
            return detach_error
        if existing is not None:
            _last_in_chain(child)["next"] = {"block": existing}
        after["next"] = {"block": child}
        target = f"{after.get('id')}:next"
    return [
        {
            "op": "connect",
            "blockId": child_id,
            "type": child.get("type"),
            "target": target,
        }
    ]


def _apply_delete_block(
    workspace: dict[str, Any],
    op: dict[str, Any],
    blocks: list[dict[str, Any]],
    by_id: dict[str, dict[str, Any]],
    where: str,
) -> list[dict[str, Any]] | str:
    target, target_error = _patch_target(op, blocks, by_id, where)
    if target_error is not None or target is None:
        return target_error or f"{where}: 対象がありません"
    block_type = str(target.get("type", ""))
    if block_type in ("pokecon_program", "pokecon_sub_def"):
        return f"{where}: プログラム / サブルーチン定義は削除できません"
    recursive = op.get("recursive") is True
    nested = False
    inputs = target.get("inputs")
    if isinstance(inputs, dict):
        nested = any(
            isinstance(slot, dict) and slot.get("block") for slot in inputs.values()
        )
    if nested and not recursive:
        return f"{where}: 子ブロックがあります（消すなら recursive: true を書いてください）"
    if recursive:
        detach_error = _detach_block(workspace, target)
    else:
        # 並びの途中なら、前後をつないで抜く（Blocklyの削除と同じ）。
        detach_error = _delete_splice(workspace, target)
    if detach_error is not None:
        return detach_error
    return [
        {
            "op": "delete_block",
            "blockId": target.get("id"),
            "type": block_type,
            "recursive": recursive,
        }
    ]


def _resolve_ref(
    ref: Any,
    blocks: list[dict[str, Any]],
    by_id: dict[str, dict[str, Any]],
    where: str,
    label: str,
) -> tuple[dict[str, Any] | None, str | None]:
    if not isinstance(ref, dict):
        return (
            None,
            f"{where}: {label} は blockId / selector のオブジェクトで書いてください",
        )
    fake = {"blockId": ref.get("blockId"), "selector": ref.get("selector")}
    return _patch_target(fake, blocks, by_id, f"{where} の {label}")


def _input_spec(info: dict[str, Any], name: str) -> dict[str, Any] | None:
    for spec in info.get("inputs", []):
        if isinstance(spec, dict) and str(spec.get("name")) == name:
            return spec
    return None


def _connection_problem(
    catalog_blocks: dict[str, dict[str, Any]],
    parent: dict[str, Any],
    input_name: str,
    child: dict[str, Any],
    where: str,
    *,
    allow_occupied: bool = False,
) -> str | None:
    parent_info = catalog_blocks.get(str(parent.get("type")))
    if parent_info is None:
        return f"{where}: parent のブロック種がカタログにありません"
    spec = _input_spec(parent_info, input_name)
    if spec is None:
        return f"{where}: {parent.get('type')} に接続口 {input_name!r} はありません"
    inputs = parent.get("inputs")
    if isinstance(inputs, dict):
        slot = inputs.get(input_name)
        if isinstance(slot, dict) and slot.get("block"):
            if not allow_occupied:
                return f"{where}: 接続口 {input_name!r} は既に埋まっています"
            if spec.get("kind") != "statement":
                return f"{where}: 値の接続口には挿入できません"
    child_info = catalog_blocks.get(str(child.get("type")))
    if child_info is None:
        return f"{where}: child のブロック種がカタログにありません"
    if spec.get("kind") == "statement":
        if not child_info.get("previous"):
            return f"{where}: このブロックは文として接続できません"
        return None
    if not child_info.get("output"):
        return f"{where}: このブロックは値として接続できません"
    check = spec.get("check") or []
    output = child_info.get("output") or []
    if check and output and not (set(check) & set(output)):
        return f"{where}: 型が合いません（入力 {check} / ブロック {output}）"
    return None


def _locate_block(
    workspace: dict[str, Any], block_id: str
) -> tuple[dict[str, Any] | None, tuple[str, Any, Any] | None]:
    top = workspace.get("blocks", {}).get("blocks", [])
    stack: list[tuple[Any, tuple[str, Any, Any]]] = [
        (block, ("top", None, None)) for block in top
    ]
    while stack:
        block, where = stack.pop()
        if not isinstance(block, dict):
            continue
        if block.get("id") == block_id:
            return block, where
        inputs = block.get("inputs")
        if isinstance(inputs, dict):
            for name, slot in inputs.items():
                if isinstance(slot, dict) and isinstance(slot.get("block"), dict):
                    stack.append((slot["block"], ("input", block, name)))
        nxt = block.get("next")
        if isinstance(nxt, dict) and isinstance(nxt.get("block"), dict):
            stack.append((nxt["block"], ("next", block, None)))
    return None, None


def _detach_block(workspace: dict[str, Any], block: dict[str, Any]) -> str | None:
    block_id = block.get("id")
    if not isinstance(block_id, str) or not block_id:
        return "ブロックIDがありません"
    found, where = _locate_block(workspace, block_id)
    if found is None or where is None:
        return f"ブロックが見つかりません: {block_id!r}"
    kind = where[0]
    if kind == "top":
        workspace["blocks"]["blocks"].remove(found)
        return None
    parent = where[1]
    if kind == "input":
        name = where[2]
        slot = parent.get("inputs", {}).get(name)
        if isinstance(slot, dict):
            slot.pop("block", None)
            if not slot:
                parent["inputs"].pop(name, None)
        return None
    parent.pop("next", None)
    return None


def _last_in_chain(block: dict[str, Any]) -> dict[str, Any]:
    current = block
    while True:
        nxt = current.get("next")
        if not (isinstance(nxt, dict) and isinstance(nxt.get("block"), dict)):
            return current
        current = nxt["block"]


def _delete_splice(workspace: dict[str, Any], block: dict[str, Any]) -> str | None:
    block_id = block.get("id")
    if not isinstance(block_id, str) or not block_id:
        return "ブロックIDがありません"
    found, where = _locate_block(workspace, block_id)
    if found is None or where is None:
        return f"ブロックが見つかりません: {block_id!r}"
    following = None
    nxt = found.get("next")
    if isinstance(nxt, dict) and isinstance(nxt.get("block"), dict):
        following = nxt["block"]
    kind = where[0]
    if kind == "top":
        top = workspace["blocks"]["blocks"]
        index = next((i for i, item in enumerate(top) if item is found), None)
        if index is None:
            return f"ブロックが見つかりません: {block_id!r}"
        if following is None:
            top.pop(index)
        else:
            top[index] = following
        return None
    parent = where[1]
    if kind == "input":
        name = where[2]
        slot = parent.get("inputs", {}).get(name)
        if not isinstance(slot, dict):
            return f"接続口 {name!r} が見つかりません"
        if following is None:
            slot.pop("block", None)
            if not slot:
                parent["inputs"].pop(name, None)
        else:
            slot["block"] = following
        return None
    if following is None:
        parent.pop("next", None)
    else:
        parent["next"] = {"block": following}
    return None


def _subtree_ids(block: Any) -> set[str]:
    found: set[str] = set()
    stack = [block]
    while stack:
        current = stack.pop()
        if not isinstance(current, dict):
            continue
        block_id = current.get("id")
        if isinstance(block_id, str) and block_id:
            found.add(block_id)
        inputs = current.get("inputs")
        if isinstance(inputs, dict):
            for slot in inputs.values():
                if isinstance(slot, dict) and isinstance(slot.get("block"), dict):
                    stack.append(slot["block"])
        nxt = current.get("next")
        if isinstance(nxt, dict) and isinstance(nxt.get("block"), dict):
            stack.append(nxt["block"])
    return found


def _new_block_id(workspace: dict[str, Any]) -> str:
    used = {block.get("id") for block in _iter_blocks(workspace)}
    for index in range(1, 10000):
        candidate = f"ai{index}"
        if candidate not in used:
            return candidate
    return "ai9999"


def _patch_operations(operations: Any) -> tuple[list[Any] | None, list[str]]:
    raw = operations
    if isinstance(raw, dict):
        raw = raw.get("operations")
    if not isinstance(raw, list):
        return None, ["operations は配列で書いてください"]
    if not raw:
        return None, ["operations が空です"]
    return raw, []


def _blocks_by_id(
    blocks: list[dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    by_id: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    for block in blocks:
        block_id = block.get("id")
        if not isinstance(block_id, str) or not block_id:
            continue
        if block_id in by_id:
            errors.append(f"ブロックIDが重複しています: {block_id!r}")
            continue
        by_id[block_id] = block
    return by_id, errors


def _patch_target(
    op: dict[str, Any],
    blocks: list[dict[str, Any]],
    by_id: dict[str, dict[str, Any]],
    where: str,
) -> tuple[dict[str, Any] | None, str | None]:
    block_id = op.get("blockId")
    selector = op.get("selector")
    if (block_id is None) == (selector is None):
        return None, f"{where}: blockId か selector のどちらか一方を書いてください"
    if block_id is not None:
        if not isinstance(block_id, str) or not block_id:
            return None, f"{where}: blockId は空でない文字列で書いてください"
        target = by_id.get(block_id)
        if target is None:
            return None, f"{where}: ブロックが見つかりません: {block_id!r}"
        return target, None
    if not isinstance(selector, dict):
        return None, f"{where}: selector はオブジェクトで書いてください"
    if "id" in selector:
        value = selector.get("id")
        if not isinstance(value, str) or not value:
            return None, f"{where}: selector.id は空でない文字列で書いてください"
        target = by_id.get(value)
        if target is None:
            return None, f"{where}: ブロックが見つかりません: {value!r}"
        return target, None
    block_type = selector.get("type")
    if not isinstance(block_type, str) or not block_type:
        return None, f"{where}: selector.type を文字列で書いてください"
    occurrence = selector.get("occurrence", 0)
    if (
        not isinstance(occurrence, int)
        or isinstance(occurrence, bool)
        or occurrence < 0
    ):
        return None, f"{where}: selector.occurrence は0以上の整数で書いてください"
    matches = [block for block in blocks if block.get("type") == block_type]
    if occurrence >= len(matches):
        return None, (
            f"{where}: {block_type} の {occurrence} 番目がありません"
            f"（{len(matches)} 個あります）"
        )
    return matches[occurrence], None


def _field_spec(info: dict[str, Any], name: str) -> dict[str, Any] | None:
    for spec in info.get("fields", []):
        if isinstance(spec, dict) and str(spec.get("name")) == name:
            return spec
    return None


def _coerce_field_value(
    spec: dict[str, Any], value: Any, where: str, field: str
) -> tuple[Any, str | None]:
    if spec.get("serializable") is False:
        return None, f"{where}: {field} は保存されない表示専用フィールドです"
    kind = spec.get("kind")
    if kind == "variable":
        return None, (
            f"{where}: {field} は変数フィールドです。patch では変更できません"
            "（workspace を直接編集するか、仕様から作り直してください）"
        )
    if kind == "checkbox":
        if isinstance(value, bool):
            return ("TRUE" if value else "FALSE"), None
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in ("true", "false"):
                return ("TRUE" if lowered == "true" else "FALSE"), None
        return None, f"{where}: {field} は true / false で書いてください"
    if kind == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            if isinstance(value, str):
                try:
                    value = float(value)
                except ValueError:
                    return None, f"{where}: {field} は数値で書いてください"
            else:
                return None, f"{where}: {field} は数値で書いてください"
        number = float(value)
        if not (number == number and number not in (float("inf"), float("-inf"))):
            return None, f"{where}: {field} は有限の数値で書いてください"
        if number.is_integer():
            return int(number), None
        return number, None
    if kind == "crop":
        if not isinstance(value, str):
            return None, f"{where}: {field} は文字列（x1,y1,x2,y2）で書いてください"
        text = value.strip()
        if not text:
            return "", None
        match = _CROP_RE.match(text)
        if match is None:
            return None, f"{where}: {field} は x1,y1,x2,y2 の形で書いてください"
        x1, y1, x2, y2 = (int(part) for part in match.groups())
        if x1 >= x2 or y1 >= y2:
            return None, f"{where}: {field} は x2>x1 / y2>y1 にしてください"
        return text, None
    if kind in ("controller", "stickpad"):
        options = spec.get("options")
        if isinstance(options, list) and options:
            allowed = {
                str(item[1])
                for item in options
                if isinstance(item, (list, tuple)) and len(item) > 1
            }
            if not isinstance(value, str) or value not in allowed:
                return (
                    None,
                    f"{where}: {field} は次のどれかで書いてください: {sorted(allowed)}",
                )
        elif not isinstance(value, str):
            return None, f"{where}: {field} は文字列で書いてください"
        return value, None
    if kind == "template":
        if not isinstance(value, str) or not value.strip():
            return None, f"{where}: {field} は空でない文字列で書いてください"
        return value, None
    if not isinstance(value, str):
        return None, f"{where}: {field} は文字列で書いてください"
    return value, None
