#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""実行時引数（`POKECON_INPUTS`）の宣言・検証・型変換。

GUI に依存しない。Blockly の生成コードと手書きコマンドの両方が同じ
宣言を使う。値の決定は「answers → ダイアログ → 既定値」の順で、
どこで決まったかを `sources` に残す。

用語:
  spec     利用者が宣言した引数の一覧（`POKECON_INPUTS`）。
  answers  AI / API が渡した値。key で照合する。
  policy   `dialog` / `default` / `fail`。実行前の決め方。
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from typing import Any

from pokecon.domain import option_sets

#: 引数の key。生成コードの属性名としても使える形に限る。
KEY_RE = re.compile(r"[a-z][a-z0-9_]{0,31}")

#: 受け付ける種別。
KINDS = frozenset({"choice", "int"})

#: ポリシー。
POLICIES = frozenset({"dialog", "default", "fail"})

#: 複数選択の既定値で「全部」を表す。
ALL = "*"

_INT_TEXT_RE = re.compile(r"[+-]?\d+")


class InputError(ValueError):
    """宣言・答えの不備。利用者向けの文言を持つ。"""


@dataclass(frozen=True)
class InputSpec:
    """1つの引数。choice の項目は `app_dir` から都度引く。"""

    key: str
    kind: str
    title: str = ""
    set_name: str = ""
    multi: bool = False
    default: Any = None
    #: `default` が宣言に書かれているか（None を「無制限」の既定値として使うため）。
    default_set: bool = False
    #: int で「上限なし」（None）を許すか。
    allow_unlimited: bool = False
    minimum: int | None = None
    maximum: int | None = None
    labels: dict[str, str] = field(default_factory=dict)


@dataclass
class InputResolution:
    """解決の結果。`errors` が空でなければ開始しない。"""

    values: dict[str, Any] = field(default_factory=dict)
    sources: dict[str, str] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    missing: list[dict[str, Any]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors and not self.missing


def _as_spec_list(raw: Any) -> list[Any] | None:
    """list か `{"inputs": [...]}` のどちらでも受ける。"""
    if isinstance(raw, dict):
        raw = raw.get("inputs")
    if raw is None:
        return []
    if not isinstance(raw, list):
        return None
    return raw


def parse_spec(
    raw: Any, app_dir: str | None = None
) -> tuple[list[InputSpec], list[str]]:
    """宣言を読み、`InputSpec` の一覧と異常を返す。

    `app_dir` を渡すとセットの実在・値の形式まで見る。None なら形だけ。
    """
    entries = _as_spec_list(raw)
    if entries is None:
        return [], ["`POKECON_INPUTS` は一覧で書いてください"]
    specs: list[InputSpec] = []
    errors: list[str] = []
    seen: set[str] = set()
    for index, entry in enumerate(entries):
        where = f"引数{index + 1}"
        if not isinstance(entry, dict):
            errors.append(f"{where}: 設定はオブジェクトで書いてください")
            continue
        key = entry.get("key")
        if not isinstance(key, str) or KEY_RE.fullmatch(key) is None:
            errors.append(
                f"{where}: key は英小文字で始まる英数字と `_`（32文字まで）にしてください"
            )
            continue
        if key in seen:
            errors.append(f"key が重複しています: {key}")
            continue
        seen.add(key)
        kind = entry.get("kind")
        if kind not in KINDS:
            errors.append(f"{key}: kind は choice か int にしてください")
            continue
        title = entry.get("title", key)
        if not isinstance(title, str) or not title.strip():
            title = key
        if kind == "choice":
            spec, spec_errors = _parse_choice(key, title, entry, app_dir)
        else:
            spec, spec_errors = _parse_int(key, title, entry)
        errors.extend(spec_errors)
        if spec is not None:
            specs.append(spec)
    return specs, errors


def _parse_choice(
    key: str, title: str, entry: dict[str, Any], app_dir: str | None
) -> tuple[InputSpec | None, list[str]]:
    errors: list[str] = []
    set_name = option_sets.normalize_set_name(entry.get("set"))
    if set_name is None:
        return None, [f"{key}: set は `Template/` からの相対で書いてください"]
    multi = entry.get("multi", False)
    if not isinstance(multi, bool):
        errors.append(f"{key}: multi は true/false にしてください")
        multi = False
    labels_raw = entry.get("labels", {})
    labels: dict[str, str] = {}
    if not isinstance(labels_raw, dict):
        errors.append(f"{key}: labels はオブジェクトで書いてください")
    else:
        for label_key, label_value in labels_raw.items():
            if not isinstance(label_key, str) or not isinstance(label_value, str):
                errors.append(f"{key}: labels の名前と表示名は文字にしてください")
                continue
            labels[label_key] = label_value
    items: list[str] | None = None
    if app_dir is not None:
        try:
            items = [item.value for item in option_sets.list_items(app_dir, set_name)]
        except option_sets.OptionSetError as exc:
            errors.append(f"{key}: {exc}")
        else:
            unknown = sorted(set(labels) - set(items))
            if unknown:
                errors.append(
                    f"{key}: labels にセットにない値があります: {', '.join(unknown)}"
                )
    default = entry.get("default")
    if default is not None and app_dir is not None:
        try:
            default = coerce_value(
                InputSpec(key=key, kind="choice", set_name=set_name, multi=multi),
                default,
                app_dir,
                allow_all=True,
            )
        except InputError as exc:
            errors.append(f"{key}: 既定値: {exc}")
    if errors:
        return None, errors
    return (
        InputSpec(
            key=key,
            kind="choice",
            title=title,
            set_name=set_name,
            multi=multi,
            default=default,
            default_set=entry.get("default") is not None,
            labels=labels,
        ),
        [],
    )


def _parse_int(
    key: str, title: str, entry: dict[str, Any]
) -> tuple[InputSpec | None, list[str]]:
    errors: list[str] = []
    minimum = entry.get("min")
    maximum = entry.get("max")
    if not isinstance(minimum, int) or isinstance(minimum, bool):
        errors.append(f"{key}: min は整数で書いてください")
    if not isinstance(maximum, int) or isinstance(maximum, bool):
        errors.append(f"{key}: max は整数で書いてください")
    allow_unlimited = entry.get("allow_unlimited", False)
    if not isinstance(allow_unlimited, bool):
        errors.append(f"{key}: allow_unlimited は true/false にしてください")
        allow_unlimited = False
    if errors:
        return None, errors
    assert isinstance(minimum, int) and isinstance(maximum, int)
    if minimum > maximum:
        return None, [f"{key}: min が max を超えています"]
    default_set = "default" in entry
    default = entry.get("default")
    if default_set:
        if default is None:
            if not allow_unlimited:
                return None, [f"{key}: 既定値を空にするには allow_unlimited が必要です"]
        elif not isinstance(default, int) or isinstance(default, bool):
            return None, [f"{key}: 既定値は整数で書いてください"]
        elif not minimum <= default <= maximum:
            return None, [
                f"{key}: 既定値が範囲外です（{minimum}〜{maximum}）: {default}"
            ]
    return (
        InputSpec(
            key=key,
            kind="int",
            title=title,
            minimum=minimum,
            maximum=maximum,
            default=default,
            default_set=default_set,
            allow_unlimited=allow_unlimited,
        ),
        [],
    )


def validate_spec(raw: Any, app_dir: str | None = None) -> list[str]:
    """保存前・受付前・Start 前の共通検査。異常の一覧を返す。"""
    _, errors = parse_spec(raw, app_dir)
    return errors


def _values_for(spec: InputSpec, app_dir: str | None) -> list[str]:
    if app_dir is None:
        return []
    try:
        return [item.value for item in option_sets.list_items(app_dir, spec.set_name)]
    except option_sets.OptionSetError as exc:
        raise InputError(str(exc)) from exc


def coerce_value(
    spec: InputSpec,
    value: Any,
    app_dir: str | None,
    *,
    allow_all: bool = False,
) -> Any:
    """1つの値を宣言に合わせて変換する。合わなければ `InputError`。"""
    if spec.kind == "int":
        if value is None or (isinstance(value, str) and not value.strip()):
            if spec.allow_unlimited:
                return None
            raise InputError("整数を指定してください")
        if isinstance(value, bool):
            raise InputError("整数を指定してください")
        if isinstance(value, int):
            number = value
        elif isinstance(value, str) and _INT_TEXT_RE.fullmatch(value.strip()):
            number = int(value.strip())
        else:
            raise InputError("整数を指定してください")
        minimum = spec.minimum if spec.minimum is not None else -(2**31)
        maximum = spec.maximum if spec.maximum is not None else 2**31
        if not minimum <= number <= maximum:
            raise InputError(f"範囲外です（{minimum}〜{maximum}）: {number}")
        return number

    if spec.kind != "choice":
        raise InputError(f"知らない種別です: {spec.kind}")

    items = _values_for(spec, app_dir)
    if spec.multi:
        if isinstance(value, str):
            text = value.strip()
            if allow_all and text == ALL:
                return list(items)
            raw_values: list[Any] = [part.strip() for part in text.split(",")]
        elif isinstance(value, (list, tuple)):
            raw_values = list(value)
        else:
            raise InputError("一覧（文字の配列）で指定してください")
        chosen: list[str] = []
        for item in raw_values:
            if not isinstance(item, str):
                raise InputError("選択肢は文字で指定してください")
            text = item.strip()
            if allow_all and text == ALL:
                return list(items)
            if not text:
                continue
            if text not in items:
                raise InputError(f"選択肢にない値です: {text}")
            if text not in chosen:
                chosen.append(text)
        if not chosen:
            raise InputError("1つ以上選んでください")
        # セットの並び順に揃える（同じ集合なら常に同じ結果にする）。
        return [item for item in items if item in chosen]

    if not isinstance(value, str):
        raise InputError("選択肢は文字で指定してください")
    text = value.strip()
    if allow_all and text == ALL:
        if not items:
            raise InputError("セットに画像がありません")
        return items[0]
    if text not in items:
        raise InputError(f"選択肢にない値です: {text}")
    return text


def _missing_payload(spec: InputSpec, app_dir: str | None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "key": spec.key,
        "kind": spec.kind,
        "title": spec.title,
    }
    if spec.kind == "choice":
        payload.update(
            {
                "multi": spec.multi,
                "set": spec.set_name,
                "options": _values_for(spec, app_dir),
                "labels": dict(spec.labels),
                "default": spec.default,
            }
        )
    else:
        payload.update(
            {
                "min": spec.minimum,
                "max": spec.maximum,
                "default": spec.default,
                "defaultSet": spec.default_set,
                "allowUnlimited": spec.allow_unlimited,
            }
        )
    return payload


def resolve(
    specs: list[InputSpec],
    answers: Any,
    policy: str,
    app_dir: str | None,
    *,
    dialog_values: dict[str, Any] | None = None,
) -> InputResolution:
    """answers・ダイアログ値・既定値の順で値を決める。

    `dialog` ポリシーで `dialog_values` が無い key は `missing` に残る。
    呼び出し側はそれを UI に出し、結果を `dialog_values` に入れて再度呼ぶ。
    """
    result = InputResolution()
    if policy not in POLICIES:
        result.errors.append(f"知らない inputPolicy です: {policy}")
        return result
    if answers is None:
        answers = {}
    if not isinstance(answers, dict):
        result.errors.append("answers はオブジェクト（key → 値）で書いてください")
        return result
    by_key = {spec.key: spec for spec in specs}
    for key in answers:
        if key not in by_key:
            result.errors.append(f"宣言にない引数です: {key}")
    dialog_values = dialog_values or {}
    for key in dialog_values:
        if key not in by_key:
            result.errors.append(f"ダイアログにない引数です: {key}")

    for spec in specs:
        if spec.key in answers:
            try:
                result.values[spec.key] = coerce_value(spec, answers[spec.key], app_dir)
            except InputError as exc:
                result.errors.append(f"{spec.key}: {exc}")
                continue
            result.sources[spec.key] = "answer"
            continue
        if spec.key in dialog_values:
            try:
                result.values[spec.key] = coerce_value(
                    spec, dialog_values[spec.key], app_dir
                )
            except InputError as exc:
                result.errors.append(f"{spec.key}: {exc}")
                continue
            result.sources[spec.key] = "dialog"
            continue
        if policy == "default" and spec.default_set:
            try:
                result.values[spec.key] = coerce_value(
                    spec, spec.default, app_dir, allow_all=True
                )
            except InputError as exc:
                result.errors.append(f"{spec.key}: 既定値: {exc}")
                continue
            result.sources[spec.key] = "default"
            continue
        result.missing.append(_missing_payload(spec, app_dir))
    return result


def raw_spec_from_code(code: str) -> tuple[Any, list[str]]:
    """生成コードのクラス属性 `POKECON_INPUTS` をリテラルとして取り出す。

    無ければ `(None, [])`。リテラルでなければ異常。
    """
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return None, [f"生成コードの文法が壊れています（{exc}）"]
    found: list[ast.expr] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for item in node.body:
            if isinstance(item, ast.Assign):
                targets = item.targets
                value: ast.expr | None = item.value
            elif isinstance(item, ast.AnnAssign):
                targets = [item.target]
                value = item.value
            else:
                continue
            if value is None:
                continue
            for target in targets:
                if isinstance(target, ast.Name) and target.id == "POKECON_INPUTS":
                    found.append(value)
    if not found:
        return None, []
    if len(found) > 1:
        return None, ["`POKECON_INPUTS` は1か所だけにしてください"]
    try:
        return ast.literal_eval(found[0]), []
    except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError) as exc:
        return None, [f"`POKECON_INPUTS` はリテラルで書いてください: {exc}"]


def spec_from_code(
    code: str, app_dir: str | None = None
) -> tuple[list[InputSpec] | None, list[str]]:
    """生成コードの `POKECON_INPUTS` を読んで `InputSpec` にする。"""
    raw, errors = raw_spec_from_code(code)
    if errors:
        return None, errors
    if raw is None:
        return [], []
    return parse_spec(raw, app_dir)


def input_keys_in_code(code: str) -> list[str]:
    """生成コード内の `self.input_value("key")` の key を集める。"""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return []
    keys: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr != "input_value":
            continue
        if not node.args:
            continue
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            if first.value not in keys:
                keys.append(first.value)
    return keys
