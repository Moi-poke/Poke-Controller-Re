"""Blockly workspace の検査・コード生成・保存・カタログ整形。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
from typing import Any

from pokecon.domain import blockly_headless, blockly_validate
from pokecon.runtime import blockly_match, blockly_save, blockly_templates
from pokecon.runtime.blockly.builder import build_workspace
from pokecon.runtime.blockly.models import (
    CheckResult,
    RenderResult,
    _catalog_blocks,
    _dedupe,
    _iter_blocks,
    _spec_stem,
    _suggest_hint,
    _workspace_object,
)


def check_spec(spec: Any, app_dir: str | Path) -> CheckResult:
    """仕様を組み立て、保存前検査を順に通す。例外は投げない。"""
    try:
        return _check_spec(spec, app_dir)
    except Exception as e:
        return CheckResult(False, errors=[f"検査できませんでした（{e}）"])


def check_workspace(workspace_json: Any, app_dir: str | Path) -> CheckResult:
    """既存 workspace JSON を、保存前検査と同じ経路で検査する。

    ブロックID・配置・コメント・変数はそのまま保持し、生成コードの検査だけを
    共通化する。仕様（コンパクトJSON）からの再構築はしない。
    """
    try:
        return _check_workspace(workspace_json, app_dir)
    except Exception as e:
        return CheckResult(False, errors=[f"検査できませんでした（{e}）"])


def render_workspace(workspace_json: Any, app_dir: str | Path) -> RenderResult:
    """workspace JSON から Python を生成する。例外は投げない。"""
    try:
        return _render_workspace(workspace_json, app_dir)
    except Exception as e:
        return RenderResult(False, errors=[f"生成できませんでした（{e}）"])


def save_spec(
    spec: Any, app_dir: str | Path, stem: str | None = None
) -> blockly_save.SaveResult:
    """検査が通った仕様を `.py` / `.blockly.json` の対として保存する。"""
    try:
        resolved = stem if stem is not None else _spec_stem(spec)
        if not isinstance(resolved, str) or not resolved.strip():
            message = "保存名（stem）を書いてください"
            return blockly_save.SaveResult(
                status="failed", message=f"保存できません: {message}", errors=[message]
            )
        stem_errors = blockly_validate.validate_stem(resolved)
        if stem_errors:
            return blockly_save.SaveResult(
                status="failed",
                message="保存できません:\n- " + "\n- ".join(stem_errors),
                errors=stem_errors,
            )
        checked = check_spec(spec, app_dir)
        if not checked.ok or checked.workspace is None:
            errors = checked.errors or ["検査に通りませんでした"]
            return blockly_save.SaveResult(
                status="failed",
                message="保存できません:\n- " + "\n- ".join(errors),
                errors=errors,
                warnings=checked.warnings,
            )
        workspace_json = json.dumps(checked.workspace, ensure_ascii=False)
        return blockly_save.save_blockly(
            app_dir, resolved, workspace_json, checked.code
        )
    except Exception as e:
        return blockly_save.SaveResult(
            status="failed",
            message=f"保存できません: {e}",
            errors=[str(e)],
        )


def save_workspace(
    workspace_json: Any, app_dir: str | Path, stem: str
) -> blockly_save.SaveResult:
    """検査が通った workspace JSON を、そのままの構造で対として保存する。

    コンパクト仕様からの再構築はしない（ブロックID・配置・コメント・変数・
    extraState を保持する）。`pokecon.pySha256` は既存の保存層が更新する。
    """
    try:
        if not isinstance(stem, str) or not stem.strip():
            message = "保存名（stem）を書いてください"
            return blockly_save.SaveResult(
                status="failed", message=f"保存できません: {message}", errors=[message]
            )
        stem_errors = blockly_validate.validate_stem(stem)
        if stem_errors:
            return blockly_save.SaveResult(
                status="failed",
                message="保存できません:\n- " + "\n- ".join(stem_errors),
                errors=stem_errors,
            )
        checked = check_workspace(workspace_json, app_dir)
        if not checked.ok or checked.workspace is None:
            errors = checked.errors or ["検査に通りませんでした"]
            return blockly_save.SaveResult(
                status="failed",
                message="保存できません:\n- " + "\n- ".join(errors),
                errors=errors,
                warnings=checked.warnings,
            )
        text = json.dumps(checked.workspace, ensure_ascii=False)
        return blockly_save.save_blockly(app_dir, stem, text, checked.code)
    except Exception as e:
        return blockly_save.SaveResult(
            status="failed",
            message=f"保存できません: {e}",
            errors=[str(e)],
        )


def workspace_digest(workspace_json: Any) -> str:
    """workspace の正規化した要約値（`pokecon` 記録は除く）。

    同じ構造なら JSON の空白・キー順・改行に左右されない。保存物の版確認に使う。
    おかしければ ValueError。
    """
    workspace, errors = _workspace_object(workspace_json)
    if errors or workspace is None:
        raise ValueError(errors[0] if errors else "workspace を読めません")
    data = {key: value for key, value in workspace.items() if key != "pokecon"}
    try:
        canonical = json.dumps(
            data, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
    except (TypeError, ValueError, RecursionError) as e:
        raise ValueError(f"workspace を正規化できません（{e}）") from None
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def catalog_markdown(catalog: Any, templates: Any) -> str:
    """カタログをエージェント向けの Markdown にする（生成物）。"""
    lines: list[str] = [
        "# Blockly ブロック一覧（生成物）",
        "",
        "このファイルは自動生成です。手で編集しないでください。",
        "",
        "生成コマンド:",
        "",
        "```sh",
        "uv run --frozen python tools/blockly_agent.py catalog --format markdown "
        "--output docs/blockly-agent/references/blocks.md",
        "```",
        "",
        "仕様の書き方は [spec.md](spec.md)、使い方は [手順](../README.md) を参照してください。",
        "",
    ]
    template_names = (
        [str(name) for name in templates]
        if isinstance(templates, (list, tuple))
        else []
    )
    lines.extend(["## 画像テンプレート（`TEMPLATE` に書ける名前）", ""])
    if template_names:
        for name in sorted(template_names):
            lines.append(f"- `{name}`")
    else:
        lines.append("- （`Template/` に画像がありません）")
    lines.append("")

    blocks = _catalog_blocks(catalog)
    if not blocks:
        lines.extend(
            [
                "## ブロック",
                "",
                "- カタログを取得できませんでした（assets/blockly を確認してください）",
                "",
            ]
        )
        return "\n".join(lines)

    pokecon = sorted(name for name in blocks if name.startswith("pokecon_"))
    standard = sorted(name for name in blocks if not name.startswith("pokecon_"))
    lines.extend(["## ブロック", ""])
    lines.extend(
        [
            "### 短縮形",
            "",
            "仕様では次の短縮形を使えます（`type`/`fields`/`inputs` の汎用形も可）。",
            "",
            "- `press` / `hold` / `release` / `stick` / `stick_off` / `wait` / "
            "`press_rep`",
            "- `repeat` / `while` / `if`(+`else`) / `break` / `continue`",
            "- `print` / `comment` / `finish` / `set` / `change` / `call` / "
            "`call_value`",
            "- `screenshot` / `discord` / `dialog_choice` / `dialog_number` / "
            "`dialog_check`",
            "",
        ]
    )
    lines.extend(["## PokeCon ブロック", ""])
    for name in pokecon:
        lines.extend(_block_markdown(name, blocks[name]))
    lines.extend(["## 標準ブロック（許可分）", ""])
    for name in standard:
        lines.extend(_block_markdown(name, blocks[name]))
    lines.extend(
        [
            "## 補足",
            "",
            '- `controls_if` の `else` は `extraState: {"hasElse": true}` と '
            "`inputs.ELSE` で表す（仕様の `if` 短縮形が自動で行う）。",
            "- `variables_set` / `variables_get` / `math_change` / `controls_for` の "
            '`VAR` は変数名の文字列で書き、builder が `{"id": 名前}` と '
            "`variables` を補う。",
            "- 設定ダイアログの `VAR` は Python の変数名（文字列）のままで、"
            "`variables` には足さない。",
            "- 画像認識ブロックの `TEMPLATE` は空にできない。"
            "`Template/<名前>` が存在すること。`CROP` は空か `x1,y1,x2,y2`、"
            "`THRESHOLD` は 0〜1。",
            "",
        ]
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 検査の流れ
# ---------------------------------------------------------------------------


def _check_spec(spec: Any, app_dir: str | Path) -> CheckResult:
    catalog_result = blockly_headless.catalog(app_dir)
    if not catalog_result.ok or catalog_result.data is None:
        return CheckResult(
            False,
            errors=catalog_result.errors or ["Blockly のカタログを取得できません"],
            warnings=catalog_result.warnings,
            stem=_spec_stem(spec),
        )
    built = build_workspace(spec, catalog_result.data)
    warnings = [*catalog_result.warnings, *built.warnings]
    stem = _spec_stem(spec)
    if not built.ok or built.workspace is None:
        return CheckResult(False, built.errors, warnings, built.workspace, "", stem)
    return _inspect_workspace(
        built.workspace, app_dir, catalog_result.data, warnings, stem
    )


def _check_workspace(workspace_json: Any, app_dir: str | Path) -> CheckResult:
    workspace, errors = _workspace_object(workspace_json)
    if errors or workspace is None:
        return CheckResult(False, errors)
    errors = _validate_json_text(workspace)
    if errors:
        return CheckResult(False, _dedupe(errors))
    catalog_result = blockly_headless.catalog(app_dir)
    if not catalog_result.ok or catalog_result.data is None:
        return CheckResult(
            False,
            errors=catalog_result.errors or ["Blockly のカタログを取得できません"],
            warnings=catalog_result.warnings,
            workspace=workspace,
        )
    return _inspect_workspace(
        workspace, app_dir, catalog_result.data, list(catalog_result.warnings), None
    )


def _inspect_workspace(
    workspace: dict[str, Any],
    app_dir: str | Path,
    catalog: Any,
    warnings: list[str],
    stem: str | None,
) -> CheckResult:
    """workspace を共通の検査へ通す（仕様由来と既存workspaceの両方）。"""
    errors: list[str] = []
    if stem is not None:
        errors.extend(f"stem: {item}" for item in blockly_validate.validate_stem(stem))
    errors.extend(_check_template_files(workspace, app_dir, catalog))
    if errors:
        return CheckResult(False, _dedupe(errors), warnings, workspace, "", stem)

    generated = blockly_headless.generate(workspace, app_dir)
    warnings = [*warnings, *generated.warnings]
    if not generated.ok or generated.data is None:
        errors.extend(generated.errors or ["Blockly でコードを生成できませんでした"])
        return CheckResult(False, _dedupe(errors), warnings, workspace, "", stem)
    code = generated.data.get("code")
    if not isinstance(code, str) or not code.strip():
        errors.append("Blockly の生成コードが空です")
        return CheckResult(False, _dedupe(errors), warnings, workspace, "", stem)
    errors.extend(blockly_validate.validate_generated_code(code))
    errors.extend(blockly_validate.validate_input_code(code, app_dir))
    errors.extend(blockly_templates.validate_template_refs(code))
    return CheckResult(not errors, _dedupe(errors), warnings, workspace, code, stem)


def _render_workspace(workspace_json: Any, app_dir: str | Path) -> RenderResult:
    checked = _check_workspace(workspace_json, app_dir)
    return RenderResult(
        checked.ok,
        code=checked.code,
        errors=checked.errors,
        warnings=checked.warnings,
    )


def _validate_json_text(workspace: Any) -> list[str]:
    try:
        text = json.dumps(workspace, ensure_ascii=False)
    except (TypeError, ValueError, RecursionError) as e:
        return [f"ワークスペースをJSONにできません（{e}）"]
    errors = blockly_validate.validate_workspace_json(text)
    errors.extend(blockly_validate.validate_dialog_vars(text))
    return errors


def _check_template_files(
    workspace: dict[str, Any], app_dir: str | Path, catalog: Any
) -> list[str]:
    """画像認識ブロックの TEMPLATE の実体を確かめる。

    名前の存在だけでなく、`Template/` 配下に実在すること（外へのリンクを
    辿らない）と、画像として復号できることまで見る。重複参照は一度だけ
    読む。実フレームが無い検査では、検索ROIの画面内整合や認識精度までは
    確かめない（それは実画像の照合で確認する）。
    """
    base = (Path(app_dir) / "Template").resolve()
    names = blockly_templates.list_image_templates(app_dir)
    base_names = {PurePosixPath(name).name for name in names}
    blocks = _catalog_blocks(catalog)
    errors: list[str] = []
    reported: set[str] = set()
    decoded: dict[Path, list[str]] = {}
    for block in _iter_blocks(workspace):
        info = blocks.get(str(block.get("type")))
        if info is None:
            continue
        fields = block.get("fields")
        if not isinstance(fields, dict):
            fields = {}
        inputs = block.get("inputs")
        expr = inputs.get("TEMPLATE_EXPR") if isinstance(inputs, dict) else None
        if isinstance(expr, dict) and isinstance(expr.get("block"), dict):
            # 動的指定。実在は option_template 側（validate_input_code）で見る。
            continue
        for spec in info.get("fields", []):
            if not isinstance(spec, dict):
                continue
            if spec.get("kind") != "template" or spec.get("serializable") is False:
                continue
            name = str(spec.get("name", "TEMPLATE"))
            value = fields.get(name, spec.get("default"))
            if not isinstance(value, str) or not value.strip():
                if "empty" not in reported:
                    reported.add("empty")
                    errors.append(
                        "TEMPLATE が空です（Template/ の画像名を指定してください）"
                    )
                continue
            raw_rel = value.replace("\\", "/").lstrip("/")
            if ":" in raw_rel or ".." in raw_rel.split("/"):
                if raw_rel not in reported:
                    reported.add(raw_rel)
                    errors.append(f"TEMPLATE の場所が不正です: {value!r}")
                continue
            rel = PurePosixPath(raw_rel).as_posix()
            if rel in reported:
                continue
            reported.add(rel)
            target = base / rel
            try:
                resolved = target.resolve()
                resolved.relative_to(base)
            except (OSError, ValueError):
                errors.append(f"TEMPLATE の場所が不正です: {value!r}")
                continue
            if resolved not in decoded:
                decoded[resolved] = _decode_template_file(resolved, value)
            errors.extend(decoded[resolved])
            if not resolved.is_file():
                hint = _suggest_hint(PurePosixPath(rel).name, base_names)
                errors.append(
                    f"TEMPLATE の画像が見つかりません: {value!r}"
                    f"（Template/ に置くか --app-dir を確認してください）{hint}"
                )
    return errors


def _decode_template_file(path: Path, shown: str) -> list[str]:
    """テンプレ画像1枚を復号して、読めるか・寸法が正しいかを見る。"""
    if not path.is_file():
        return []
    try:
        data = path.read_bytes()
    except OSError as e:
        return [f"TEMPLATE の画像を読めません: {shown!r}（{e}）"]
    try:
        image = blockly_match.decode_frame_png(data)
    except ValueError:
        return [f"TEMPLATE の画像が壊れています（画像として読めません）: {shown!r}"]
    if image.size == 0 or image.shape[0] == 0 or image.shape[1] == 0:
        return [f"TEMPLATE の画像が空です: {shown!r}"]
    return []


# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------


def _block_markdown(name: str, info: dict[str, Any]) -> list[str]:
    lines = [f"### `{name}`", ""]
    tooltip = info.get("tooltip")
    if isinstance(tooltip, str) and tooltip.strip():
        lines.extend([tooltip.strip(), ""])
    shape = _shape_label(info)
    lines.append(f"- 形: {shape}")
    fields = info.get("fields")
    if isinstance(fields, list) and fields:
        lines.append("- fields:")
        for spec in fields:
            if not isinstance(spec, dict) or not spec.get("name"):
                continue
            kind = str(spec.get("kind", "text"))
            if spec.get("serializable", True) is False:
                lines.append(f"  - `{spec['name']}` ({kind}、表示専用)")
                continue
            default = spec.get("default")
            suffix = ""
            if kind not in ("variable",) and default not in (None, ""):
                suffix = f"、既定 `{default}`"
            options = spec.get("options")
            option_text = ""
            if kind != "variable" and isinstance(options, list) and options:
                values = [
                    f"`{option[1]}`"
                    for option in options
                    if isinstance(option, list | tuple) and len(option) > 1
                ]
                if values:
                    shown = ", ".join(values[:24])
                    if len(values) > 24:
                        shown += f", …（他 {len(values) - 24} 個）"
                    option_text = f"、選択肢: {shown}"
            lines.append(f"  - `{spec['name']}` ({kind}{suffix}){option_text}")
    inputs = info.get("inputs")
    if isinstance(inputs, list) and inputs:
        lines.append("- inputs:")
        for spec in inputs:
            if not isinstance(spec, dict) or not spec.get("name"):
                continue
            check = spec.get("check")
            check_text = ""
            if isinstance(check, list) and check:
                check_text = " " + "|".join(str(item) for item in check)
            lines.append(
                f"  - `{spec['name']}` ({spec.get('kind', 'value')}{check_text})"
            )
    lines.append("")
    return lines


def _shape_label(info: dict[str, Any]) -> str:
    output = info.get("output")
    previous = bool(info.get("previous"))
    nxt = bool(info.get("next"))
    if output is not None:
        if isinstance(output, list) and output:
            return "値（" + "|".join(str(item) for item in output) + "）"
        return "値（型自由）"
    if previous and nxt:
        return "文（前後に接続）"
    if previous:
        return "文（末尾のみ）"
    return "トップレベル専用"
