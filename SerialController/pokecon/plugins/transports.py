#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""transport プラグインの読み込み（manifest 方式）。

置き方:

    <plugin_dir>/
      switch-pro-cont/
        manifest.toml
        __init__.py   # def register(register_transport): ...

manifest.toml の必須キー:

    name            = "switch-pro-cont"   # 登録名の接頭辞。表示と重複回避に使う
    version         = "1.2.0"
    api_version     = 1                   # この読み込み口の版。合わなければ読まない
    min_app_version = "4.0.0"             # これ未満のアプリでは読まない

なぜ manifest か: 素の .py を走査する形だと、版の整合も読み込み順も
保証できず、1本の壊れたファイルが何をしたのか分からない。manifest を
要求すると「読まなかった理由」を利用者に見せられる（Blender の
blender_manifest.toml と同じ考え方）。

1本の失敗は他へ波及させない。理由は必ず返し、呼び出し側がログと画面へ出す。
"""

from __future__ import annotations

import importlib.util
import sys
import tomllib
import traceback
from pathlib import Path

from loguru import logger
from pokecon import __version__
from pokecon.domain.transport.registry import _REGISTRY, register_transport
from pokecon.domain.version import at_least, parse_version

MANIFEST_NAME = "manifest.toml"

#: この読み込み口の版。plugin 側の api_version と一致するものだけ読む。
#: 破壊的変更をするときは上げ、旧版の plugin は理由つきで読まない。
API_VERSION = 1

REQUIRED_KEYS = ("name", "version", "api_version", "min_app_version")


def _parse_manifest(path: Path) -> dict[str, object] | None:
    try:
        with open(path, "rb") as file:
            data = tomllib.load(file)
    except (OSError, tomllib.TOMLDecodeError) as e:
        logger.warning(f"{path}: manifest を読めません: {e}")
        return None
    missing = [key for key in REQUIRED_KEYS if key not in data]
    if missing:
        logger.warning(f"{path}: manifest に必須キーがありません: {missing}")
        return None
    return data


def _rejection_reason(data: dict[str, object]) -> str | None:
    """読まない理由を返す。読んでよければ None。"""
    api_version = data.get("api_version")
    if api_version != API_VERSION:
        return (
            f"api_version={api_version!r} はこの版の受け口（{API_VERSION}）と合いません"
        )
    min_app_version = str(data.get("min_app_version", ""))
    if parse_version(min_app_version) is None:
        return f"min_app_version={min_app_version!r} は X.Y.Z で書いてください"
    if not at_least(min_app_version, __version__):
        return (
            f"min_app_version={min_app_version} は今のアプリ（{__version__}）より新しい"
        )
    return None


def _import_plugin(directory: Path, module_name: str) -> object | None:
    init_file = directory / "__init__.py"
    if not init_file.is_file():
        logger.warning(f"{directory}: __init__.py がありません")
        return None
    spec = importlib.util.spec_from_file_location(module_name, init_file)
    if spec is None or spec.loader is None:
        logger.warning(f"{init_file}: 読み込めません")
        return None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(module_name, None)
        logger.error(f"{init_file}: import に失敗しました\n{traceback.format_exc()}")
        return None
    return module


def load_dir(dir_path: str) -> tuple[list[str], list[str]]:
    """フォルダ配下の transport プラグインを読む。

    戻り値は (登録された名前, 読まなかった理由)。理由は空でない限り
    呼び出し側がログと画面へ出す（利用者の次の行動が変わるため）。
    """
    added: list[str] = []
    rejected: list[str] = []
    if not dir_path:
        return added, rejected
    root = Path(dir_path)
    if not root.is_dir():
        return added, rejected
    for directory in sorted(p for p in root.iterdir() if p.is_dir()):
        manifest = directory / MANIFEST_NAME
        if not manifest.is_file():
            rejected.append(f"{directory.name}: {MANIFEST_NAME} がありません")
            continue
        data = _parse_manifest(manifest)
        if data is None:
            rejected.append(f"{directory.name}: manifest が不正です")
            continue
        reason = _rejection_reason(data)
        if reason is not None:
            rejected.append(f"{directory.name}: {reason}")
            continue
        module = _import_plugin(directory, f"pokecon_plugin_{directory.name}")
        if module is None:
            rejected.append(f"{directory.name}: import に失敗しました")
            continue
        hook = getattr(module, "register", None)
        if not callable(hook):
            rejected.append(
                f"{directory.name}: register(register_transport) がありません"
            )
            continue
        before = set(_REGISTRY)
        try:
            hook(register_transport)
        except Exception:
            logger.error(
                f"{directory.name}: register に失敗しました\n{traceback.format_exc()}"
            )
            rejected.append(f"{directory.name}: register に失敗しました")
            continue
        names = sorted(set(_REGISTRY) - before)
        if not names:
            rejected.append(f"{directory.name}: 何も登録しませんでした")
        added.extend(names)
    return added, rejected
