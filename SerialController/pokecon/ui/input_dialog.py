#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Start 直前に1回だけ出す実行時引数ダイアログ。

常設パネルではない。コマンドクラスが `POKECON_INPUTS` を宣言している
ときだけ、GUI スレッドから `prompt()` を呼ぶ。`PokeConDialogue` を
そのまま使い、閉じるまで待つ（Start は GUI スレッドなので入れ子の
`wait_window` で完結する）。

"""

from __future__ import annotations

import tkinter as tk
from dataclasses import dataclass
from tkinter import messagebox
from typing import Any

from Commands.CommandDialog import PokeConDialogue
from loguru import logger
from pokecon.domain import inputs, option_sets


@dataclass(frozen=True)
class PromptResult:
    """引数ダイアログの結果。

    `values` が入っていれば開始してよい。`canceled` は利用者が Cancel /
    閉じた場合だけ。`error` は検証失敗・ダイアログを開けない等で、取消とは
    区別する（「取り消した」と誤って表示しないため）。
    """

    values: dict[str, Any] | None = None
    canceled: bool = False
    error: str = ""


#: ダイアログを開いているか。Start・試し実行・/run の再入を断る。
_OPEN = False
#: いま表示している Toplevel。`/run/control` の停止で閉じるために持つ。
_TOP: tk.Toplevel | None = None


def is_open() -> bool:
    """引数ダイアログを表示中か。GUI スレッドから読む。"""
    return _OPEN


def cancel() -> None:
    """表示中のダイアログを閉じる（GUI スレッドから呼ぶ）。

    `PokeConDialogue` は触らず、親の Toplevel を壊すだけ。閉じると
    `ret_value` は Cancel と同じ `None` を返す。
    """
    top = _TOP
    if top is None:
        return
    try:
        if top.winfo_exists():
            top.destroy()
    except tk.TclError:
        pass


def _unique_titles(specs: list[inputs.InputSpec]) -> list[str]:
    """widget のラベルは辞書キーなので重複させない。"""
    counts: dict[str, int] = {}
    for spec in specs:
        title = spec.title or spec.key
        counts[title] = counts.get(title, 0) + 1
    used: set[str] = set()
    result: list[str] = []
    for spec in specs:
        title = spec.title or spec.key
        label = title if counts[title] == 1 else f"{title} [{spec.key}]"
        while label in used:
            label = f"{label}_"
        used.add(label)
        result.append(label)
    return result


def _widgets(
    specs: list[inputs.InputSpec],
    app_dir: str,
    initial: dict[str, Any] | None = None,
) -> tuple[list[list[Any]], dict[int, dict[str, Any]]]:
    """`PokeConDialogue` の mode1 仕様と、index→メタ情報を作る。"""
    widgets: list[list[Any]] = []
    meta: dict[int, dict[str, Any]] = {}
    initial = initial or {}
    titles = _unique_titles(specs)
    for index, (spec, title) in enumerate(zip(specs, titles, strict=True)):
        if spec.kind == "choice":
            items = option_sets.list_items(app_dir, spec.set_name, spec.labels)
            entries = [
                (
                    item.value,
                    item.label,
                    str(option_sets.icon_path(app_dir, item.icon)),
                )
                for item in items
            ]
            chosen = initial.get(spec.key, spec.default)
            if not spec.multi and chosen is None and entries:
                chosen = entries[0][0]
            widgets.append(
                [
                    "choice",
                    title,
                    entries,
                    chosen,
                    {"multi": spec.multi, "thumb": 40},
                ]
            )
            meta[index] = {"spec": spec}
        else:
            chosen = initial.get(spec.key, spec.default)
            explicit_unlimited = bool(
                spec.allow_unlimited
                and spec.key in initial
                and initial[spec.key] is None
            )
            unlimited = bool(
                explicit_unlimited
                or (spec.allow_unlimited and spec.default_set and spec.default is None)
            )
            if chosen is None and not unlimited:
                chosen = spec.minimum if spec.minimum is not None else 0
            widgets.append(
                [
                    "int_limit",
                    title,
                    "" if chosen is None else str(chosen),
                    {
                        "min": spec.minimum,
                        "max": spec.maximum,
                        "allow_unlimited": spec.allow_unlimited,
                        "unlimited": unlimited,
                    },
                ]
            )
            meta[index] = {"spec": spec}
    return widgets, meta


def _coerce(
    raw_values: list[Any], meta: dict[int, dict[str, Any]], app_dir: str
) -> tuple[dict[str, Any] | None, str | None]:
    """ダイアログの戻り値を引数の辞書にする。"""
    values: dict[str, Any] = {}
    for index, raw in enumerate(raw_values):
        info = meta.get(index)
        if info is None:
            continue
        spec: inputs.InputSpec = info["spec"]
        try:
            values[spec.key] = inputs.coerce_value(spec, raw, app_dir)
        except inputs.InputError as exc:
            return None, f"{spec.title or spec.key}: {exc}"
    return values, None


def prompt(
    root: Any,
    raw_spec: Any,
    app_dir: str,
    *,
    initial: dict[str, Any] | None = None,
) -> PromptResult:
    """引数ダイアログを1回開く。結果は `PromptResult`。

    GUI スレッドから呼ぶこと。Cancel と検証失敗を区別して返す。
    """
    global _OPEN, _TOP
    if root is None:
        return PromptResult(
            error="引数ダイアログを開けません（本体の画面がありません）"
        )
    if _OPEN:
        return PromptResult(
            error="引数ダイアログは既に開いています（先に閉じてください）"
        )
    specs, errors = inputs.parse_spec(raw_spec, app_dir)
    if errors:
        messagebox.showerror("実行時の引数", "\n".join(errors), parent=root)
        return PromptResult(error="引数の宣言が不正です: " + "; ".join(errors))
    if not specs:
        return PromptResult(values={})

    _OPEN = True
    top: tk.Toplevel | None = None
    try:
        top = tk.Toplevel(root)
        _TOP = top
        widgets, meta = _widgets(specs, app_dir, initial)
        dialogue = PokeConDialogue(top, "実行時の引数", widgets, mode=1)
        raw_values = dialogue.ret_value(list)
        if raw_values is None:
            return PromptResult(canceled=True)
        values, error = _coerce(list(raw_values), meta, app_dir)
        if error is not None:
            messagebox.showerror("実行時の引数", error, parent=root)
            return PromptResult(error="引数の値が不正です: " + error)
        return PromptResult(values=values)
    except tk.TclError as exc:
        logger.warning(f"引数ダイアログを開けませんでした: {exc}")
        return PromptResult(
            error="引数ダイアログを開けませんでした（本体の画面を確認してください）"
        )
    finally:
        _OPEN = False
        _TOP = None
        if top is not None:
            try:
                if top.winfo_exists():
                    top.destroy()
            except tk.TclError:
                pass
