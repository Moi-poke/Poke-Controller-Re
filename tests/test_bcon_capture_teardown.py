"""取込期限と非表示・終了のキャンセル。サービスの購読は取込後も維持する。"""

from BconSetup import SAVE_SUCCESS_TEXT, SAVE_TIMEOUT_TEXT
from bcon_ui_support import Service, setup


def test_capture_deadline_requires_saved_player_info() -> None:
    obj = setup(Service(opened=True, phase="ready"))
    obj.set_visible(True)
    obj._arm_capture_deadline(1)
    obj._note_player_save(4)
    obj._on_capture_deadline()
    obj._poll()
    assert SAVE_SUCCESS_TEXT in obj._log.text
    assert len(obj._service.callbacks) == 1


def test_capture_without_player_info_does_not_infer_success() -> None:
    obj = setup()
    obj.set_visible(True)
    obj._arm_capture_deadline(1)
    obj._on_capture_deadline()
    obj._poll()
    assert SAVE_TIMEOUT_TEXT in obj._log.text


def test_hide_cancels_capture_and_poll_but_retains_log() -> None:
    obj = setup()
    obj.set_visible(True)
    obj._append("履歴")
    obj._arm_capture_deadline(15)
    obj.set_visible(False)
    assert obj._capture_deadline_id is None
    assert obj._poll_id is None
    assert not obj.window.pending
    assert not obj._service.callbacks and not obj._service.logs
    obj.set_visible(True)
    obj.set_visible(True)
    assert obj._log.text == "履歴\n"
    assert len(obj.window.pending) == 1
    assert len(obj._service.callbacks) == 1


def test_generation_change_cancels_capture_deadline() -> None:
    service = Service(opened=True, phase="ready")
    obj = setup(service)
    obj.set_visible(True)
    obj._arm_capture_deadline(15)
    service.publish(generation=1, opened=False, phase="closing", player_info=b"")
    obj.inbox.drain()
    assert obj._capture_deadline_id is None
    assert obj._capture_saved_seen is False
