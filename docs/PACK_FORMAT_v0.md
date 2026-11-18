# 配布書式 v0（pokecon.json。`format`欄は将来の判別用に予約、現行は無くてもv0扱い）

zip 内の配置は以下のとおりである。

- `pokecon.json`（本書式）
- `Commands/PythonCommands/<entry>`（自作スクリプト本体。実ツリーと同じ形）
- `Template/<name>/...`（テンプレ画像。素の名前は新規禁止）

書式および検証の規則は以下のとおりである。

- `pokecon.json` の項目は [`pokecon/domain/pack_manifest.py`](../SerialController/pokecon/domain/pack_manifest.py) の検査を正とする。
- `entry`・`templates` は相対パスのみとする（絶対パス・`..`・`:` は禁止）。
- `entry` のフォルダ名・ファイル名（拡張子を除く）は英字・数字・`_` のみとする（例: `my_pack/MyPack.py`、`my-pack/` は不可）。
- 画像拡張子は `.png` / `.jpg` / `.jpeg` / `.bmp` とする。
- `entry` の `import` 走査は相対 import と公開面外を異常とし、未知のトップレベルは注意に留める（正本は [`pokecon/domain/user_api_allowlist.py`](../SerialController/pokecon/domain/user_api_allowlist.py) である）。

検証は `uv run --frozen pytest tests/test_pack_manifest.py -q` で実行する。

## zipの配置

- `pokecon.json`
- `Commands/PythonCommands/<entry>`（単一 `.py`。区切りは英字・数字・`_` のみ）
- `Template/<name>/...`

- **記録とバックアップ**: 記録は `<APP_DIR>/InstalledPacks/<name>.json`、上書き前の写しは `<APP_DIR>/InstalledPacks/.backup/<name>_<日時>/` に保存する。
- **再導入と実行中制限**: 同版の再導入は失敗となり、異版は確認のうえで上書きする。実行中は GUI が受け付けない。
- **CLI**: `uv run --frozen python SerialController/pokecon/app/ScriptPackTool.py <pack|check|install|uninstall|list> ...` を使用する（`--help` で詳細を確認できる）。
