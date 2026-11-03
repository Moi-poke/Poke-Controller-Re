#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""選択肢セット（`Template/<pkg>/<set>/` 直下の画像）とアイコンの解決。

Blockly の実行時引数は、フォルダを1つの選択肢セットとして扱う。
値は拡張子を除いたファイル名（`^[A-Za-z0-9_\\-]{1,64}$`）に限る。
表示用アイコンは `Template/<set>/icons/<値>.png` があればそれを使い、
無ければテンプレート画像を縮小して使う。アイコンは認識には使わない。

GUI に依存しない。実在しない値・セットは `OptionSetError`（回復可能）に
する。保存前の検査はこの例外を捕まえて文字列にする。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from pokecon.domain.recovery import RecoverableError

TEMPLATE_DIR_REL = "Template"

#: 値として認めるファイル名（拡張子を除く）。
VALUE_RE = re.compile(r"[A-Za-z0-9_\-]{1,64}")

#: セットに入れられる画像の拡張子。
IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".bmp"})

#: 表示専用アイコンの置き場（セット直下）。
ICONS_DIR = "icons"


class OptionSetError(RecoverableError):
    """選択肢セットの不備。保存前にも実行時にも使う。"""

    title = "選択肢セットの画像を解決できません"

    def __init__(self, message: str = "", *, hint: str = "") -> None:
        super().__init__(message, hint=hint)


@dataclass(frozen=True)
class OptionItem:
    """1つの選択肢。パスはすべて `Template/` からの相対（posix）。"""

    value: str
    label: str
    icon: str
    template: str


def _template_dir(app_dir: str | Path) -> Path:
    return (Path(app_dir) / TEMPLATE_DIR_REL).resolve()


def normalize_set_name(name: object) -> str | None:
    """`pkg/set` を posix に正規化する。不正なら None。"""
    if not isinstance(name, str):
        return None
    probe = name.replace("\\", "/").strip()
    if not probe or probe.startswith("/") or ":" in probe:
        return None
    parts = [part for part in PurePosixPath(probe).parts if part not in ("", ".")]
    if not parts or any(part == ".." for part in parts):
        return None
    return "/".join(parts)


def template_path(app_dir: str | Path, rel: str) -> Path:
    """`Template/` からの相対パスを実パスにする。外へ出る指定は拒否する。"""
    base = _template_dir(app_dir)
    target = (base / PurePosixPath(rel)).resolve()
    try:
        target.relative_to(base)
    except ValueError as exc:
        raise OptionSetError(
            f"`Template/` の外を指しています: {rel}",
            hint="`Template/` からの相対で指定してください。",
        ) from exc
    return target


def set_dir(app_dir: str | Path, set_name: object) -> Path:
    """セットの実フォルダを返す。不正・不在なら `OptionSetError`。"""
    normalized = normalize_set_name(set_name)
    if normalized is None:
        raise OptionSetError(
            f"セット名が不正です: {set_name!r}",
            hint="`<パッケージ>/<セット>` の形で指定してください。",
        )
    path = template_path(app_dir, normalized)
    if not path.is_dir():
        raise OptionSetError(
            f"セットがありません: {normalized}",
            hint=f"`{TEMPLATE_DIR_REL}/{normalized}/` に画像を置いてください。",
        )
    return path


def _direct_images(directory: Path) -> list[Path]:
    """直下の画像だけを名前順で返す（サブフォルダは見ない）。"""
    try:
        entries = list(directory.iterdir())
    except OSError as exc:
        raise OptionSetError(f"セットを読めません: {directory.name}: {exc}") from exc
    images = [
        path
        for path in entries
        if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES
    ]
    images.sort(key=lambda path: path.name.casefold())
    return images


def _icon_for(base: Path, set_path: Path, value: str, default: str) -> str:
    """表示アイコンの `Template/` 相対パス。無ければテンプレートを使う。"""
    icons = set_path / ICONS_DIR
    if icons.is_dir():
        matches = [
            path
            for path in icons.iterdir()
            if path.is_file()
            and path.suffix.lower() in IMAGE_SUFFIXES
            and path.stem == value
        ]
        # .png を優先し、それ以外は名前順。複数あっても表示用なので寛容にする。
        matches.sort(key=lambda path: (path.suffix.lower() != ".png", path.name))
        if matches:
            return matches[0].relative_to(base).as_posix()
    return default


def _relative_template(base: Path, path: Path) -> str:
    """`Template/` から見た相対パス（posix）を返す。"""
    try:
        return path.relative_to(base).as_posix()
    except ValueError as exc:
        raise OptionSetError(
            f"`{TEMPLATE_DIR_REL}/` の外を指しています: {path}"
        ) from exc


def list_items(
    app_dir: str | Path,
    set_name: object,
    labels: dict[str, str] | None = None,
) -> list[OptionItem]:
    """セットの項目を返す。値の重複・不正なファイル名はエラー。"""
    normalized = normalize_set_name(set_name)
    root = set_dir(app_dir, normalized)
    base = _template_dir(app_dir)
    files = _direct_images(root)
    if not files:
        raise OptionSetError(
            f"セットに画像がありません: {normalized}",
            hint=f"`{TEMPLATE_DIR_REL}/{normalized}/` に png/jpg を置いてください。",
        )
    seen: dict[str, str] = {}
    items: list[OptionItem] = []
    for path in files:
        value = path.stem
        if VALUE_RE.fullmatch(value) is None:
            raise OptionSetError(
                f"選択肢の名前に使えない文字があります: {path.name}",
                hint="英字・数字・`_`・`-`（64文字まで）にしてください。",
            )
        folded = value.casefold()
        if folded in seen:
            raise OptionSetError(
                f"選択肢の値が重複しています: {seen[folded]} と {path.name}",
                hint="Windows では大文字小文字だけの違いも同じ名前として扱われます。",
            )
        seen[folded] = path.name
        template = _relative_template(base, path)
        icon = _icon_for(base, root, value, template)
        label = value
        if labels and isinstance(labels.get(value), str):
            label = str(labels[value])
        items.append(OptionItem(value=value, label=label, icon=icon, template=template))
    return items


def item_values(app_dir: str | Path, set_name: object) -> list[str]:
    """セットの値だけを返す（並びはセットの順）。"""
    return [item.value for item in list_items(app_dir, set_name)]


def option_template(app_dir: str | Path, set_name: object, value: object) -> str:
    """値に対応するテンプレートの `Template/` 相対パスを返す。

    実行時（`self.option_template`）から呼ばれる。セットに無い値は
    `OptionSetError`（回復可能）にする。
    """
    text = str(value).strip() if isinstance(value, (str, int)) else ""
    for item in list_items(app_dir, set_name):
        if item.value == text:
            return item.template
    raise OptionSetError(
        f"セットにない値です: {set_name} / {value!r}",
        hint="選択肢の画像名（拡張子を除く）と一致させてください。",
    )


def icon_path(app_dir: str | Path, rel: str) -> Path:
    """`Template/` 相対のアイコンパスを実パスにする。"""
    return template_path(app_dir, rel)
