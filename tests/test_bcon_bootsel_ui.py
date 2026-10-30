"""再起動操作はサービスへ明示して、画面独自の再接続をしない。"""

from typing import Any

import pytest
from bcon_ui_support import Service, setup


@pytest.mark.parametrize(
    ("handler", "method", "restart", "bootloader"),
    [
        ("_on_bootsel", "request_bootsel", False, True),
        ("_on_wired_on", "set_wired_mode", True, False),
        ("_on_wired_off", "set_wired_mode", True, False),
    ],
)
def test_restart_operation_has_service_flags_and_actual_result(
    monkeypatch: Any,
    handler: str,
    method: str,
    restart: bool,
    bootloader: bool,
) -> None:
    from types import SimpleNamespace

    monkeypatch.setattr("BconSetup.tkmsg.askquestion", lambda *_a, **_k: "yes")
    service = Service(opened=True, phase="ready")
    obj = setup(service)
    getattr(obj, handler)()
    _, work, options = service.operations[-1]
    assert options == dict(restarts_device=restart, bootloader=bootloader)
    assert work(SimpleNamespace(**{method: lambda *_a, **_k: True})) is True
    assert work(SimpleNamespace(**{method: lambda *_a, **_k: False})) is False
    assert work(SimpleNamespace(**{method: lambda *_a, **_k: None})) is False


def test_cancelled_bootsel_never_submits(monkeypatch: Any) -> None:
    monkeypatch.setattr("BconSetup.tkmsg.askquestion", lambda *_a, **_k: "no")
    obj = setup(Service(opened=True, phase="ready"))
    obj._on_bootsel()
    assert not obj._service.operations
