#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`X.Y.Z` 表記の版の読み取り（GUI非依存）。

スクリプトのパック配布と、プラグインの manifest が同じ規則で
「このアプリの版で動くか」を判定するために、解釈をここ1か所に置く。
"""

from __future__ import annotations


def parse_version(value: str) -> tuple[int, int, int] | None:
    """`X.Y.Z` の先頭を数値3つで返す。読めなければ None。"""
    text = value.strip().lstrip("vV")
    head = text.split()[0] if text.split() else ""
    head = head.split("-")[0].split("+")[0]
    parts = head.split(".")
    if len(parts) != 3:
        return None
    try:
        nums = (int(parts[0]), int(parts[1]), int(parts[2]))
    except ValueError:
        return None
    if any(n < 0 or n > 999 for n in nums):
        return None
    return nums


def at_least(minimum: str, current: str) -> bool:
    """`current` が `minimum` 以上か。どちらか読めなければ False。"""
    need = parse_version(minimum)
    have = parse_version(current)
    if need is None or have is None:
        return False
    return have >= need
