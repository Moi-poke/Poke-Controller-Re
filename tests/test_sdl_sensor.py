"""SDLセンサー換算のhostベクタ（実機不要）。"""


def test_accel_1g_is_4096():
    from pokecon.domain.sdl_sensor import accel_to_raw

    # 1/0.000244 = 4098.36 -> 4098（純正実測≒4104と整合）
    assert accel_to_raw(9.80665) == 4098
    assert accel_to_raw(0.0) == 0
    assert accel_to_raw(-9.80665) == -4098


def test_gyro_1rad_is_938():
    from pokecon.domain.sdl_sensor import gyro_to_raw

    # 1rad/s = 57.2958dps / 0.06103 = 938.7 -> 939
    assert gyro_to_raw(1.0) == 939
    assert gyro_to_raw(0.0) == 0


def test_conversion_saturates_and_rejects_nonfinite():
    from pokecon.domain.sdl_sensor import accel_to_raw, gyro_to_raw

    assert accel_to_raw(1e9) == 32767
    assert accel_to_raw(-1e9) == -32768
    assert gyro_to_raw(1e9) == 32767
    assert accel_to_raw(float("nan")) == 0
    assert accel_to_raw(float("inf")) == 0
    assert gyro_to_raw(float("nan")) == 0
    assert gyro_to_raw("bad") == 0  # type: ignore[arg-type]


def test_default_map_is_sdl_inverse():
    from pokecon.domain.sdl_sensor import SdlSensorReader

    # SDLのSwitch→SDL変換（SDL_X=-Sw_Y・SDL_Y=+Sw_Z・SDL_Z=-Sw_X）の逆写像。
    # Sw_X=-SDL_Z・Sw_Y=-SDL_X・Sw_Z=+SDL_Y。
    reader = SdlSensorReader()
    reader.read_raw = lambda: ((1.0, 2.0, 3.0), (0.1, 0.2, 0.3))  # type: ignore[method-assign]
    got = reader.read()
    assert got is not None
    from pokecon.domain.sdl_sensor import accel_to_raw, gyro_to_raw

    assert got[0] == accel_to_raw(-3.0)
    assert got[1] == accel_to_raw(-1.0)
    assert got[2] == accel_to_raw(2.0)
    assert got[3] == gyro_to_raw(-0.3)
    assert got[4] == gyro_to_raw(-0.1)
    assert got[5] == gyro_to_raw(0.2)


def test_axis_map_and_bias_applied():
    from pokecon.domain.sdl_sensor import SdlSensorReader

    reader = SdlSensorReader()
    calls = {"n": 0}

    def fake_raw():
        calls["n"] += 1
        return ((1.0, 2.0, 3.0), (0.1, 0.2, 0.3))

    reader.read_raw = fake_raw  # type: ignore[method-assign]
    base = reader.read()
    assert base is not None
    # 軸入替: accel恒等、gyro恒等へ戻す
    reader.set_axis_map((0, 1, 2), (1, 1, 1), (0, 1, 2), (1, 1, 1))
    mapped = reader.read()
    assert mapped is not None
    from pokecon.domain.sdl_sensor import accel_to_raw, gyro_to_raw

    assert mapped[0] == accel_to_raw(1.0)
    assert mapped[3] == gyro_to_raw(0.1)
    # バイアス: gyro Xから0.1引く -> 0になる
    reader.set_gyro_bias((0.1, 0.0, 0.0))
    biased = reader.read()
    assert biased is not None
    assert biased[3] == 0
    assert calls["n"] == 3


def test_axis_map_rejects_invalid():
    from pokecon.domain.sdl_sensor import SdlSensorReader

    reader = SdlSensorReader()
    reader.read_raw = lambda: ((9.80665, 0.0, 0.0), (0.0, 0.0, 0.0))  # type: ignore[method-assign]
    before = reader.read()
    reader.set_axis_map((5, 0, 1), (1, 1, 1), (0, 1, 2), (1, 1, 1))
    reader.set_gyro_bias((float("nan"), 0.0, 0.0))
    after = reader.read()
    assert before == after
    # 既定逆写像で Sw_Y=-SDL_X -> -4098
    assert after is not None and after == (0, -4098, 0, 0, 0, 0)


def test_closed_reader_returns_none():
    from pokecon.domain.sdl_sensor import SdlSensorReader

    reader = SdlSensorReader()
    assert reader.read() is None
    assert reader.is_open() is False
    reader.close()


def _sensor_with_lib(lib):
    from pokecon.domain.sdl_sensor import SdlSensorReader

    reader = SdlSensorReader()
    reader._ready = True
    reader._lib = lib
    return reader


def test_sensor_open_failure_logs_once():
    """SDL_GameControllerOpen の失敗は再試行しても1回だけ出す。"""
    from loguru import logger

    class _Lib:
        def SDL_GameControllerOpen(self, index):
            return None

    reader = _sensor_with_lib(_Lib())
    messages: list[str] = []
    handler = logger.add(lambda m: messages.append(str(m)), level="WARNING")
    try:
        assert reader.open(0, retries=1) is False
        assert reader.open(0, retries=1) is False
    finally:
        logger.remove(handler)
    assert sum("開けません" in m for m in messages) == 1


def test_sensor_enable_failure_logs_once():
    """SetSensorEnabled の失敗も理由ごとに1回だけ出す。"""
    from loguru import logger

    class _Lib:
        def SDL_GameControllerOpen(self, index):
            return 1

        def SDL_GameControllerHasSensor(self, handle, kind):
            return 1

        def SDL_GameControllerSetSensorEnabled(self, handle, kind, enabled):
            return -1

        def SDL_GameControllerClose(self, handle):
            return None

    reader = _sensor_with_lib(_Lib())
    messages: list[str] = []
    handler = logger.add(lambda m: messages.append(str(m)), level="WARNING")
    try:
        assert reader.open(0, retries=1) is False
        assert reader.open(0, retries=1) is False
    finally:
        logger.remove(handler)
    assert sum("有効化できません" in m for m in messages) == 1


def test_sensor_sdl_ensure_failure_logs_once(monkeypatch):
    """SDL初期化の失敗も再試行のたびに出さない。"""
    import sys
    import types

    from loguru import logger

    fake = types.ModuleType("pygame")
    fake._sdl2 = types.ModuleType("pygame._sdl2")  # type: ignore[attr-defined]
    fake_sdl2 = fake._sdl2
    fake_gc = types.ModuleType("pygame._sdl2.controller")
    fake_gc.init = lambda: None  # type: ignore[attr-defined]
    fake_sdl2.controller = fake_gc  # type: ignore[attr-defined]
    fake.init = lambda: (_ for _ in ()).throw(RuntimeError("no sdl"))  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "pygame", fake)
    monkeypatch.setitem(sys.modules, "pygame._sdl2", fake_sdl2)
    monkeypatch.setitem(sys.modules, "pygame._sdl2.controller", fake_gc)

    from pokecon.domain.sdl_sensor import SdlSensorReader

    reader = SdlSensorReader()
    messages: list[str] = []
    handler = logger.add(lambda m: messages.append(str(m)), level="WARNING")
    try:
        assert reader.available is False
        assert reader.available is False
    finally:
        logger.remove(handler)
    assert sum("SDLを初期化できません" in m for m in messages) == 1


def _bit(name):
    from pokecon.domain.controller_preview import CONTROL_NAMES

    return 1 << CONTROL_NAMES.index(name)


class _FakePadLib:
    """GameController の既存ハンドルだけを使う読取の偽SDL。"""

    def __init__(self, buttons=(), axes=None):
        self.handle = object()
        self.joy = object()
        self.attached = 1
        self.buttons = set(buttons)
        self.axes = dict(axes or {})
        self.opened = 0
        self.closed = 0
        self.sensor_enabled = []
        self.attached_handles = []
        self.joy_handles = []
        self.button_calls = []
        self.axis_calls = []

    def SDL_GameControllerGetAttached(self, handle):
        self.attached_handles.append(handle)
        return self.attached

    def SDL_GameControllerGetJoystick(self, handle):
        self.joy_handles.append(handle)
        return self.joy

    def SDL_GameControllerGetButton(self, handle, num):
        self.button_calls.append(num)
        return 1 if num in self.buttons else 0

    def SDL_JoystickGetAxis(self, joy, axis):
        self.axis_calls.append(axis)
        return self.axes.get(axis, 0)

    def SDL_GameControllerOpen(self, index):
        self.opened += 1
        return self.handle

    def SDL_GameControllerClose(self, handle):
        self.closed += 1

    def SDL_GameControllerSetSensorEnabled(self, handle, kind, enabled):
        self.sensor_enabled.append((kind, enabled))
        return 0


def _pad_reader(lib):
    from pokecon.domain.sdl_sensor import SdlSensorReader

    reader = SdlSensorReader()
    reader._ready = True
    reader._lib = lib
    reader._handle = lib.handle
    return reader


def test_read_pad_uses_existing_handle_without_open_or_close():
    """センサーが開いたハンドルをそのまま読み、開閉しない。"""
    from pokecon.domain.controller_preview import controls_from_pad
    from pokecon.domain.xinput import (
        BTN_A,
        BTN_DPAD_LEFT,
        BTN_DPAD_UP,
        BTN_LEFT_SHOULDER,
        BTN_LEFT_THUMB,
        BTN_RIGHT_SHOULDER,
        BTN_RIGHT_THUMB,
        BTN_START,
    )

    lib = _FakePadLib(buttons={0, 6, 5, 15, 9, 10, 7, 8, 11, 13})
    reader = _pad_reader(lib)
    reader._ready = False
    calls = []
    reader._ensure = lambda: calls.append(True) or False  # type: ignore[method-assign]

    state = reader.read_pad()

    assert state.connected is True
    assert calls == []
    assert lib.opened == 0
    assert lib.closed == 0
    assert lib.attached_handles == [lib.handle]
    assert lib.joy_handles == [lib.handle]
    assert state.pressed == frozenset(
        {
            "A",
            "START",
            "HOME",
            "CAPTURE",
            "LEFT_SHOULDER",
            "RIGHT_SHOULDER",
            "LEFT_THUMB",
            "RIGHT_THUMB",
            "DPAD_UP",
            "DPAD_LEFT",
        }
    )
    assert state.buttons == (
        BTN_A
        | BTN_START
        | BTN_LEFT_SHOULDER
        | BTN_RIGHT_SHOULDER
        | BTN_LEFT_THUMB
        | BTN_RIGHT_THUMB
        | BTN_DPAD_UP
        | BTN_DPAD_LEFT
    )
    # 描画側の割当でも同じボタンが落ちない。
    assert controls_from_pad(state).buttons == (
        _bit("A")
        | _bit("PLUS")
        | _bit("HOME")
        | _bit("CAPTURE")
        | _bit("L")
        | _bit("R")
        | _bit("LCLICK")
        | _bit("RCLICK")
        | _bit("UP")
        | _bit("LEFT")
    )


def test_read_pad_axes_follow_raw_joystick_order_and_invert_y():
    from pokecon.domain.sdl_sensor import _trigger_to_byte

    lib = _FakePadLib(
        axes={0: 32767, 1: 32767, 2: -32768, 3: -32768, 4: 32767, 5: -32768}
    )
    state = _pad_reader(lib).read_pad()

    assert state.connected is True
    assert sorted(lib.axis_calls) == [0, 1, 2, 3, 4, 5]
    assert state.thumb_lx == 32767
    assert state.thumb_ly == -32767
    assert state.thumb_rx == -32767
    assert state.thumb_ry == 32767
    assert state.left_trigger == _trigger_to_byte(1.0)
    assert state.right_trigger == _trigger_to_byte(-1.0)
    assert state.raw_axes == (1.0, 1.0, -1.0, -1.0, 1.0, -1.0)


def test_read_pad_packet_advances_only_on_content_change():
    lib = _FakePadLib(axes={0: 0, 1: 0}, buttons={0})
    reader = _pad_reader(lib)

    first = reader.read_pad()
    second = reader.read_pad()
    assert first.connected and second.connected
    assert second.packet == first.packet

    lib.buttons.add(1)
    third = reader.read_pad()
    assert third.packet == first.packet + 1
    assert "B" in third.pressed


def test_read_pad_disconnected_returns_neutral_state():
    lib = _FakePadLib()
    lib.attached = 0
    state = _pad_reader(lib).read_pad()

    assert state.connected is False
    assert state.buttons == 0
    assert state.thumb_lx == state.thumb_ly == 0
    assert lib.button_calls == []
    assert lib.axis_calls == []
    assert lib.opened == 0
    assert lib.closed == 0


def test_read_pad_without_handle_is_disconnected():
    from pokecon.domain.sdl_sensor import SdlSensorReader

    lib = _FakePadLib()
    reader = SdlSensorReader()
    reader._ready = True
    reader._lib = lib

    state = reader.read_pad()
    assert state.connected is False
    assert lib.attached_handles == []
    assert lib.opened == 0


def test_read_pad_holds_lock_while_using_handle():
    """読取中に close がハンドルを閉じない（錠が待たせる）。"""
    import threading
    import time

    class _BlockingLib(_FakePadLib):
        def __init__(self):
            super().__init__()
            self.entered = threading.Event()
            self.release = threading.Event()

        def SDL_GameControllerGetJoystick(self, handle):
            self.entered.set()
            self.release.wait(2.0)
            return self.joy

    lib = _BlockingLib()
    reader = _pad_reader(lib)
    results = []
    closed = []
    close_started = False
    read_thread = threading.Thread(target=lambda: results.append(reader.read_pad()))
    close_thread = threading.Thread(target=lambda: closed.append(reader.close()))
    read_thread.start()
    try:
        assert lib.entered.wait(1.0)
        close_thread.start()
        close_started = True
        # 読取がハンドルを握っている間は close が完了できない。
        time.sleep(0.05)
        assert closed == []
        assert lib.closed == 0
    finally:
        lib.release.set()
        read_thread.join(2.0)
        if close_started:
            close_thread.join(2.0)
    assert read_thread.is_alive() is False
    assert close_thread.is_alive() is False
    assert results and results[0].connected is True
    assert closed == [None]
    assert lib.closed == 1


def test_read_pad_query_exception_returns_disconnected():
    """途中のボタン・軸の問い合わせ失敗を connected な部分状態にしない。"""

    class _BrokenButtons(_FakePadLib):
        def SDL_GameControllerGetButton(self, handle, num):
            raise RuntimeError("button query failed")

    class _BrokenAxis(_FakePadLib):
        def SDL_JoystickGetAxis(self, joy, axis):
            raise RuntimeError("axis query failed")

    for lib in (_BrokenButtons(), _BrokenAxis()):
        state = _pad_reader(lib).read_pad()
        assert state.connected is False
        assert state.buttons == 0
        assert state.pressed == frozenset()
        assert state.packet == 0
