"""再生操作もCONFIGの粘着errcodeを差分で判定する。"""

from types import SimpleNamespace
from typing import Any

from bcon_ui_support import Service, setup


def test_beacon_requires_saved_player_info() -> None:
    service = Service(opened=True, phase="ready")
    obj = setup(service)
    obj._on_beacon()
    assert not service.operations


def test_beacon_unchanged_sticky_error_is_success() -> None:
    status = dict(flags=0, last_seq=7, err_drop=2, err_crc=0, errcode=0x11)
    sent: list[Any] = []

    def send_frame(*args: Any) -> bool:
        sent.append(args)
        return True

    transport = SimpleNamespace(
        last_status=lambda: dict(status),
        request_status=lambda **_: dict(status),
        _send_session_frame=send_frame,
    )
    service = Service(transport, opened=True, phase="ready", player_info=b"\x01\x04")
    obj = setup(service)
    obj._on_beacon()
    assert service.operations[-1][1](transport) is True
    assert len(sent) == 1
