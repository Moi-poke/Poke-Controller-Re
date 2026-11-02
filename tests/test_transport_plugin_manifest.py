"""transport プラグインの manifest 読み込み（gate と隔離）の検証。"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from pokecon.domain.transport import registry
from pokecon.plugins import transports


def _write_plugin(
    root: Path,
    dirname: str,
    *,
    manifest: dict[str, object] | None,
    body: str = (
        "def register(register_transport):\n"
        "    register_transport('test-{name}', lambda **kw: None)\n"
    ),
) -> Path:
    directory = root / dirname
    directory.mkdir(parents=True)
    if manifest is not None:
        lines = [f"{key} = {json.dumps(value)}" for key, value in manifest.items()]
        (directory / "manifest.toml").write_text(
            "\n".join(lines) + "\n", encoding="utf-8"
        )
    (directory / "__init__.py").write_text(
        body.replace("{name}", dirname), encoding="utf-8"
    )
    return directory


def _manifest(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "name": "test",
        "version": "1.0.0",
        "api_version": transports.API_VERSION,
        "min_app_version": "4.0.0",
    }
    base.update(overrides)
    return base


@pytest.fixture(autouse=True)
def _cleanup() -> Iterator[None]:
    """登録した名前を消す（registry はプロセス全体で共有される）。"""
    before = set(registry._REGISTRY)
    yield
    for name in set(registry._REGISTRY) - before:
        registry.unregister_transport(name)


def test_valid_manifest_registers_and_reports_no_reason(tmp_path: Path) -> None:
    _write_plugin(tmp_path, "good", manifest=_manifest())

    added, rejected = transports.load_dir(str(tmp_path))

    assert added == ["test-good"]
    assert rejected == []


def test_api_version_mismatch_is_refused_with_a_reason(tmp_path: Path) -> None:
    _write_plugin(tmp_path, "old-api", manifest=_manifest(api_version=0))

    added, rejected = transports.load_dir(str(tmp_path))

    assert added == []
    assert len(rejected) == 1
    assert "api_version" in rejected[0]


def test_newer_min_app_version_is_refused(tmp_path: Path) -> None:
    _write_plugin(tmp_path, "future", manifest=_manifest(min_app_version="99.0.0"))

    added, rejected = transports.load_dir(str(tmp_path))

    assert added == []
    assert "min_app_version" in rejected[0]


def test_missing_manifest_is_reported_not_loaded(tmp_path: Path) -> None:
    _write_plugin(tmp_path, "bare", manifest=None)

    added, rejected = transports.load_dir(str(tmp_path))

    assert added == []
    assert "manifest.toml" in rejected[0]


def test_one_broken_plugin_does_not_stop_the_rest(tmp_path: Path) -> None:
    _write_plugin(
        tmp_path, "broken", manifest=_manifest(), body="raise RuntimeError('boom')\n"
    )
    _write_plugin(tmp_path, "healthy", manifest=_manifest())

    added, rejected = transports.load_dir(str(tmp_path))

    assert added == ["test-healthy"]
    assert len(rejected) == 1
    assert "broken" in rejected[0]


def test_empty_dir_returns_nothing() -> None:
    assert transports.load_dir("") == ([], [])
    assert transports.load_dir("no_such_dir_at_all") == ([], [])
