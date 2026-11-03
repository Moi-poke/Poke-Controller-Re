"""Pokemon Home 統計の JSON 読み書き（キャッシュ利用の経路）の検証。

`json.load(fp, encoding=...)` は Python 3 で TypeError になり、保存済み
データを使う経路が落ちていた。読み書きを `_load_json` / `_save_json` に
集約したので、往復と旧形式（ASCII エスケープ）の読み込みを確かめる。
"""

from __future__ import annotations

from pathlib import Path

from pokecon.app.get_pokestatistics import _load_json, _save_json


def test_round_trip_keeps_japanese_readable(tmp_path: Path) -> None:
    data = {"英語": "Pikachu", "データ": [1, {"名前": "ピカチュウ"}]}
    target = tmp_path / "stats.json"

    _save_json(str(target), data)

    assert "ピカチュウ" in target.read_text(encoding="utf-8")
    assert _load_json(str(target)) == data


def test_reads_ascii_escaped_files_from_the_old_writer(tmp_path: Path) -> None:
    target = tmp_path / "old.json"
    target.write_text(
        '{"name": "\\u30d4\\u30ab\\u30c1\\u30e5\\u30a6"}', encoding="utf-8"
    )

    assert _load_json(str(target)) == {"name": "ピカチュウ"}
