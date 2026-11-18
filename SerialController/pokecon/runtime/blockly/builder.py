"""コンパクト仕様から Blockly の workspace JSON を組み立てる。"""

from __future__ import annotations

import keyword
import re
from typing import Any

from pokecon.domain import blockly_validate
from pokecon.runtime.blockly.models import (
    _CROP_RE,
    _DIALOG_BLOCKS,
    _DIALOG_RESERVED,
    _DYNAMIC_IF_INPUT_RE,
    _IGNORED_FIELDS,
    _MAX_SUB_ARGS,
    _RESERVED_SUB_NAMES,
    _SHORT_FORMS,
    _SHORT_PARAMS,
    _STEM_RE,
    _TOP_KEYS,
    BuildResult,
    _catalog_blocks,
    _is_identifier,
    _suggest_hint,
)


def build_workspace(spec: Any, catalog: Any) -> BuildResult:
    """コンパクト仕様を workspace JSON へ組み立てる。例外は投げない。"""
    try:
        builder = _Builder(catalog)
        workspace = builder.build(spec)
    except Exception as e:  # 仕様が想定外の型でも落とさない
        return BuildResult(None, errors=[f"仕様を組み立てられませんでした（{e}）"])
    if builder.errors:
        return BuildResult(None, builder.errors, builder.warnings)
    return BuildResult(workspace, [], builder.warnings)


class _Builder:
    """仕様を workspace JSON へ組み立てる。エラーは self.errors に溜める。"""

    def __init__(self, catalog: Any) -> None:
        self.blocks = _catalog_blocks(catalog)
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.variables: list[dict[str, str]] = []
        self._var_ids: set[str] = set()
        self.sub_defs: dict[str, list[str]] = {}
        self._bad_sub_args: set[str] = set()
        self.calls: list[tuple[str, str, int]] = []
        self.caller = "do"

    # -- 入口 ---------------------------------------------------------------

    def build(self, spec: Any) -> dict[str, Any] | None:
        if not isinstance(spec, dict):
            self.error("仕様は JSON オブジェクトで書いてください")
            return None
        for key in spec:
            if key not in _TOP_KEYS:
                self.error(
                    f"仕様の項目 `{key}` は使えません"
                    + _suggest_hint(str(key), set(_TOP_KEYS))
                )
        stem = spec.get("stem")
        if stem is not None:
            if not isinstance(stem, str):
                self.error("stem は文字列で書いてください")
            else:
                for item in blockly_validate.validate_stem(stem):
                    self.error(f"stem: {item}")
        name = spec.get("name")
        if not isinstance(name, str) or not name.strip():
            self.error("name（一覧に出る表示名）を書いてください")
            name = "無題"
        tags = self._tags(spec.get("tags"))
        raw_do = spec.get("do", [])
        raw_subs = spec.get("subs", [])
        if not isinstance(raw_subs, list):
            self.error("subs は配列で書いてください")
            raw_subs = []

        # 呼び出しの解決に備え、先に定義名と引数だけ読む。
        sub_items: list[dict[str, Any]] = []
        for index, item in enumerate(raw_subs):
            if not isinstance(item, dict):
                self.error(f"subs[{index}] は JSON オブジェクトで書いてください")
                continue
            if not self._sub_metadata(item, f"subs[{index}]"):
                continue
            sub_items.append(item)

        self.caller = "do"
        body = self._statements(raw_do, "do")
        sub_blocks: list[dict[str, Any]] = []
        for item in sub_items:
            sub_block = self._sub_block(item)
            if sub_block is not None:
                sub_blocks.append(sub_block)
        self._validate_calls()

        program: dict[str, Any] = {
            "type": "pokecon_program",
            "fields": {"NAME": name, "TAGS": ",".join(tags)},
        }
        if body:
            program["inputs"] = {"DO": {"block": body[0]}}
        top = [program, *sub_blocks]
        workspace: dict[str, Any] = {
            "blocks": {"languageVersion": 0, "blocks": top},
        }
        if self.variables:
            workspace["variables"] = self.variables
        if self.errors:
            return None
        return workspace

    # -- メタ ---------------------------------------------------------------

    def _sub_metadata(self, item: dict[str, Any], where: str) -> bool:
        allowed = {"name", "args", "do", "return"}
        self._only(item, allowed, where, "サブプログラム")
        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            self.error(f"{where}: name を書いてください")
            return False
        name = name.strip()
        if not _is_identifier(name):
            self.error(f"サブプログラム名は識別子にしてください: {name}")
            return False
        if name in _RESERVED_SUB_NAMES or (
            name.startswith("__") and name.endswith("__")
        ):
            self.error(f"サブプログラム名に予約された名前は使えません: {name}")
            return False
        if name in self.sub_defs:
            self.error(f"サブプログラム名が重複しています: {name}")
            return False
        args = self._parse_sub_args(item.get("args", ""), where, name)
        self.sub_defs[name] = args
        return True

    def _parse_sub_args(self, raw: Any, where: str, name: str) -> list[str]:
        if raw is None:
            parts: list[str] = []
        elif isinstance(raw, str):
            parts = [part.strip() for part in raw.split(",")]
            parts = [part for part in parts if part]
        elif isinstance(raw, list):
            parts = []
            for part in raw:
                if not isinstance(part, str):
                    self.error(f"{where}: 引数名は文字列で書いてください")
                    continue
                if part.strip():
                    parts.append(part.strip())
        else:
            self.error(f"{where}: args は文字列か配列で書いてください")
            return []
        if len(parts) > _MAX_SUB_ARGS:
            self.error(f"{where}: サブルーチンの引数は最大 {_MAX_SUB_ARGS} 個までです")
            self._bad_sub_args.add(name)
            parts = parts[:_MAX_SUB_ARGS]
        seen: set[str] = set()
        valid = True
        for part in parts:
            if not _is_identifier(part):
                self.error(f"{where}: 引数名は識別子にしてください: {part}")
                valid = False
            elif part == "self":
                self.error(f"{where}: 引数名に self は使えません")
                valid = False
            elif part in seen:
                self.error(f"{where}: 引数名が重複しています: {part}")
                valid = False
            seen.add(part)
        if not valid:
            self._bad_sub_args.add(name)
        return parts

    def _sub_block(self, item: dict[str, Any]) -> dict[str, Any] | None:
        name = item["name"].strip()
        args = self.sub_defs.get(name, [])
        self.caller = name
        block: dict[str, Any] = {
            "type": "pokecon_sub_def",
            "fields": {"NAME": name, "ARGS": ",".join(args)},
        }
        raw_do = item.get("do", [])
        body = self._statements(raw_do, f"sub {name}")
        if body:
            block["inputs"] = {"DO": {"block": body[0]}}
        if "return" in item:
            value = self._value(item["return"], None, f"sub {name} return")
            if value is not None:
                block.setdefault("inputs", {})["RETURN"] = {"block": value}
        self.caller = "do"
        return self._finish(block, f"sub {name}")

    # -- 文の組み立て -------------------------------------------------------

    def _statement_param(self, raw: Any, where: str) -> list[dict[str, Any]]:
        """短縮形の `do` / `else` を文の並びにする（単一 dict も許容）。"""
        if isinstance(raw, dict):
            return self._statements([raw], where)
        return self._statements(raw, where)

    def _statements(self, raw: Any, where: str) -> list[dict[str, Any]]:
        if not isinstance(raw, list):
            self.error(f"{where}: 文の並びは配列で書いてください")
            return []
        built: list[dict[str, Any]] = []
        for index, item in enumerate(raw):
            block = self._statement(item, f"{where}[{index}]")
            if block is not None:
                built.append(block)
        for index in range(len(built) - 1):
            info = self._block_info(built[index]["type"])
            if info is not None and not info.get("next"):
                self.error(f"{where}: `{built[index]['type']}` の後ろに文を置けません")
                break
            built[index]["next"] = {"block": built[index + 1]}
        return built

    def _statement(self, item: Any, where: str) -> dict[str, Any] | None:
        if not isinstance(item, dict):
            self.error(f"{where}: 文は JSON オブジェクトで書いてください")
            return None
        if "type" in item:
            return self._generic(item, where, as_value=False)
        found = [key for key in item if key in _SHORT_FORMS]
        if not found:
            self.error(
                f"{where}: ブロックの種類が分かりません（`type` か短縮形を書いてください）"
            )
            return None
        candidates = [
            key
            for key in found
            if all(
                other == key
                or other not in _SHORT_FORMS
                or other in _SHORT_PARAMS.get(key, frozenset())
                for other in item
            )
        ]
        if len(candidates) != 1:
            self.error(f"{where}: 短縮形を1つに決められません（{', '.join(found)}）")
            return None
        short = candidates[0]
        params = {key: value for key, value in item.items() if key != short}
        handler = getattr(self, f"_short_{short}", None)
        if handler is None:
            self.error(f"{where}: `{short}` は使えません")
            return None
        block = handler(item[short], params, where)
        if block is None:
            return None
        if not self._require_statement(block, where):
            return None
        return block

    def _generic(
        self, item: dict[str, Any], where: str, *, as_value: bool
    ) -> dict[str, Any] | None:
        allowed = {"type", "fields", "inputs", "extraState"}
        self._only(item, allowed, where, "ブロック")
        block_type = item.get("type")
        if not isinstance(block_type, str) or not block_type:
            self.error(f"{where}: type を書いてください")
            return None
        if self._block_info(block_type) is None:
            self.error(
                f"{where}: ブロック `{block_type}` は使えません"
                + _suggest_hint(block_type, set(self.blocks))
            )
            return None
        block: dict[str, Any] = {"type": block_type}
        fields = item.get("fields", {})
        if fields is not None:
            if not isinstance(fields, dict):
                self.error(f"{where}: fields はオブジェクトで書いてください")
            else:
                for name, value in fields.items():
                    self._set_field(block, str(name), value, where)
        inputs = item.get("inputs", {})
        if inputs is not None:
            if not isinstance(inputs, dict):
                self.error(f"{where}: inputs はオブジェクトで書いてください")
            else:
                for name, value in inputs.items():
                    self._set_input(block, str(name), value, where)
        extra = item.get("extraState")
        if extra is not None:
            if not isinstance(extra, dict):
                self.error(f"{where}: extraState はオブジェクトで書いてください")
            else:
                block["extraState"] = extra
        if as_value:
            if not self._require_value(block, where):
                return None
        elif not self._require_statement(block, where):
            return None
        return self._finish(block, where)

    # -- 入力 ---------------------------------------------------------------

    def _set_input(
        self, block: dict[str, Any], name: str, value: Any, where: str
    ) -> None:
        info = self._input_info(block["type"], name)
        if info is None:
            self.error(
                f"{where}: ブロック `{block['type']}` に input `{name}` はありません"
                + _suggest_hint(name, self._input_names(block["type"]))
            )
            return
        if info.get("kind") == "statement":
            raw = value if isinstance(value, list) else [value]
            built = self._statements(raw, f"{where}.{name}")
            if built:
                block.setdefault("inputs", {})[name] = {"block": built[0]}
            return
        built_value = self._value(value, info.get("check"), f"{where}.{name}")
        if built_value is not None:
            block.setdefault("inputs", {})[name] = {"block": built_value}

    def _input_info(self, block_type: str, name: str) -> dict[str, Any] | None:
        info = self._block_info(block_type)
        if info is not None:
            for entry in info.get("inputs", []):
                if isinstance(entry, dict) and entry.get("name") == name:
                    return entry
        if block_type == "controls_if":
            if name == "ELSE":
                return {"name": "ELSE", "kind": "statement", "check": []}
            match = _DYNAMIC_IF_INPUT_RE.fullmatch(name)
            if match is not None:
                prefix = match.group(1)
                return {
                    "name": name,
                    "kind": "value" if prefix == "IF" else "statement",
                    "check": ["Boolean"] if prefix == "IF" else [],
                }
        return None

    def _input_names(self, block_type: str) -> set[str]:
        info = self._block_info(block_type)
        if info is None:
            return set()
        names = {
            str(entry.get("name"))
            for entry in info.get("inputs", [])
            if isinstance(entry, dict) and entry.get("name")
        }
        if block_type == "controls_if":
            names.update({"ELSE"})
        return names

    # -- 値 -----------------------------------------------------------------

    def _value(self, value: Any, check: Any, where: str) -> dict[str, Any] | None:
        check_list = [str(item) for item in check] if isinstance(check, list) else []
        built: dict[str, Any] | None
        if isinstance(value, dict):
            keys = set(value)
            if "type" in keys:
                built = self._generic(value, where, as_value=True)
            elif keys == {"var"}:
                name = self._var_name(value.get("var"), where)
                if name is None:
                    return None
                self._register_var(name)
                built = {"type": "variables_get", "fields": {"VAR": {"id": name}}}
            elif keys == {"elapsed"}:
                if value.get("elapsed") is not True:
                    self.error(f"{where}: elapsed は true で書いてください")
                    return None
                built = {"type": "pokecon_elapsed"}
            elif keys == {"call_value", "args"} or keys == {"call_value"}:
                built = self._short_call_value(
                    value.get("call_value"),
                    {"args": value.get("args", [])} if "args" in keys else {},
                    where,
                )
            else:
                self.error(
                    f"{where}: 値の書き方が分かりません（`type` / `var` / `elapsed` / "
                    "`call_value` のどれかで書いてください）"
                )
                return None
        else:
            if isinstance(value, bool):
                built = {
                    "type": "logic_boolean",
                    "fields": {"BOOL": "TRUE" if value else "FALSE"},
                }
            elif isinstance(value, int | float):
                built = {"type": "math_number", "fields": {"NUM": value}}
            elif isinstance(value, str):
                built = {"type": "text", "fields": {"TEXT": value}}
            else:
                self.error(
                    f"{where}: 値は数値・文字列・真偽値・ブロックで書いてください"
                )
                return None
        if built is None:
            return None
        if check_list and not self._value_fits(built["type"], check_list):
            self.error(
                f"{where}: この入力は {', '.join(check_list)} 型です"
                f"（{built['type']} は使えません）"
            )
            return None
        return built

    def _value_fits(self, block_type: str, check: list[str]) -> bool:
        info = self._block_info(block_type)
        if info is None:
            return False
        output = info.get("output")
        if output is None:
            return False
        if not isinstance(output, list) or not output:
            return True
        return bool(set(str(item) for item in output) & set(check))

    # -- フィールド ---------------------------------------------------------

    def _set_field(
        self, block: dict[str, Any], name: str, value: Any, where: str
    ) -> None:
        block_type = block["type"]
        if name in _IGNORED_FIELDS:
            return
        info = self._block_info(block_type)
        if info is None:
            self.error(f"{where}: ブロック `{block_type}` がカタログにありません")
            return
        spec = self._field_info(block_type, name)
        if spec is None:
            self.error(
                f"{where}: ブロック `{block_type}` に field `{name}` はありません"
                + _suggest_hint(name, self._field_names(block_type))
            )
            return
        if spec.get("serializable") is False:
            return
        kind = str(spec.get("kind", "text"))
        converted = self._convert_field(block_type, name, kind, spec, value, where)
        if converted is None:
            return
        block.setdefault("fields", {})[name] = converted
        if kind == "crop" and isinstance(converted, str) and converted:
            self._validate_crop(converted, where)
        if name == "THRESHOLD" and isinstance(converted, int | float):
            if isinstance(converted, bool) or not 0 <= converted <= 1:
                self.error(f"{where}: THRESHOLD は 0〜1 で書いてください: {converted}")

    def _convert_field(
        self,
        block_type: str,
        name: str,
        kind: str,
        spec: dict[str, Any],
        value: Any,
        where: str,
    ) -> Any:
        if kind == "variable":
            if block_type in _DIALOG_BLOCKS:
                text = self._require_str(value, where, f"{name}（受け変数）")
                if text is None:
                    return None
                if (
                    _STEM_RE.fullmatch(text) is None
                    or text in keyword.kwlist
                    or text in _DIALOG_RESERVED
                ):
                    self.error(
                        f"{where}: 受け変数名は英字・数字・`_`にしてください: {text}"
                    )
                    return None
                return text
            variable = self._var_name(value, where)
            if variable is None:
                return None
            self._register_var(variable)
            return {"id": variable}
        if kind == "number":
            if isinstance(value, bool) or not isinstance(value, int | float):
                self.error(f"{where}: `{name}` は数値で書いてください: {value!r}")
                return None
            return value
        if kind == "checkbox":
            if isinstance(value, bool):
                return "TRUE" if value else "FALSE"
            if isinstance(value, str) and value.upper() in ("TRUE", "FALSE"):
                return value.upper()
            self.error(f"{where}: `{name}` は true / false で書いてください")
            return None
        if kind == "controller":
            return self._controller_value(name, spec, value, where)
        if kind == "stickpad":
            text = self._require_str(value, where, name)
            if text is None:
                return None
            if (
                re.fullmatch(r"\s*-?\d+(?:\.\d+)?\s*,\s*-?\d+(?:\.\d+)?\s*", text)
                is None
            ):
                self.error(
                    f"{where}: `{name}` は `角度,強さ` で書いてください: {text!r}"
                )
                return None
            return text
        if kind in ("display", "image"):
            return None
        text = self._require_str(value, where, name)
        if text is None:
            return None
        if name == "OPTIONS" and isinstance(value, list):
            # `_require_str` が先に落とすため、ここへは来ない（保険）。
            text = ",".join(str(item) for item in value)
        options = spec.get("options")
        if name not in ("NAME", "TEMPLATE") and isinstance(options, list) and options:
            values = [
                str(option[1])
                for option in options
                if isinstance(option, list | tuple) and len(option) > 1
            ]
            if values and text not in values:
                self.error(
                    f"{where}: `{name}` の値が不正です: {text!r}"
                    + _suggest_hint(text, set(values))
                )
                return None
        return text

    def _controller_value(
        self,
        name: str,
        spec: dict[str, Any],
        value: Any,
        where: str,
    ) -> str | None:
        text = self._require_str(value, where, name)
        if text is None:
            return None
        options = spec.get("options")
        values = (
            [
                str(option[1])
                for option in options
                if isinstance(option, list | tuple) and len(option) > 1
            ]
            if isinstance(options, list)
            else []
        )
        if not values:
            return text
        if text in values:
            return text
        if not text.startswith(("Button.", "Hat.", "Direction.")):
            candidate = "Button." + text
            if candidate in values:
                return candidate
        else:
            bare = text.split(".", 1)[1]
            if bare in values:
                return bare
        self.error(
            f"{where}: `{name}` のボタン名が不正です: {text!r}"
            + _suggest_hint(text, set(values))
        )
        return None

    def _validate_crop(self, text: str, where: str) -> None:
        match = _CROP_RE.fullmatch(text)
        if match is None:
            self.error(f"{where}: CROP は空か `x1,y1,x2,y2` で書いてください: {text!r}")
            return
        x1, y1, x2, y2 = (int(match.group(i)) for i in range(1, 5))
        if x2 <= x1 or y2 <= y1:
            self.error(f"{where}: CROP は x2>x1 かつ y2>y1 にしてください: {text!r}")

    # -- 短縮形 -------------------------------------------------------------

    def _short_press(
        self, value: Any, params: dict[str, Any], where: str
    ) -> dict[str, Any] | None:
        self._only(params, {"duration", "wait"}, where, "press")
        block = self._new("pokecon_press", where)
        if block is None:
            return None
        self._set_field(block, "BUTTON", value, where)
        if "duration" in params:
            self._set_field(block, "DURATION", params["duration"], where)
        if "wait" in params:
            self._set_field(block, "WAIT", params["wait"], where)
        return self._finish(block, where)

    def _short_hold(
        self, value: Any, params: dict[str, Any], where: str
    ) -> dict[str, Any] | None:
        self._only(params, {"wait"}, where, "hold")
        block = self._new("pokecon_hold", where)
        if block is None:
            return None
        self._set_field(block, "TARGET", value, where)
        if "wait" in params:
            self._set_field(block, "WAIT", params["wait"], where)
        return self._finish(block, where)

    def _short_release(
        self, value: Any, params: dict[str, Any], where: str
    ) -> dict[str, Any] | None:
        self._only(params, set(), where, "release")
        block = self._new("pokecon_hold_end", where)
        if block is None:
            return None
        self._set_field(block, "TARGET", value, where)
        return self._finish(block, where)

    def _short_stick(
        self, value: Any, params: dict[str, Any], where: str
    ) -> dict[str, Any] | None:
        self._only(params, {"angle", "duration", "wait"}, where, "stick")
        block = self._new("pokecon_stick", where)
        if block is None:
            return None
        stick = self._stick_side(value, where)
        if stick is not None:
            self._set_field(block, "STICK", stick, where)
        angle = params.get("angle", 90)
        if isinstance(angle, bool) or not isinstance(angle, int | float):
            self.error(f"{where}: angle は数値で書いてください: {angle!r}")
        else:
            angle_text = str(int(angle)) if float(angle).is_integer() else str(angle)
            self._set_field(block, "PAD", f"{angle_text},100", where)
            self._set_field(block, "ANGLE", angle, where)
        if "duration" in params:
            self._set_field(block, "DURATION", params["duration"], where)
        if "wait" in params:
            self._set_field(block, "WAIT", params["wait"], where)
        return self._finish(block, where)

    def _short_stick_off(
        self, value: Any, params: dict[str, Any], where: str
    ) -> dict[str, Any] | None:
        self._only(params, set(), where, "stick_off")
        block = self._new("pokecon_stick_release", where)
        if block is None:
            return None
        stick = self._stick_side(value, where)
        if stick is not None:
            self._set_field(block, "STICK", stick, where)
        return self._finish(block, where)

    def _short_wait(
        self, value: Any, params: dict[str, Any], where: str
    ) -> dict[str, Any] | None:
        self._only(params, set(), where, "wait")
        block = self._new("pokecon_wait", where)
        if block is None:
            return None
        self._set_field(block, "SEC", value, where)
        return self._finish(block, where)

    def _short_press_rep(
        self, value: Any, params: dict[str, Any], where: str
    ) -> dict[str, Any] | None:
        self._only(
            params, {"count", "duration", "interval", "wait"}, where, "press_rep"
        )
        block = self._new("pokecon_press_rep", where)
        if block is None:
            return None
        self._set_field(block, "TARGET", value, where)
        for name, key in (
            ("COUNT", "count"),
            ("DURATION", "duration"),
            ("INTERVAL", "interval"),
            ("WAIT", "wait"),
        ):
            if key in params:
                self._set_field(block, name, params[key], where)
        return self._finish(block, where)

    def _short_repeat(
        self, value: Any, params: dict[str, Any], where: str
    ) -> dict[str, Any] | None:
        self._only(params, {"do"}, where, "repeat")
        if "do" not in params:
            self.error(f"{where}: repeat には do を書いてください")
            return None
        block = self._new("controls_repeat_ext", where)
        if block is None:
            return None
        times = self._value(value, ["Number"], f"{where}.repeat")
        if times is not None:
            block.setdefault("inputs", {})["TIMES"] = {"block": times}
        body = self._statement_param(params["do"], f"{where}.do")
        if body:
            block.setdefault("inputs", {})["DO"] = {"block": body[0]}
        return self._finish(block, where)

    def _short_while(
        self, value: Any, params: dict[str, Any], where: str
    ) -> dict[str, Any] | None:
        self._only(params, {"do"}, where, "while")
        if "do" not in params:
            self.error(f"{where}: while には do を書いてください")
            return None
        block = self._new("controls_whileUntil", where)
        if block is None:
            return None
        self._set_field(block, "MODE", "WHILE", where)
        condition = self._value(value, ["Boolean"], f"{where}.while")
        if condition is not None:
            block.setdefault("inputs", {})["BOOL"] = {"block": condition}
        body = self._statement_param(params["do"], f"{where}.do")
        if body:
            block.setdefault("inputs", {})["DO"] = {"block": body[0]}
        return self._finish(block, where)

    def _short_if(
        self, value: Any, params: dict[str, Any], where: str
    ) -> dict[str, Any] | None:
        self._only(params, {"do", "else"}, where, "if")
        if "do" not in params:
            self.error(f"{where}: if には do を書いてください")
            return None
        block = self._new("controls_if", where)
        if block is None:
            return None
        condition = self._value(value, ["Boolean"], f"{where}.if")
        if condition is not None:
            block.setdefault("inputs", {})["IF0"] = {"block": condition}
        body = self._statement_param(params["do"], f"{where}.do")
        if body:
            block.setdefault("inputs", {})["DO0"] = {"block": body[0]}
        if "else" in params:
            other = self._statement_param(params["else"], f"{where}.else")
            if other:
                block["extraState"] = {"hasElse": True}
                block.setdefault("inputs", {})["ELSE"] = {"block": other[0]}
        return self._finish(block, where)

    def _short_break(
        self, value: Any, params: dict[str, Any], where: str
    ) -> dict[str, Any] | None:
        return self._flow("BREAK", value, params, where, "break")

    def _short_continue(
        self, value: Any, params: dict[str, Any], where: str
    ) -> dict[str, Any] | None:
        return self._flow("CONTINUE", value, params, where, "continue")

    def _flow(
        self,
        flow: str,
        value: Any,
        params: dict[str, Any],
        where: str,
        short: str,
    ) -> dict[str, Any] | None:
        self._only(params, set(), where, short)
        if value is not True:
            self.error(f"{where}: {short} は true で書いてください")
            return None
        block = self._new("controls_flow_statements", where)
        if block is None:
            return None
        self._set_field(block, "FLOW", flow, where)
        return self._finish(block, where)

    def _short_print(
        self, value: Any, params: dict[str, Any], where: str
    ) -> dict[str, Any] | None:
        self._only(params, {"kind", "result"}, where, "print")
        if "kind" in params and "result" in params:
            self.error(f"{where}: print の kind と result は同時に使えません")
            return None
        kind = params.get("kind", "print")
        if "result" in params:
            if not isinstance(params["result"], bool):
                self.error(f"{where}: result は true / false で書いてください")
                return None
            kind = "print2" if params["result"] else "print"
        block = self._new("pokecon_print", where)
        if block is None:
            return None
        self._set_field(block, "KIND", kind, where)
        self._set_input(block, "TEXT", value, where)
        return self._finish(block, where)

    def _short_comment(
        self, value: Any, params: dict[str, Any], where: str
    ) -> dict[str, Any] | None:
        self._only(params, set(), where, "comment")
        text = self._require_str(value, where, "comment")
        if text is None:
            return None
        block = self._new("pokecon_comment", where)
        if block is None:
            return None
        self._set_field(block, "TEXT", text, where)
        return self._finish(block, where)

    def _short_finish(
        self, value: Any, params: dict[str, Any], where: str
    ) -> dict[str, Any] | None:
        self._only(params, set(), where, "finish")
        if value is not True:
            self.error(f"{where}: finish は true で書いてください")
            return None
        block = self._new("pokecon_finish", where)
        return self._finish(block, where) if block is not None else None

    def _short_screenshot(
        self, value: Any, params: dict[str, Any], where: str
    ) -> dict[str, Any] | None:
        self._only(params, set(), where, "screenshot")
        if value is not True:
            self.error(f"{where}: screenshot は true で書いてください")
            return None
        block = self._new("pokecon_screenshot", where)
        return self._finish(block, where) if block is not None else None

    def _short_set(
        self, value: Any, params: dict[str, Any], where: str
    ) -> dict[str, Any] | None:
        self._only(params, {"to"}, where, "set")
        if "to" not in params:
            self.error(f"{where}: set には to を書いてください")
            return None
        block = self._new("variables_set", where)
        if block is None:
            return None
        self._set_field(block, "VAR", value, where)
        self._set_input(block, "VALUE", params["to"], where)
        return self._finish(block, where)

    def _short_change(
        self, value: Any, params: dict[str, Any], where: str
    ) -> dict[str, Any] | None:
        self._only(params, {"by"}, where, "change")
        if "by" not in params:
            self.error(f"{where}: change には by を書いてください")
            return None
        block = self._new("math_change", where)
        if block is None:
            return None
        self._set_field(block, "VAR", value, where)
        self._set_input(block, "DELTA", params["by"], where)
        return self._finish(block, where)

    def _short_call(
        self, value: Any, params: dict[str, Any], where: str
    ) -> dict[str, Any] | None:
        self._only(params, {"args"}, where, "call")
        block = self._new("pokecon_sub_call", where)
        if block is None:
            return None
        name = self._call_name(block, value, where)
        if name is not None:
            args = self._call_args(params.get("args", []), f"{where}.args")
            for index, arg in enumerate(args):
                self._set_input(block, f"ARG{index}", arg, where)
            self.calls.append((self.caller, name, len(args)))
        return self._finish(block, where)

    def _short_call_value(
        self, value: Any, params: dict[str, Any], where: str
    ) -> dict[str, Any] | None:
        self._only(params, {"args"}, where, "call_value")
        block = self._new("pokecon_sub_call_value", where)
        if block is None:
            return None
        name = self._call_name(block, value, where)
        if name is not None:
            args = self._call_args(params.get("args", []), f"{where}.args")
            for index, arg in enumerate(args):
                self._set_input(block, f"ARG{index}", arg, where)
            self.calls.append((self.caller, name, len(args)))
        return self._finish(block, where)

    def _call_name(self, block: dict[str, Any], value: Any, where: str) -> str | None:
        if not isinstance(value, str) or not value.strip():
            self.error(f"{where}: 呼ぶサブルーチン名を書いてください")
            return None
        name = value.strip()
        if not _is_identifier(name):
            self.error(f"{where}: サブルーチン名は識別子にしてください: {name}")
            return None
        block.setdefault("fields", {})["NAME"] = name
        return name

    def _call_args(self, raw: Any, where: str) -> list[Any]:
        if raw is None:
            return []
        if not isinstance(raw, list):
            self.error(f"{where}: args は配列で書いてください")
            return []
        if len(raw) > _MAX_SUB_ARGS:
            self.error(f"{where}: サブルーチンの引数は最大 {_MAX_SUB_ARGS} 個までです")
            return raw[:_MAX_SUB_ARGS]
        return raw

    def _short_discord(
        self, value: Any, params: dict[str, Any], where: str
    ) -> dict[str, Any] | None:
        self._only(params, {"kind"}, where, "discord")
        block = self._new("pokecon_discord", where)
        if block is None:
            return None
        self._set_field(block, "KIND", params.get("kind", "text"), where)
        self._set_input(block, "CONTENT", value, where)
        return self._finish(block, where)

    def _short_dialog_choice(
        self, value: Any, params: dict[str, Any], where: str
    ) -> dict[str, Any] | None:
        return self._dialog(
            "pokecon_dialog_choice", value, params, where, "dialog_choice"
        )

    def _short_dialog_number(
        self, value: Any, params: dict[str, Any], where: str
    ) -> dict[str, Any] | None:
        return self._dialog(
            "pokecon_dialog_number", value, params, where, "dialog_number"
        )

    def _short_dialog_check(
        self, value: Any, params: dict[str, Any], where: str
    ) -> dict[str, Any] | None:
        return self._dialog(
            "pokecon_dialog_check", value, params, where, "dialog_check"
        )

    def _dialog(
        self,
        block_type: str,
        value: Any,
        params: dict[str, Any],
        where: str,
        short: str,
    ) -> dict[str, Any] | None:
        mapping = {
            "pokecon_dialog_choice": {
                "var": "VAR",
                "title": "TITLE",
                "label": "LABEL",
                "options": "OPTIONS",
                "default": "DEFAULT",
            },
            "pokecon_dialog_number": {
                "var": "VAR",
                "title": "TITLE",
                "label": "LABEL",
                "min": "MIN",
                "max": "MAX",
                "default": "DEFAULT",
            },
            "pokecon_dialog_check": {
                "var": "VAR",
                "title": "TITLE",
                "label": "LABEL",
                "default": "DEFAULT",
            },
        }[block_type]
        if isinstance(value, str):
            merged: dict[str, Any] = {"var": value}
        elif isinstance(value, dict):
            merged = dict(value)
        else:
            self.error(f"{where}: {short} は受け変数名かオブジェクトで書いてください")
            return None
        for key, item in params.items():
            if key in merged:
                self.error(f"{where}: {short} の `{key}` が重複しています")
                continue
            merged[key] = item
        self._only(merged, set(mapping), where, short)
        if "options" in merged and isinstance(merged["options"], list):
            raw_options = merged["options"]
            if not raw_options:
                self.error(f"{where}: options を1つ以上書いてください")
            elif not all(isinstance(item, str) for item in raw_options):
                self.error(f"{where}: options は文字列の配列で書いてください")
            else:
                merged["options"] = ",".join(str(item) for item in raw_options)
        block = self._new(block_type, where)
        if block is None:
            return None
        for key, value_item in merged.items():
            field_name = mapping[key]
            self._set_field(block, field_name, value_item, where)
        return self._finish(block, where)

    # -- 小物 ---------------------------------------------------------------

    def _new(self, block_type: str, where: str) -> dict[str, Any] | None:
        if self._block_info(block_type) is None:
            self.error(
                f"{where}: ブロック `{block_type}` がカタログにありません"
                "（assets/blockly を確認してください）"
            )
            return None
        return {"type": block_type}

    def _finish(self, block: dict[str, Any], where: str) -> dict[str, Any]:
        self._check_constraints(block, where)
        if not block.get("fields"):
            block.pop("fields", None)
        if not block.get("inputs"):
            block.pop("inputs", None)
        return block

    def _check_constraints(self, block: dict[str, Any], where: str) -> None:
        info = self._block_info(block["type"])
        if info is None:
            return
        fields = block.get("fields")
        for spec in info.get("fields", []):
            if not isinstance(spec, dict):
                continue
            if spec.get("kind") != "template" or spec.get("serializable") is False:
                continue
            name = str(spec.get("name", "TEMPLATE"))
            value = fields.get(name) if isinstance(fields, dict) else None
            if value is None:
                value = spec.get("default")
            if not isinstance(value, str) or not value.strip():
                self.error(
                    f"{where}: TEMPLATE が空です"
                    f"（ブロック `{block['type']}` に Template/ の画像名を指定してください）"
                )

    def _require_statement(self, block: dict[str, Any], where: str) -> bool:
        info = self._block_info(block["type"])
        if info is None:
            return True
        if not info.get("previous"):
            self.error(f"{where}: ブロック `{block['type']}` は文として置けません")
            return False
        return True

    def _require_value(self, block: dict[str, Any], where: str) -> bool:
        info = self._block_info(block["type"])
        if info is None:
            return True
        if info.get("output") is None:
            self.error(f"{where}: ブロック `{block['type']}` は値として使えません")
            return False
        return True

    def _stick_side(self, value: Any, where: str) -> str | None:
        if not isinstance(value, str):
            self.error(f"{where}: スティックは L / R / L+R で書いてください")
            return None
        mapping = {
            "L": "LEFT",
            "LEFT": "LEFT",
            "R": "RIGHT",
            "RIGHT": "RIGHT",
            "L+R": "BOTH",
            "BOTH": "BOTH",
            "LR": "BOTH",
        }
        result = mapping.get(value.strip().upper())
        if result is None:
            self.error(f"{where}: スティックは L / R / L+R で書いてください: {value!r}")
        return result

    def _var_name(self, value: Any, where: str) -> str | None:
        if not isinstance(value, str) or not value.strip():
            self.error(f"{where}: 変数名を文字列で書いてください")
            return None
        name = value.strip()
        if not _is_identifier(name):
            self.error(f"{where}: 変数名は識別子にしてください: {name}")
            return None
        if name == "self":
            self.error(f"{where}: 変数名に self は使えません")
            return None
        return name

    def _register_var(self, name: str) -> None:
        if name in self._var_ids:
            return
        self._var_ids.add(name)
        self.variables.append({"name": name, "id": name})

    def _tags(self, value: Any) -> list[str]:
        if value is None:
            return ["blockly"]
        if isinstance(value, str):
            raw = re.split(r"[,、]", value)
        elif isinstance(value, list):
            raw = []
            for item in value:
                if not isinstance(item, str):
                    self.error("tags は文字列か文字列の配列で書いてください")
                    continue
                raw.append(item)
        else:
            self.error("tags は文字列か文字列の配列で書いてください")
            return ["blockly"]
        tags: list[str] = []
        for item in raw:
            text = item.strip()
            if text and text not in tags:
                tags.append(text)
        return tags or ["blockly"]

    def _validate_calls(self) -> None:
        for caller, name, argc in self.calls:
            if name not in self.sub_defs:
                self.error(
                    f"未定義のサブルーチンです: self.{name}()（{caller} から）"
                    + _suggest_hint(name, set(self.sub_defs))
                )
                continue
            if name in self._bad_sub_args:
                continue
            expected = len(self.sub_defs[name])
            if argc != expected:
                self.error(
                    f"引数の数が違います: self.{name}()"
                    f"（{caller} から: {argc}個、定義は{expected}個）"
                )
        graph: dict[str, set[str]] = {name: set() for name in self.sub_defs}
        for caller, name, _argc in self.calls:
            if caller in graph and name in graph:
                graph[caller].add(name)
        state: dict[str, int] = {}
        stack: list[str] = []
        reported: set[tuple[str, ...]] = set()

        def visit(node: str) -> None:
            current = state.get(node, 0)
            if current == 2:
                return
            if current == 1:
                if node in stack:
                    cycle = tuple(stack[stack.index(node) :] + [node])
                else:
                    cycle = (node,)
                if cycle not in reported:
                    reported.add(cycle)
                    self.error(
                        "サブルーチンの循環呼び出しがあります: "
                        + " -> ".join(f"self.{item}()" for item in cycle)
                    )
                return
            state[node] = 1
            stack.append(node)
            for nxt in sorted(graph.get(node, set())):
                visit(nxt)
            stack.pop()
            state[node] = 2

        for name in sorted(graph):
            visit(name)

    def _block_info(self, block_type: str) -> dict[str, Any] | None:
        info = self.blocks.get(block_type)
        return info if isinstance(info, dict) else None

    def _field_info(self, block_type: str, name: str) -> dict[str, Any] | None:
        info = self._block_info(block_type)
        if info is None:
            return None
        for spec in info.get("fields", []):
            if isinstance(spec, dict) and spec.get("name") == name:
                return spec
        return None

    def _field_names(self, block_type: str) -> set[str]:
        info = self._block_info(block_type)
        if info is None:
            return set()
        return {
            str(spec.get("name"))
            for spec in info.get("fields", [])
            if isinstance(spec, dict) and spec.get("name")
        }

    def _only(
        self, item: dict[str, Any], allowed: set[str], where: str, label: str
    ) -> None:
        for key in item:
            if key not in allowed:
                self.error(
                    f"{where}: {label} の項目 `{key}` は使えません"
                    + _suggest_hint(str(key), allowed)
                )

    def _require_str(self, value: Any, where: str, label: str) -> str | None:
        if not isinstance(value, str):
            self.error(f"{where}: `{label}` は文字列で書いてください")
            return None
        return value

    def error(self, message: str) -> None:
        self.errors.append(message)
