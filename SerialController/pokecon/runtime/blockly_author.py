#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""コンパクト仕様から Blockly の保存物を作る（GUI非依存・再公開ファサード）。

実体は `pokecon.runtime.blockly` 配下へ機能別に分割されている。
"""

from __future__ import annotations

from pokecon.runtime.blockly import (
    BuildResult as BuildResult,
    CheckResult as CheckResult,
    PatchResult as PatchResult,
    RenderResult as RenderResult,
    build_workspace as build_workspace,
    catalog_markdown as catalog_markdown,
    check_spec as check_spec,
    check_workspace as check_workspace,
    patch_workspace as patch_workspace,
    render_workspace as render_workspace,
    save_spec as save_spec,
    save_workspace as save_workspace,
    workspace_digest as workspace_digest,
)

__all__ = [
    "BuildResult",
    "CheckResult",
    "PatchResult",
    "RenderResult",
    "build_workspace",
    "catalog_markdown",
    "check_spec",
    "check_workspace",
    "patch_workspace",
    "render_workspace",
    "save_spec",
    "save_workspace",
    "workspace_digest",
]
