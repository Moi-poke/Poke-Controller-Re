"""Bcon設定小窓のhostベクタ（実機不要・Task 6）。

窓自体は作らない（ヘッドレス）。純粋助けと接続手順（開→HELLO→
live開始）を偽transportで固める。Tkを使う箇所は __new__＋窓 stub で
閉鎖規律（溜め捨て・閉鎖後put禁止）だけ見る。
"""


def test_bcon_setup_imports_without_touching_wake():
    import importlib.util
    from pathlib import Path

    assert Path("SerialController/BconSetup.py").exists()
    assert Path("SerialController/WakeSetup.py").exists()
    spec = importlib.util.spec_from_file_location(
        "bcon_setup", "SerialController/BconSetup.py"
    )
    assert spec is not None and spec.loader is not None


def test_parse_capture_seconds() -> None:
    from BconSetup import parse_capture_seconds

    assert parse_capture_seconds("15") == 15
    assert parse_capture_seconds(1) == 1
    assert parse_capture_seconds("60") == 60
    assert parse_capture_seconds(" 30 ") == 30
    assert parse_capture_seconds("0") is None
    assert parse_capture_seconds("61") is None
    assert parse_capture_seconds("abc") is None
    assert parse_capture_seconds("") is None
    assert parse_capture_seconds(None) is None


def test_parse_color_hex_and_build_payload() -> None:
    from BconSetup import build_color_payload, parse_color_hex

    assert parse_color_hex("ff0000") == (255, 0, 0)
    assert parse_color_hex("#00FF00") == (0, 255, 0)
    assert parse_color_hex("0000ff") == (0, 0, 255)
    assert parse_color_hex("fff") is None
    assert parse_color_hex("xyz123") is None
    assert parse_color_hex("") is None
    payload = build_color_payload([(255, 0, 0), (0, 255, 0), (0, 0, 255), (0, 0, 0)])
    assert payload == bytes([255, 0, 0, 0, 255, 0, 0, 0, 255, 0, 0, 0])
    assert len(payload) == 12
    try:
        build_color_payload([(0, 0, 0)] * 3)
    except ValueError:
        pass
    else:
        raise AssertionError("3色は12BにならないためValueErrorのはず")


def test_describe_bcon_errcode() -> None:
    from BconSetup import config_reject_type, describe_bcon_errcode

    assert "正常" in describe_bcon_errcode(0x00)
    assert "CRC" in describe_bcon_errcode(0x02)
    assert "CAPTURE" in describe_bcon_errcode(0x10)
    assert "BEACON" in describe_bcon_errcode(0x11)
    assert "WIRED" in describe_bcon_errcode(0x14)
    assert "不明" in describe_bcon_errcode(0xFF)
    assert config_reject_type(0x10) == 0x30
    assert config_reject_type(0x11) == 0x31
    assert config_reject_type(0x14) == 0x34
    assert config_reject_type(0x00) is None
    assert config_reject_type(0x06) is None


def test_format_status_flags() -> None:
    from BconSetup import format_status_flags

    assert "有線" in format_status_flags(0x20)
    wired_off = format_status_flags(0x03)
    assert "USB" in wired_off
    assert "Switch" in wired_off
    assert "有線" not in wired_off
    assert "なし" in format_status_flags(0x00)


def test_decode_player_lamp_bitmask() -> None:
    """プレイヤーLEDの下位4bitをそのまま4つの真偽へ（並べ替え無し）。"""
    from BconSetup import decode_player_lamp

    # bit0=LED1, bit1=LED2, bit2=LED3, bit3=LED4。0x00-0x0Fを総当たり。
    for value in range(0x10):
        assert decode_player_lamp(value) == (
            bool(value & 0x01),
            bool(value & 0x02),
            bool(value & 0x04),
            bool(value & 0x08),
        )
    # 代表値の抜き取り。
    assert decode_player_lamp(0x00) == (False, False, False, False)
    assert decode_player_lamp(0x01) == (True, False, False, False)
    assert decode_player_lamp(0x03) == (True, True, False, False)
    assert decode_player_lamp(0x07) == (True, True, True, False)
    assert decode_player_lamp(0x0F) == (True, True, True, True)
    assert decode_player_lamp(0x02) == (False, True, False, False)
    assert decode_player_lamp(0x05) == (True, False, True, False)
    assert decode_player_lamp(0x0A) == (False, True, False, True)


def test_decode_player_flags_imu_vib() -> None:
    """プレイヤーflagsはbit0=IMU・bit1=振動のみ。予約上位bitは無視。"""
    from BconSetup import decode_player_flags

    assert decode_player_flags(0x00) == (False, False)
    assert decode_player_flags(0x01) == (True, False)
    assert decode_player_flags(0x02) == (False, True)
    assert decode_player_flags(0x03) == (True, True)
    # 予約bit（bit2-7）は見ない: 0xFC=1111_1100 -> (False, False)。
    assert decode_player_flags(0xFC) == (False, False)
    assert decode_player_flags(0xFF) == (True, True)


def test_decode_player_imu_range_bits() -> None:
    """flags bit3-4=gyro・bit5-6=accel の要求レンジ復号。"""
    from BconSetup import decode_player_imu_range

    assert decode_player_imu_range(0x00) == ("±250dps", "±8G")
    assert decode_player_imu_range(0x18) == ("±2000dps", "±8G")  # gyro sel=3
    # IMU/vib/saved (bit0-2) は範囲に影響しない。
    assert decode_player_imu_range(0x07) == ("±250dps", "±8G")
    # accel sel=2 (±2G)
    assert decode_player_imu_range(0x18 | (2 << 5)) == ("±2000dps", "±2G")


def test_player_lamp_s2_edge_no_raise() -> None:
    """S2端: 予約/空/1byteでも落とさない（据え置き or 全消灯）。

    GREENで窓口 _update_player_lamps(payload) を足すとき、b"" や1byte・
    予約値は「据え置き or 全消灯」で例外にしない契約にする。ここでは
    その手前の復号段が全byteで例外を出さないことだけ固める。
    """
    from BconSetup import decode_player_flags, decode_player_lamp

    for value in range(0x100):
        lamp = decode_player_lamp(value)
        assert len(lamp) == 4
        assert all(isinstance(bit, bool) for bit in lamp)
        flags = decode_player_flags(value)
        assert len(flags) == 2
        assert all(isinstance(bit, bool) for bit in flags)


def test_is_bcon_transport() -> None:
    from BconSetup import is_bcon_transport

    class Bcon:
        name = "bcon"
        capability = "BCON_STATE"

    class Legacy:
        name = "legacy_text"
        capability = "TEXT_ROW"

    assert is_bcon_transport(Bcon()) is True
    assert is_bcon_transport(Legacy()) is False
    assert is_bcon_transport(None) is False
    assert is_bcon_transport(object()) is False


def test_send_config_frame_uses_session_mouth() -> None:
    from BconSetup import send_config_frame

    class Fake:
        def __init__(self) -> None:
            self.sent: list[tuple[int, bytes]] = []

        def _send_session_frame(self, type_: int, payload: bytes, note: str) -> bool:
            _ = note
            self.sent.append((type_, bytes(payload)))
            return True

    fake = Fake()
    assert send_config_frame(fake, 0x30, b"\x0f") is True
    assert fake.sent == [(0x30, b"\x0f")]
    # 会話口が無ければ送らずFalse（落とさない）。
    assert send_config_frame(object(), 0x30, b"\x0f") is False
    assert send_config_frame(None, 0x30, b"\x0f") is False

    class Raiser:
        def _send_session_frame(self, type_: int, payload: bytes, note: str) -> bool:
            raise RuntimeError("boom")

    assert send_config_frame(Raiser(), 0x30, b"\x0f") is False


def test_retry_and_hunt_are_the_only_failed_open_recovery() -> None:
    from bcon_ui_support import Service, setup

    service = Service(opened=True, phase="failed")
    obj = setup(service)
    obj._on_connect()
    obj._on_hunt()
    obj._on_status()
    assert (service.retry, service.hunt) == (1, 1)
    assert not service.operations


def test_busy_and_disconnected_handlers_never_submit_work() -> None:
    from bcon_ui_support import Service, setup

    for service, busy in [
        (Service(), False),
        (Service(opened=True, phase="ready"), True),
    ]:
        obj = setup(service)
        obj._command_busy = lambda: busy
        for handler in (
            obj._on_connect,
            obj._on_hunt,
            obj._on_status,
            obj._on_ping,
            obj._on_capture,
            obj._on_beacon,
            obj._on_clear,
            obj._on_delete_keys,
            obj._on_color,
            obj._on_bootsel,
            obj._on_wired_on,
            obj._on_wired_off,
            obj._on_reconnect,
        ):
            handler()
        assert not service.operations
        assert service.retry == service.hunt == 0


def test_status_dictionary_is_success_and_timeout_does_not_refresh_truth() -> None:
    from types import SimpleNamespace

    from bcon_ui_support import Service, setup

    service = Service(opened=True, phase="ready")
    obj = setup(service)
    obj._on_status()
    work = service.operations[-1][1]
    status = dict(flags=0, last_seq=1, err_crc=0, err_drop=0, errcode=0)
    assert work(SimpleNamespace(request_status=lambda **_: status)) is True
    assert work(SimpleNamespace(request_status=lambda **_: None)) is False
    assert service.snapshot.status_received_at is None


def test_config_sticky_error_accepts_only_unchanged_error_and_counters() -> None:
    from types import SimpleNamespace

    from bcon_ui_support import setup

    obj = setup()
    pre = dict(flags=0, last_seq=1, err_crc=0, err_drop=3, errcode=0x11)
    notes: list[str] = []
    post = dict(pre)
    transport = SimpleNamespace(request_status=lambda **_: post)
    assert obj._config_result(transport, "BEACON", pre, log=notes.append) is True
    assert "過去の記録" in notes[-1]
    post["err_drop"] += 1
    assert obj._config_result(transport, "BEACON", pre, log=notes.append) is False
    post.update(err_drop=3, errcode=0x10)
    assert obj._config_result(transport, "取込", pre, log=notes.append) is False


def test_close_unsubscribes_and_rejects_delayed_callbacks() -> None:
    from bcon_ui_support import Service, setup

    service = Service()
    obj = setup(service)
    obj.set_visible(True)
    obj._queue.put(("log", "pending"))
    obj.close()
    obj.close()
    obj._receive_snapshot(service.snapshot)
    obj._receive_log(0, "late")
    assert not service.callbacks and not service.logs
    assert not obj.window.pending
    assert obj._queue.empty()
    assert obj.inbox.pending() == 0


def test_delayed_operation_logs_are_discarded_after_generation_change() -> None:
    from bcon_ui_support import Service, setup

    service = Service(generation=1)
    obj = setup(service)
    obj.set_visible(True)
    log = obj._operation_log()
    service.publish(generation=2)
    log("旧接続の遅延ログ")
    obj._poll()
    assert "旧接続の遅延ログ" not in obj._log.text


def test_unsupported_method_disables_only_the_matching_action() -> None:
    from types import SimpleNamespace

    from bcon_ui_support import Service, setup

    service = Service(opened=True, phase="ready")
    obj = setup(service)
    obj.set_visible(True)
    obj._on_ping()
    assert service.operations[-1][1](SimpleNamespace()) is False
    obj._poll()
    assert obj._btn_ping.values["state"] == "disabled"
    assert obj._btn_status.values["state"] == "normal"


def test_closed_log_callback_cannot_repopulate_drained_queue() -> None:
    from bcon_ui_support import setup

    obj = setup()
    log = obj._operation_log()
    obj.close()
    log("late")
    assert obj._queue.empty()
