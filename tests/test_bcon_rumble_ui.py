"""振動表示は世代つきsnapshotから更新し、UIではRPCを呼ばない。"""

import threading

from bcon_ui_support import Service, setup


def test_worker_snapshot_callback_only_enqueues() -> None:
    service = Service(opened=True, phase="ready")
    obj = setup(service)
    obj.set_visible(True)
    before = dict(obj._vib_label.values)
    worker = threading.Thread(target=lambda: service.publish(rumble=b"\x0a\x05"))
    worker.start()
    worker.join()
    assert obj._vib_label.values == before
    obj.inbox.drain()
    assert "L=10 R=5" in obj._vib_label.values["text"]


def test_missing_rumble_is_unknown_and_no_stale_payload_survives_disconnect() -> None:
    service = Service(opened=True, phase="ready", rumble=b"\x0a\x05")
    obj = setup(service)
    obj.set_visible(True)
    service.publish(generation=1, phase="disconnected", opened=False, rumble=b"")
    obj.inbox.drain()
    assert obj._vib_label.values["text"] == "振動: 未受信"


def test_status_age_is_stale_without_guessing_connection_mode() -> None:
    import time

    service = Service(opened=True, phase="ready")
    obj = setup(service)
    obj.set_visible(True)
    assert "Switch 接続方式: 不明" in obj._summary.values["text"]
    service.publish(status=(0x20, 1, 0, 0, 0), status_received_at=time.monotonic() - 4)
    obj.inbox.drain()
    assert "有線（古い情報）" in obj._summary.values["text"]
