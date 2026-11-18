#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""bcon.py - bconバイナリをpyserialで送る実装（線そのもの）。

公開入口・ポート開閉・STATE/KEYBOARD/IMU送出をここに置き、120Hz/200Hz
ループ（bcon_loops）・RXポンプ/解釈（bcon_rx）・会話層（bcon_session）へ
責務を分割している。テストの `bcon.serial.Serial` 差し替え口は維持する。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from logging import DEBUG, NullHandler, getLogger
from typing import Any

import serial
from pokecon.domain.transport.base import BCON_STATE, Transport
from pokecon.domain.transport.bcon_loops import BconLoopsMixin
from pokecon.domain.transport.bcon_protocol import (
    BAUD_TABLE,
    DEFAULT_BAUD_INDEX,
    T_IMU_SAMPLE,
    T_KEYBOARD,
    T_STATE,
    BconParser,
    frame_build,
)
from pokecon.domain.transport.bcon_rx import (
    BCON_READ_TIMEOUT as BCON_READ_TIMEOUT,
    BconFrame as BconFrame,
    BconRxMixin,
)
from pokecon.domain.transport.bcon_session import (
    BCON_DEFAULT_BAUDRATE as BCON_DEFAULT_BAUDRATE,
    EMULATE_ROLE_NAMES as EMULATE_ROLE_NAMES,
    HUNT_PRIME_GAP_S as HUNT_PRIME_GAP_S,
    HUNT_PRIME_S as HUNT_PRIME_S,
    HUNT_QUICK_HELLO_S as HUNT_QUICK_HELLO_S,
    HUNT_UPGRADE_CONFIRM_S as HUNT_UPGRADE_CONFIRM_S,
    BconSessionMixin,
)
from pokecon.domain.transport.bcon_shared import (
    IMU_INTERVAL_S as IMU_INTERVAL_S,
    LIVE_INTERVAL_S as LIVE_INTERVAL_S,
    clamp_i16,
    clamp_imu_tuple,
    precise_timer as _precise_timer,
    zero_status,
)

BCON_WRITE_TIMEOUT = 0.2

# 後方互換の既定値参照を明示（未参照警告回避）。
_ = (BAUD_TABLE, DEFAULT_BAUD_INDEX, _precise_timer)

# 姿勢空間（Keys.Button・Senderの持つ姿勢・PicoのS行）からwire空間へのbit写像。
_POSTURE_TO_WIRE = (2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15)


def _s_row_to_wire_row(row: str) -> str:
    """Pico式S行（姿勢空間）をModifiedのwire行へ読み替える。"""
    parts = row.strip().split()
    if len(parts) != 7 or parts[0].upper() != "S":
        raise ValueError(f"bconへ送れないS行形式です: {row!r}")
    try:
        posture = int(parts[1], 16)
    except ValueError:
        raise ValueError(f"bconへ送れないS行形式です: {row!r}") from None
    wire = 0x0003
    bits = int(posture) & 0x3FFF
    for bit in range(14):
        if bits & (1 << bit):
            wire |= 1 << _POSTURE_TO_WIRE[bit]
    return f"{wire:x} {parts[2]} {parts[3]} {parts[4]} {parts[5]} {parts[6]}"


def _is_s_row(text: str) -> bool:
    """Pico式S行か（先頭S＋空白、またはS単独の崩れ）。wire行と紛れない。"""
    upper = text.upper()
    return upper.startswith("S ") or upper == "S"


def decode_rumble(payload: bytes) -> tuple[int, int]:
    """RUMBLE payload（2B: 左・右の強さ0-255）を通読用の対へ直す。"""
    try:
        data = bytes(payload)
    except (TypeError, ValueError):
        return (0, 0)
    if len(data) < 2:
        return (0, 0)
    try:
        return (int(data[0]) & 0xFF, int(data[1]) & 0xFF)
    except (TypeError, ValueError, IndexError):
        return (0, 0)


class BconTransport(BconSessionMixin, BconRxMixin, BconLoopsMixin, Transport):
    """bcon用。STATEバイナリを送りSTATUS/PONG/ACKを読む。"""

    #: is_open が手元で完結する（子プロセス経由の方式は立てない）。
    is_open_is_local = True

    name = "switch-bcon-inproc"
    capability = BCON_STATE

    def __init__(self, logger: Any = None, use_len12: bool = False) -> None:
        self._tx_listeners: list[Callable[[str], None]] = []
        self._tx_last_note: str | None = None
        self._tx_listener_ng: set[int] = set()
        self.ser: Any = None
        self._logger = logger if logger is not None else getLogger(__name__)
        if logger is None:
            self._logger.addHandler(NullHandler())
            self._logger.setLevel(DEBUG)
            self._logger.propagate = True
        self._lock = threading.RLock()
        # 順序は常に _write_lock → _lock（逆順取得禁止）。
        self._write_lock = threading.Lock()
        self._tx_seq = 0
        self.use_len12 = bool(use_len12)
        self._on_write_begin: Callable[..., None] | None = None
        self._on_write_end: Callable[..., None] | None = None
        self._rx_lock = threading.Lock()
        self._rx_subs: list[Callable[[BconFrame], None]] = []
        self._rx_waiters: list[
            tuple[tuple[int, ...], threading.Event, dict[str, Any]]
        ] = []
        self._parser = BconParser()
        self._rx_thread: threading.Thread | None = None
        self._rx_stop = threading.Event()
        self._rx_dead = False
        self._last_tx = 0.0
        self._last_status: dict[str, int] = zero_status()
        self._last_status_time = 0.0
        self._last_player_info = b""
        self._last_player_time = 0.0
        self._last_rumble = b""
        self._last_rumble_time = 0.0
        self._last_hello_ack: dict[str, int] = {
            "adopted": 0,
            "major": 0,
            "minor": 0,
            "result": 0,
        }
        self._last_hello_time = 0.0
        self._hello_version: tuple[int, int] | None = None
        self._hello_established = False
        self._last_good_baud = BCON_DEFAULT_BAUDRATE
        self._tx_hold = False
        self._input_hold = False
        self._open_error: str | None = None
        self._rx_last_seq: int | None = None
        self._rx_seq_gap = 0
        self._live_loop_thread: threading.Thread | None = None
        self._live_loop_stop = threading.Event()
        self._live_loop_event = threading.Event()
        self._live_interval_s = LIVE_INTERVAL_S
        self._live_pending: tuple[bytes, str, bool] | None = None
        self._live_latest: tuple[bytes, str] | None = None
        self._live_last_tx = 0.0
        self._live_sent = 0
        self._live_merged = 0
        self._live_dropped = 0
        self._live_rtt_ms: float | None = None
        self._imu_loop_thread: threading.Thread | None = None
        self._imu_loop_stop = threading.Event()
        self._imu_gen = 0
        self._imu_interval_s = IMU_INTERVAL_S
        self._imu_source: (
            Callable[[], tuple[int, int, int, int, int, int] | None] | None
        ) = None
        self._imu_owner = ""
        self._imu_sent = 0
        self._imu_dropped = 0
        self._imu_listeners: list[
            Callable[[tuple[int, int, int, int, int, int]], None]
        ] = []
        self._last_frame_by_type: dict[int, tuple[BconFrame, float]] = {}

    def _clear_receive_locked(self) -> None:
        """接続境界で前の機器の受信値を残さない。_lock 保持で呼ぶ。"""
        self._last_status = zero_status()
        self._last_status_time = 0.0
        self._last_hello_ack = dict.fromkeys(self._last_hello_ack, 0)
        self._last_hello_time = 0.0
        self._hello_version = None
        self._last_player_info = b""
        self._last_player_time = 0.0
        self._last_rumble = b""
        self._last_rumble_time = 0.0
        self._last_frame_by_type.clear()

    def open(
        self,
        portNum: int,
        portName: str = "",
        baudrate: int = BCON_DEFAULT_BAUDRATE,
        **extra: Any,
    ) -> bool:
        """線を開く。既定1Mbps。不正・不良はFalse＋logで落とさない。"""
        _ = extra
        with self._write_lock:
            with self._lock:
                self._clear_receive_locked()
                self._hello_established = False
                self._live_pending = None
                self._live_latest = None
        self._open_error = None
        try:
            rate = int(baudrate)  # type: ignore[arg-type]
        except (TypeError, ValueError, OverflowError):
            self._open_error = f"通信速度の指定が不正です: {baudrate!r}"
            self._logger.warning(self._open_error)
            return False
        if rate <= 0:
            self._open_error = f"通信速度の指定が不正です: {baudrate!r}"
            self._logger.warning(self._open_error)
            return False
        name = portName.strip() if isinstance(portName, str) else ""
        if not name:
            try:
                num = int(portNum)
            except (TypeError, ValueError, OverflowError):
                self._open_error = f"ポート番号が不正です: {portNum!r}"
                self._logger.warning(self._open_error)
                return False
            if num < 0:
                self._open_error = f"ポート番号が不正です: {num}"
                self._logger.warning(self._open_error)
                return False
            try:
                from pokecon.domain.transport.text_serial import (
                    TextSerialTransport as _TextSerial,
                )

                default_path = _TextSerial._default_port_path(num)
            except Exception as exc:
                self._open_error = str(exc)
                self._logger.debug("既定パス解決に失敗", exc_info=True)
                default_path = None
            if default_path is None:
                self._open_error = (
                    self._open_error or "この OS ではポート名を直接指定してください。"
                )
                self._logger.warning(
                    "Not supported OS: portName を直接指定してください"
                )
                return False
            name = default_path
        try:
            ser = serial.Serial(
                port=name,
                baudrate=rate,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=BCON_READ_TIMEOUT,
                write_timeout=BCON_WRITE_TIMEOUT,
            )
        except Exception as e:
            self._open_error = str(e)
            self._logger.warning(f"bconを開けませんでした({name} {rate}bps): {e!r}")
            return False
        with self._write_lock:
            with self._lock:
                old = self.ser
                self.ser = ser
                self._parser = BconParser()
                self._live_pending = None
                self._live_latest = None
                self._tx_last_note = None
                self._live_last_tx = 0.0
                self._live_sent = 0
                self._live_merged = 0
                self._live_dropped = 0
                self._live_rtt_ms = None
                self._hello_established = False
                self._input_hold = False
                self._tx_hold = False
                self._rx_last_seq = None
                self._rx_seq_gap = 0
        if old is not None:
            try:
                old.close()
            except Exception as e:
                self._logger.debug(f"bconの古い線の close に失敗: {e!r}")
        return True

    def close(self) -> None:
        """線を閉じる。読みポンプを先に止め、閉じるのは錠の外で行う。"""
        try:
            self.stop_live_loop()
        except Exception:
            self._logger.debug("live loopの停止に失敗", exc_info=True)
        try:
            self.stop_imu_loop()
        except Exception:
            self._logger.debug("IMU loopの停止に失敗", exc_info=True)
        try:
            if self.ser is not None:
                with self._lock:
                    held = self._tx_hold
                if not held:
                    self._send_neutral_once()
        except Exception:
            self._logger.debug("終了時NEUTRALに失敗", exc_info=True)
        self.stop_rx_pump()
        with self._write_lock:
            with self._lock:
                ser, self.ser = self.ser, None
                self._parser = BconParser()
                self._hello_established = False
                self._clear_receive_locked()
                self._live_pending = None
                self._live_latest = None
        if ser is None:
            return
        try:
            ser.close()
        except Exception as e:
            self._logger.debug(f"bconの close に失敗: {e!r}")

    def last_open_error(self) -> str | None:
        return self._open_error

    def current_baud(self) -> int | None:
        return self._current_baud()

    def hold_input(self, hold: bool) -> bool:
        """書込と同じ錠で入力を抑止し、保留姿勢を持ち越さない。"""
        with self._write_lock:
            with self._lock:
                if not hold and not self._hello_established:
                    return False
                self._input_hold = bool(hold)
                if hold:
                    self._live_pending = None
                    self._live_latest = None
                return True

    def is_open(self) -> bool:
        """開いているか。pySerialのis_open属性を優先する。"""
        if self.ser is None:
            return False
        prop = getattr(self.ser, "is_open", None)
        if isinstance(prop, bool):
            return prop
        legacy = getattr(self.ser, "isOpen", None)
        if callable(legacy):
            try:
                return bool(legacy())
            except Exception as e:
                self._logger.debug(f"is_open 判定に失敗: {e!r}")
                return False
        return False

    def get_raw_serial(self) -> None:
        """生のシリアルは渡さない。bconはバイナリ専用のため直読み禁止。"""
        return None

    def send_row(self, row: str, measure_perf: bool = True) -> None:
        """1行ぶんの姿勢をSTATEバイナリ1発で送る。"""
        with self._lock:
            held = self._tx_hold or self._input_hold or not self._hello_established
        if held:
            with self._lock:
                self._live_dropped += 1
            self._logger.debug("bconはbaud hunt中のためSTATE送出を抑えます")
            return
        from pokecon.domain.serial.bcon_mapping import (
            state_to_len8,
            state_to_len12,
            wire_row_to_state,
        )

        try:
            text = row.strip() if isinstance(row, str) else ""
            if _is_s_row(text):
                state = wire_row_to_state(_s_row_to_wire_row(text))
            else:
                state = wire_row_to_state(row)
        except ValueError as e:
            self._logger.warning(f"bconへ送れない行のため捨てます: {e}")
            return
        except Exception as e:
            self._logger.warning(f"bconの行変換に失敗したため捨てます: {e!r}")
            return
        if self.use_len12:
            payload = state_to_len12(state)
        else:
            payload = state_to_len8(state)
        note = row if isinstance(row, str) else ""
        with self._lock:
            thread = self._live_loop_thread
            loop_on = thread is not None and thread.is_alive()
            if loop_on:
                if self._tx_hold or self._input_hold or not self._hello_established:
                    self._live_dropped += 1
                    return
                if self._live_pending is not None:
                    self._live_merged += 1
                self._live_pending = (payload, note, bool(measure_perf))
                event = self._live_loop_event
        if loop_on:
            try:
                event.set()
            except Exception:
                self._logger.debug("live loopの起床に失敗", exc_info=True)
            return
        with self._write_lock:
            with self._lock:
                if self._tx_hold or self._input_hold or not self._hello_established:
                    self._live_dropped += 1
                    return
                seq = self._tx_seq
                self._tx_seq = (self._tx_seq + 1) & 0xFF
                ser = self.ser
                begin = self._on_write_begin
                end = self._on_write_end
            if ser is None:
                self._logger.debug("bconは開いていないため送りません")
                return
            frame = frame_build(T_STATE, payload, seq)
            sent_ok = bool(self._write_frame(frame, row, measure_perf, begin, end, ser))
            with self._lock:
                if sent_ok:
                    self._live_sent += 1
                if not self._input_hold and self._hello_established:
                    self._live_latest = (payload, note)
                    self._live_last_tx = time.perf_counter()
            if sent_ok:
                self._notify_tx(note if isinstance(note, str) else "")

    def send_keyboard(self, report: bytes) -> bool:
        """HID boot keyboard report 8B を KEYBOARD 1発で送る。成否を返す。"""
        try:
            if len(report) != 8:
                return False
            body = bytes(report)
            if len(body) != 8:
                return False
        except Exception:
            return False
        with self._write_lock:
            with self._lock:
                if self._tx_hold or self._input_hold or not self._hello_established:
                    return False
                ser = self.ser
            if ser is None:
                return False
            seq = self._take_seq()
            frame = frame_build(T_KEYBOARD, body, seq)
            return self._write_frame(
                frame,
                row="bcon:KEYBOARD",
                measure_perf=False,
                begin=None,
                end=None,
                ser=ser,
            )

    def send_imu(
        self,
        ax: int,
        ay: int,
        az: int,
        gx: int,
        gy: int,
        gz: int,
    ) -> bool:
        """IMU生値6値をIMU_SAMPLE 1発で送る。成否を返す。"""
        try:
            vals = (int(ax), int(ay), int(az), int(gx), int(gy), int(gz))
        except (TypeError, ValueError):
            self._logger.debug("bconのIMU値が数値でないため捨てます")
            return False
        payload = b"".join(
            clamp_i16(v).to_bytes(2, "little", signed=True) for v in vals
        )
        with self._lock:
            held = self._tx_hold or self._input_hold or not self._hello_established
        if held:
            with self._lock:
                self._imu_dropped += 1
            return False
        with self._write_lock:
            with self._lock:
                if self._tx_hold or self._input_hold or not self._hello_established:
                    self._imu_dropped += 1
                    return False
                seq = self._tx_seq
                self._tx_seq = (self._tx_seq + 1) & 0xFF
                ser = self.ser
            if ser is None:
                self._logger.debug("bconは開いていないためIMUを送りません")
                return False
            try:
                frame = frame_build(T_IMU_SAMPLE, payload, seq)
            except Exception as e:
                self._logger.debug(f"bconのIMU組立に失敗: {e!r}")
                return False
            sent_ok = bool(self._write_frame(frame, "bcon:IMU", False, None, None, ser))
        if sent_ok:
            with self._lock:
                self._imu_sent += 1
            self._notify_imu(clamp_imu_tuple(vals))
        return sent_ok

    def flush_pending(self) -> None:
        """保留があれば送り切る。loop起動中は起こして即送させる。"""
        try:
            if self.live_loop_running():
                self._live_loop_event.set()
        except Exception:
            self._logger.debug("flushの起床に失敗", exc_info=True)

    def _take_seq(self) -> int:
        """送信用SEQを1つ採番する（方向別mod256）。"""
        with self._lock:
            seq = self._tx_seq
            self._tx_seq = (self._tx_seq + 1) & 0xFF
            return seq

    def _write_frame(
        self,
        frame: bytes,
        row: str,
        measure_perf: bool,
        begin: Callable[..., None] | None,
        end: Callable[..., None] | None,
        ser: Any,
    ) -> bool:
        """1フレームを1回のwriteで出す。成否を返す。"""
        if begin is not None:
            try:
                begin(row, measure_perf)
            except Exception:
                self._logger.debug("書き出し前フックで例外", exc_info=True)
        ok = False
        try:
            count = ser.write(frame)
            ok = count is None or int(count) == len(frame)
            if end is not None:
                try:
                    end(row, measure_perf)
                except Exception:
                    self._logger.debug("書き出し後フックで例外", exc_info=True)
        except serial.SerialTimeoutException as e:
            print("Serial write timeout")
            self._logger.error(f"Serial write timeout: {e}")
        except serial.serialutil.SerialException as e:
            print(e)
            self._logger.error(f"Error : {e}")
        except AttributeError as e:
            print("Using a port that is not open.")
            self._logger.error(f"Maybe Using a port that is not open.: {e}")
        except Exception as e:
            self._logger.error(f"bconの書込に失敗: {e!r}")
        finally:
            if ok:
                with self._lock:
                    self._last_tx = time.perf_counter()
        return ok

    def _current_baud(self) -> int | None:
        """現在のbaud率。取れなければNone（落とさない）。"""
        try:
            ser = self.ser
            if ser is None:
                return None
            rate = getattr(ser, "baudrate", None)
            if rate is None:
                return None
            return int(rate)
        except (TypeError, ValueError):
            return None
        except Exception:
            self._logger.debug("baud取得で例外", exc_info=True)
            return None

    def _set_baud(self, rate: int) -> bool:
        """host側baudを切り替える。失敗はFalse（落とさない）。"""
        try:
            with self._write_lock:
                ser = self.ser
                if ser is None:
                    return False
                with self._lock:
                    self._hello_established = False
                    self._live_pending = None
                    self._live_latest = None
                ser.baudrate = int(rate)
            return True
        except (TypeError, ValueError, AttributeError) as e:
            self._logger.debug(f"baud切替に失敗: {e!r}")
            return False
        except Exception as e:
            self._logger.debug(f"baud切替で例外: {e!r}")
            return False
