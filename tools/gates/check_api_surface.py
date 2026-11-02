#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""公開済み版で約束した名前が今も import できるかの検査。

`core/api_surface.py`（凍結面の一覧）と `core/user_api_allowlist.py`
（凍結 / 暫定の区分）が食い違っていないか、および一覧の名前が
モジュールのトップレベルに残っているかを見る。

モジュールを import しない。AST でトップレベル名を数えるので、
GUI も OpenCV も要らず、副作用なく走る（`Commands.PythonCommandBase`
は import 時に Windows のタイマー分解能を変えるため、実行は避ける）。

使い方: uv run --frozen python tools/gates/check_api_surface.py（task api）
違反があれば一覧を出して非ゼロ終了する。
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONTROLLER = ROOT / "SerialController"
if str(CONTROLLER) not in sys.path:
    sys.path.insert(0, str(CONTROLLER))

from pokecon.domain.api_surface import FROZEN_MODULES  # noqa: E402
from pokecon.domain.user_api_allowlist import (  # noqa: E402
    FROZEN_COMMANDS_SUBS,
    PROVISIONAL_COMMANDS_SUBS,
)


def _bound_names(tree: ast.Module) -> set[str]:
    """モジュール直下で名前を束縛している文から、名前だけを集める。"""
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                continue
            for alias in node.names:
                names.add(alias.asname or alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.asname or alias.name.split(".")[0])
    return names


def main() -> int:
    violations: list[str] = []
    checked = 0

    frozen_modules = set(FROZEN_MODULES)
    from_allowlist = {f"Commands.{name}" for name in FROZEN_COMMANDS_SUBS}
    if frozen_modules != from_allowlist:
        for missing in sorted(from_allowlist - frozen_modules):
            violations.append(
                f"user_api_allowlist.py の凍結経路が api_surface.py にありません: {missing}"
            )
        for extra in sorted(frozen_modules - from_allowlist):
            violations.append(
                f"api_surface.py の経路が user_api_allowlist.py の凍結面にありません: {extra}"
            )

    overlap = frozen_modules & {f"Commands.{n}" for n in PROVISIONAL_COMMANDS_SUBS}
    for name in sorted(overlap):
        violations.append(f"凍結と暫定の両方に載っています: {name}")

    for module, names in FROZEN_MODULES.items():
        path = CONTROLLER / (module.replace(".", "/") + ".py")
        rel = path.relative_to(ROOT).as_posix()
        if not path.is_file():
            violations.append(f"{module}: モジュールがありません（{rel}）")
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
        except (OSError, SyntaxError) as e:
            violations.append(f"{module}: 読めません: {e}")
            continue
        checked += 1
        bound = _bound_names(tree)
        for name in names:
            if name not in bound:
                violations.append(f"{module}: 公開名が消えています: {name}")

    if violations:
        print("公開面（凍結）の違反:")
        for violation in violations:
            print(f"  {violation}")
        return 1
    total = sum(len(names) for names in FROZEN_MODULES.values())
    print(f"公開面（凍結）OK（{checked} モジュール / {total} 名）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
