#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""層の向きの検査。`pokecon/` の中で依存の向きを守る。

規則:
  - domain/** は tkinter / pygubu を import してはならない。
    さらに app / ui を import してはならない（domain は純粋ロジック）。
  - runtime/** は tkinter / pygubu を import してはならない。
    画面部品（pokecon.ui.*）と Window 本体を import してはならない。
  - ui/** は Window 本体（pokecon.app.Window）を import してはならない
    （循環になる）。tkinter・domain・runtime の利用は可。
  - どの層も相対 import を使わない（`SerialController/` を sys.path に
    置く前提の絶対 import に統一する）。

使い方: uv run --frozen python tools/gates/check_core.py（task bounds）
違反があれば一覧を出して非ゼロ終了する。
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PACKAGE = ROOT / "SerialController" / "pokecon"
DOMAIN = PACKAGE / "domain"
RUNTIME = PACKAGE / "runtime"
UI = PACKAGE / "ui"

BANNED_TOP = {"tkinter", "pygubu"}

# domain から見て「外側」の層。
DOMAIN_BANNED_PREFIXES = ("pokecon.app", "pokecon.ui", "Commands")
# runtime から見て「画面」の層。
RUNTIME_BANNED_PREFIXES = ("pokecon.ui", "pokecon.app")
# ui から見て禁止の1点（Window 本体。循環になる）。
UI_BANNED_PREFIXES = ("pokecon.app.Window",)


def _modules(tree: ast.AST) -> list[str]:
    """import されたモジュール名（ドット付きのまま）を返す。"""
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level and node.level > 0:
                names.append("<relative>")
            elif node.module:
                names.append(node.module)
    return names


def _banned(module: str, prefixes: tuple[str, ...]) -> bool:
    return any(module == p or module.startswith(p + ".") for p in prefixes)


def _check_dir(
    base: Path,
    label: str,
    banned_top: set[str],
    banned_prefixes: tuple[str, ...],
) -> tuple[list[str], int]:
    """1フォルダを検査する。(違反一覧, 検査件数) を返す。"""
    violations: list[str] = []
    if not base.is_dir():
        return ([f"{label}/ がありません"], 0)
    files = sorted(base.rglob("*.py"))
    if not files:
        return ([f"{label}/ に .py がありません"], 0)
    for path in files:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError) as e:
            violations.append(f"{path}: 読めません: {e}")
            continue
        rel = path.relative_to(ROOT).as_posix()
        for module in _modules(tree):
            if module == "<relative>":
                violations.append(f"{rel}: 相対 import 禁止（絶対 import を使う）")
                continue
            head = module.split(".")[0]
            if head in banned_top:
                violations.append(f"{rel}: GUI 依存の import 禁止: {module}")
            elif _banned(module, banned_prefixes):
                violations.append(f"{rel}: 層の向き違反（{label} → {module}）")
    return (violations, len(files))


def main() -> int:
    domain_violations, domain_count = _check_dir(
        DOMAIN, "domain", BANNED_TOP, DOMAIN_BANNED_PREFIXES
    )
    runtime_violations, runtime_count = _check_dir(
        RUNTIME, "runtime", BANNED_TOP, RUNTIME_BANNED_PREFIXES
    )
    ui_violations, ui_count = _check_dir(UI, "ui", set(), UI_BANNED_PREFIXES)
    violations = domain_violations + runtime_violations + ui_violations
    if violations:
        print("層の向きの違反:")
        for violation in violations:
            print(f"  {violation}")
        return 1
    print(
        f"境界 OK（domain {domain_count} / runtime {runtime_count} / ui {ui_count} ファイル検査）"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
