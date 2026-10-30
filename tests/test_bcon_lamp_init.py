"""ランプ表示の真実は接続サービスの写しだけから取得する。"""

from dataclasses import replace

from BconSetup import PLAYER_LAMP_OFF_COLOR, PLAYER_LAMP_ON_COLOR
from bcon_ui_support import Service, setup


def test_snapshot_seeds_lamps_without_transport_getters() -> None:
    service = Service(generation=2, opened=True, phase="ready", player_info=b"\x05\x03")
    obj = setup(service)
    obj.set_visible(True)
    assert obj._lamp_canvas.values["0"]["fill"] == PLAYER_LAMP_ON_COLOR
    assert obj._lamp_canvas.values["1"]["fill"] == PLAYER_LAMP_OFF_COLOR
    assert obj._lamp_canvas.values["2"]["fill"] == PLAYER_LAMP_ON_COLOR


def test_old_generation_never_reseeds_lamps() -> None:
    service = Service(generation=3)
    obj = setup(service)
    obj.set_visible(True)
    obj._receive_snapshot(
        replace(service.snapshot, generation=2, player_info=b"\x0f\x03")
    )
    obj.inbox.drain()
    assert obj._snapshot.generation == 3
    assert all(
        value["fill"] == PLAYER_LAMP_OFF_COLOR
        for value in obj._lamp_canvas.values.values()
    )


def test_disconnected_snapshot_clears_visible_lamps() -> None:
    service = Service(opened=True, phase="ready", player_info=b"\x0f\x03")
    obj = setup(service)
    obj.set_visible(True)
    service.publish(
        generation=1, opened=False, phase="disconnected", player_info=b"", rumble=b""
    )
    obj.inbox.drain()
    assert all(
        value["fill"] == PLAYER_LAMP_OFF_COLOR
        for value in obj._lamp_canvas.values.values()
    )
