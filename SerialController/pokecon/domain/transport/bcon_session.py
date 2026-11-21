#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""bcon_session.py - BconTransport の会話層 (HELLO/PING/STATUS/BAUD/CONFIG) Mixin."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from pokecon.domain.transport.bcon_protocol import (
    BAUD_TABLE,
    BOOTSEL_MAGIC,
    DEFAULT_BAUD_INDEX,
    PROTO_VER,
    RESULT_DOWNGRADED,
    RESULT_OK,
    RESULT_UNSUPPORTED,
    T_BAUD_SET,
    T_BEACON_START,
    T_BOOTSEL,
    T_COLOR_GET,
    T_COLOR_INFO,
    T_EMULATE_MODE,
    T_HELLO,
    T_HELLO_ACK,
    T_NEUTRAL,
    T_PING,
    T_PONG,
    T_RECONNECT,
    T_STATUS,
    T_STATUS_REQ,
    T_WIRED_MODE,
    BconParser,
    frame_build,
)
from pokecon.domain.transport.bcon_shared import parse_clamped_timeout, zero_status

#: 既定ボーレート（1Mbps）。
BCON_DEFAULT_BAUDRATE = BAUD_TABLE[DEFAULT_BAUD_INDEX]

# baud hunt 定数。Switch-bcon の HUNT_DWELL_MS (150ms) x 4 slot に対応。
HUNT_PRIME_S = 0.7
HUNT_PRIME_GAP_S = 0.005
HUNT_QUICK_HELLO_S = 0.15
HUNT_UPGRADE_CONFIRM_S = 0.5

# EMULATE_MODE の役割番号。0=Pro Controller・1=Joy-Con (L)・2=Joy-Con (R)。
EMULATE_ROLE_NAMES = ("Pro Controller", "Joy-Con (L)", "Joy-Con (R)")


class BconSessionMixin:
    """HELLO/PING/STATUS/BAUD/CONFIG 会話と状態取得を担う Mixin."""

    if TYPE_CHECKING:
        ser: Any
        _logger: Any
        _lock: threading.RLock
        _write_lock: threading.Lock
        _rx_lock: threading.Lock
        _rx_waiters: list[tuple[tuple[int, ...], threading.Event, dict[str, Any]]]
        _parser: BconParser
        _tx_hold: bool
        _hello_established: bool
        _last_good_baud: int
        _last_status: dict[str, int]
        _last_player_info: bytes
        _last_rumble: bytes
        _live_pending: tuple[bytes, str, bool] | None
        _live_latest: tuple[bytes, str] | None
        _live_rtt_ms: float | None
        _on_write_begin: Callable[..., None] | None
        _on_write_end: Callable[..., None] | None

        def rx_pump_running(self) -> bool: ...
        def start_rx_pump(self) -> bool: ...
        def subscribe_rx(
            self, func: Callable[[tuple[int, bytes, int]], None]
        ) -> Callable[[], None]: ...
        def _take_seq(self) -> int: ...
        def _write_frame(
            self,
            frame: bytes,
            row: str,
            measure_perf: bool,
            begin: Callable[..., None] | None,
            end: Callable[..., None] | None,
            ser: Any,
        ) -> bool: ...
        def _current_baud(self) -> int | None: ...
        def _set_baud(self, rate: int) -> bool: ...

    def _send_session_frame(self, type_: int, payload: bytes, row_note: str) -> bool:
        """会話フレームを1発で送る。採番→組立→書込を1単位で包む。"""
        with self._write_lock:
            try:
                seq = self._take_seq()
                frame = frame_build(int(type_) & 0xFF, bytes(payload), seq)
            except ValueError as e:
                self._logger.warning(f"bconの会話フレームが組めません: {e}")
                return False
            except Exception as e:
                self._logger.warning(f"bconの会話フレーム組立に失敗: {e!r}")
                return False
            with self._lock:
                ser = self.ser
                begin = self._on_write_begin
                end = self._on_write_end
            if ser is None:
                self._logger.debug("bconは開いていないため会話を送りません")
                return False
            return bool(self._write_frame(frame, row_note, False, begin, end, ser))

    def _send_neutral_once(self) -> None:
        """NEUTRALを1発だけ送る（UNSUPPORTED時の維持用）。失敗は捨てる。"""
        try:
            self._send_session_frame(T_NEUTRAL, b"", "bcon:NEUTRAL")
        except Exception:
            self._logger.debug("NEUTRAL送出で例外", exc_info=True)

    def _gui(self, msg: str, quiet: bool, info: bool = False) -> None:
        if not quiet:
            print(msg)
        if info:
            self._logger.info(msg)
        else:
            self._logger.warning(msg)

    def _ensure_rx_ready(
        self,
        action: str,
        *,
        quiet: bool = False,
        announce: bool = True,
    ) -> bool:
        """ポート開通と受信ポンプ稼働を検証し、未稼働なら起動を試みる。"""
        if self.ser is None:
            if announce and action:
                self._gui(f"bconが開いていないため{action}。", quiet)
            return False
        if not self.rx_pump_running():
            try:
                if not self.start_rx_pump() or not self.rx_pump_running():
                    if announce and action:
                        self._gui(f"bconの受信ポンプが動かないため{action}。", quiet)
                    return False
            except Exception:
                if announce and action:
                    self._gui(f"bconの受信ポンプ起動に失敗したため{action}。", quiet)
                return False
        return True

    def _send_and_wait_frame(
        self,
        want_types: tuple[int, ...],
        frame_type: int,
        payload: bytes,
        row_note: str,
        limit: float,
    ) -> tuple[bool, Any]:
        """待機登録→フレーム送信→応答待ちの共通手順。"""
        found: dict[str, Any] = {}
        done = threading.Event()
        entry = (want_types, done, found)
        with self._rx_lock:
            self._rx_waiters.append(entry)
        try:
            ok = self._send_session_frame(frame_type, payload, row_note)
            if not ok:
                return False, None
            if not done.wait(limit):
                return True, None
            return True, found.get("frame")
        finally:
            with self._rx_lock:
                if entry in self._rx_waiters:
                    self._rx_waiters.remove(entry)

    def hello(self, timeout: float = 3.0, quiet: bool = False) -> bool:
        """HELLO(ver=4)を送りHELLO_ACKを確認する。失敗はFalse＋可視log。"""
        with self._write_lock:
            with self._lock:
                self._hello_established = False
                self._live_pending = None
                self._live_latest = None
        try:
            limit = parse_clamped_timeout(timeout, nan_value=0.0, min_s=0.0, max_s=10.0)
            if limit is None:
                self._logger.warning(f"bconのhello待ちが不正です: {timeout!r}")
                return False
            if not self._ensure_rx_ready("HELLOを送りません", quiet=quiet):
                return False
            if limit <= 0.0:
                self._logger.warning("bconのHELLO待ちが0のため送らずFalseを返します。")
                return False
            start = time.perf_counter()
            deadline = start + limit
            attempt = 0
            while True:
                remaining = deadline - time.perf_counter()
                if remaining <= 0.0:
                    break
                attempt += 1
                grain = min(0.5, remaining)
                sent_ok, got = self._send_and_wait_frame(
                    (T_HELLO_ACK,),
                    T_HELLO,
                    bytes([PROTO_VER, 0x01]),
                    "bcon:HELLO",
                    grain,
                )
                if not sent_ok:
                    self._gui("bconのHELLO送出に失敗しました。", quiet)
                    return False
                if got is None:
                    continue
                payload = got[1]
                if len(payload) != 4:
                    self._logger.warning(f"bconのHELLO_ACK長が不正です: {len(payload)}")
                    continue
                adopted = int(payload[0]) & 0xFF
                major = int(payload[1]) & 0xFF
                minor = int(payload[2]) & 0xFF
                result = int(payload[3]) & 0xFF
                if adopted == PROTO_VER and result == RESULT_OK:
                    with self._lock:
                        self._hello_established = True
                        rate = self._current_baud()
                        if rate is not None:
                            self._last_good_baud = rate
                    self._logger.info(f"bconのHELLOが通りました(v{major}.{minor})。")
                    return True
                if adopted == PROTO_VER and result == RESULT_DOWNGRADED:
                    msg = (
                        "bconがDOWNGRADEDで返しました"
                        f"(v{major}.{minor})。PCはver=4専用のため"
                        "入力送信を止めます。"
                    )
                    self._gui(msg, quiet)
                    self._send_neutral_once()
                    return False
                if result == RESULT_UNSUPPORTED or adopted != PROTO_VER:
                    msg = (
                        "bconの版が合いません"
                        f"(adopted=0x{adopted:02X} v{major}.{minor} "
                        f"RESULT=0x{result:02X})。PCはver=4で送っています。"
                        "入力送信を止めます。"
                    )
                else:
                    msg = (
                        "bconのHELLO応答が想定外です"
                        f"(adopted=0x{adopted:02X} v{major}.{minor} "
                        f"RESULT=0x{result:02X})。"
                        "入力送信を止めます。"
                    )
                self._gui(msg, quiet)
                self._send_neutral_once()
                return False
            msg = (
                "bconのHELLO_ACKが来ませんでした"
                f"({attempt}回再送・{limit:.1f}s)。"
                "再huntへ回してください(baud_hunt)。"
            )
            self._gui(msg, quiet)
            return False
        except Exception as e:
            self._logger.debug(f"helloで例外: {e!r}", exc_info=True)
            return False

    def ping(self, timeout: float = 1.0, quiet: bool = False) -> float | None:
        """PINGを送りPONGのSEQエコーでRTT秒を返す。失敗はNone＋可視log。"""
        try:
            limit = parse_clamped_timeout(timeout, nan_value=0.0, min_s=0.0, max_s=5.0)
            if limit is None:
                self._logger.warning(f"bconのping待ちが不正です: {timeout!r}")
                return None
            if not self._ensure_rx_ready("PINGを送りません", quiet=quiet):
                return None
            if limit <= 0.0:
                self._logger.warning("bconのPING待ちが0のため送らずNoneを返します。")
                return None
            matched: dict[str, Any] = {}
            done = threading.Event()

            def _on_pong(frame: tuple[int, bytes, int]) -> None:
                try:
                    if (int(frame[0]) & 0xFF) != T_PONG:
                        return
                    want = matched.get("seq")
                    if want is None:
                        return
                    payload = frame[1]
                    if len(payload) != 1:
                        return
                    if (int(payload[0]) & 0xFF) == (int(want) & 0xFF):
                        matched["rtt"] = time.perf_counter() - float(
                            matched.get("t0", 0.0)
                        )
                        done.set()
                except Exception:
                    self._logger.debug("PONG照合で例外", exc_info=True)

            unsub = self.subscribe_rx(_on_pong)
            try:
                with self._write_lock:
                    try:
                        sent_seq = self._take_seq()
                        frame = frame_build(T_PING, b"", sent_seq)
                    except Exception as e:
                        self._logger.warning(f"bconのPING組立に失敗: {e!r}")
                        return None
                    with self._lock:
                        ser = self.ser
                        begin = self._on_write_begin
                        end = self._on_write_end
                    if ser is None:
                        return None
                    matched["seq"] = sent_seq
                    matched["t0"] = time.perf_counter()
                    write_began = time.perf_counter()
                    self._write_frame(frame, "bcon:PING", False, begin, end, ser)
                    write_ms = (time.perf_counter() - write_began) * 1000.0
                if done.wait(limit):
                    try:
                        rtt = float(matched.get("rtt", 0.0))
                    except (TypeError, ValueError):
                        return None
                    try:
                        match_ms = max(0.0, rtt * 1000.0 - write_ms)
                        print(
                            f"PING内訳: 書込{write_ms:.1f}ms・応答待ち{match_ms:.1f}ms"
                        )
                    except Exception:
                        pass
                    try:
                        with self._lock:
                            self._live_rtt_ms = rtt * 1000.0
                    except Exception:
                        self._logger.debug("RTT保持で例外", exc_info=True)
                    return rtt
            finally:
                try:
                    unsub()
                except Exception:
                    self._logger.debug("PONG購読解除で例外", exc_info=True)
            msg = f"bconのPONGが来ませんでした({limit:.1f}s)。"
            self._gui(msg, quiet)
            return None
        except Exception as e:
            self._logger.debug(f"pingで例外: {e!r}", exc_info=True)
            return None

    def last_status(self) -> dict[str, int]:
        """直近STATUSの写しを返す（flags/last_seq/err_crc/err_drop/errcode）。"""
        try:
            with self._lock:
                return dict(self._last_status)
        except Exception:
            return zero_status()

    def hello_established(self) -> bool:
        """HELLO_ACKが成立しているか。線に触れない。例外は投げない。"""
        try:
            with self._lock:
                return bool(self._hello_established)
        except Exception:
            return False

    def last_player_info(self) -> bytes:
        """直近PLAYER_INFO payloadの写し。未受信は空。例外は投げない。"""
        try:
            with self._lock:
                return bytes(self._last_player_info)
        except Exception:
            return b""

    def last_rumble(self) -> bytes:
        """直近RUMBLE payloadの写し。未受信は空。例外は投げない。"""
        try:
            with self._lock:
                return bytes(self._last_rumble)
        except Exception:
            return b""

    def request_status(
        self, timeout: float = 1.0, quiet: bool = False
    ) -> dict[str, int] | None:
        """STATUS_REQを送り即時STATUSを待つ。PLAYER_INFO付随も受ける。"""
        try:
            limit = parse_clamped_timeout(timeout, nan_value=0.0, min_s=0.0, max_s=5.0)
            if limit is None:
                self._logger.warning(f"bconの状態確認待ちが不正です: {timeout!r}")
                return None
            if not self._ensure_rx_ready("状態確認を送りません", quiet=quiet):
                return None
            if limit <= 0.0:
                self._logger.warning(
                    "bconの状態確認待ちが0のため送らずNoneを返します。"
                )
                return None
            sent_ok, frame = self._send_and_wait_frame(
                (T_STATUS,), T_STATUS_REQ, b"", "bcon:STATUS_REQ", limit
            )
            if not sent_ok:
                self._gui("bconの状態確認送出に失敗しました。", quiet)
                return None
            if frame is None:
                if self.hello_established():
                    self._gui(f"bconのSTATUSが来ませんでした({limit:.1f}s)。", quiet)
                else:
                    self._logger.debug(
                        f"HELLO確立前のSTATUS欠落を抑えました({limit:.1f}s)。"
                    )
                return None
            return self.last_status()
        except Exception as e:
            self._logger.debug(f"request_statusで例外: {e!r}", exc_info=True)
            return None

    def _prime_hunt(self, seconds: float = HUNT_PRIME_S) -> None:
        """現rateでNEUTRALを短い間隔で流し、hunt中のPicoを確定させる。"""
        end = time.perf_counter() + max(0.0, float(seconds))
        while time.perf_counter() < end:
            if not self._send_session_frame(T_NEUTRAL, b"", "bcon:HUNT"):
                return
            time.sleep(HUNT_PRIME_GAP_S)

    def baud_hunt(
        self,
        candidates: list[int] | tuple[int, ...] | None = None,
        timeout_per: float = 0.5,
    ) -> int | None:
        """baudをlast-good先頭＋降順sweepで探し2連続有効でlockする。"""
        try:
            per = parse_clamped_timeout(
                timeout_per, nan_value=0.5, min_s=0.1, max_s=2.0, fallback=0.5
            )
            assert per is not None
            if not self._ensure_rx_ready("baud huntしません"):
                return None
            order: list[int] = []
            if candidates is None:
                try:
                    with self._lock:
                        last_good = int(self._last_good_baud)
                except (TypeError, ValueError):
                    last_good = BCON_DEFAULT_BAUDRATE
                all_rates = sorted(set(BAUD_TABLE.values()), reverse=True)
                order = [last_good] + [r for r in all_rates if r != last_good]
            else:
                try:
                    raw_list = list(candidates)
                except TypeError:
                    self._gui(f"bconのhunt候補が不正です: {candidates!r}", False)
                    return None
                for item in raw_list:
                    try:
                        value = int(item)  # type: ignore[arg-type]
                    except (TypeError, ValueError):
                        continue
                    if 0 <= value <= 4 and value in BAUD_TABLE:
                        order.append(int(BAUD_TABLE[value]))
                    elif value > 0:
                        order.append(int(value))
                seen: set[int] = set()
                dedup: list[int] = []
                for rate in order:
                    if rate not in seen:
                        dedup.append(rate)
                        seen.add(rate)
                order = dedup
                if not order:
                    self._gui(f"bconのhunt候補が空です: {candidates!r}", False)
                    return None
            initial_rate = self._current_baud()
            was_ready = self.hello_established()
            self._logger.info(
                f"bconのbaud huntを始めます(候補={order})。STATE送出を抑えます。"
            )
            with self._write_lock:
                with self._lock:
                    previous_hold = self._tx_hold
                    self._tx_hold = True
                    self._live_pending = None
                    self._live_latest = None
            locked: int | None = None
            try:
                for rate in order:
                    try:
                        if not self._set_baud(rate):
                            continue
                        with self._lock:
                            self._parser = BconParser()
                        quick = min(per, HUNT_QUICK_HELLO_S)
                        if not self.hello(timeout=quick, quiet=True):
                            self._prime_hunt()
                            if not self.hello(timeout=per, quiet=True):
                                continue
                        if not self.hello(timeout=per, quiet=True):
                            continue
                        with self._lock:
                            self._last_good_baud = int(rate)
                        locked = int(rate)
                        break
                    except Exception:
                        self._logger.debug("hunt1候補で例外", exc_info=True)
                        continue
            finally:
                with self._write_lock:
                    with self._lock:
                        self._tx_hold = previous_hold
            if locked is None and was_ready and initial_rate is not None:
                if self._confirm_hunted_baud(initial_rate, per) and self.hello(
                    timeout=per, quiet=True
                ):
                    self._gui(
                        f"探索では応答を得られませんでしたが、元の{initial_rate}bpsでHELLOを再確認しました。",
                        False,
                        info=True,
                    )
                    return initial_rate
            if locked is None:
                self._gui(
                    f"bconのbaud huntに失敗しました(候補={order})。"
                    "配線・電源を確認してください。",
                    False,
                )
                return None
            return self._upgrade_hunted_baud(locked)
        except Exception as e:
            self._logger.debug(f"baud_huntで例外: {e!r}", exc_info=True)
            return None

    def _confirm_hunted_baud(self, rate: int, timeout: float) -> bool:
        """lockしたrateへ戻してHELLOで確認する。失敗はFalse。"""
        if self._current_baud() != rate:
            if not self._set_baud(rate):
                return False
            with self._lock:
                self._parser = BconParser()
        if self.hello(timeout=timeout, quiet=True):
            return True
        self._prime_hunt()
        return self.hello(timeout=timeout, quiet=True)

    def _upgrade_hunted_baud(self, locked: int) -> int | None:
        """huntでlockしたrateから既定baud（1Mbps）へ引き上げる。"""
        target = int(BAUD_TABLE[DEFAULT_BAUD_INDEX])
        if locked >= target:
            self._gui(
                f"bconのbaudを{locked}bpsでlockしました(2連続有効)。",
                False,
                info=True,
            )
            return locked
        self._logger.info(f"bconのbaudを{locked}bpsから{target}bpsへ引き上げます。")
        try:
            upgraded = bool(self.set_baud_index(DEFAULT_BAUD_INDEX))
        except Exception:
            self._logger.debug("baud引き上げで例外", exc_info=True)
            upgraded = False
        if upgraded:
            with self._lock:
                self._last_good_baud = target
            self._gui(
                f"bconのbaudを{target}bpsでlockしました"
                f"({locked}bpsから引き上げ・HELLOでadopt確認)。",
                False,
                info=True,
            )
            return target
        try:
            confirmed = self._confirm_hunted_baud(locked, HUNT_UPGRADE_CONFIRM_S)
        except Exception:
            self._logger.debug("hunt復帰確認で例外", exc_info=True)
            confirmed = False
        if confirmed:
            with self._lock:
                self._last_good_baud = locked
            self._gui(
                f"bconのbaudを{locked}bpsのまま採用しました"
                f"({target}bpsへ引き上げできず、旧rateでHELLO確認済み)。",
                False,
            )
            return locked
        self._gui(
            f"bconのbaudを{target}bpsへ引き上げられず、"
            f"{locked}bpsでの復帰もHELLOで確認できませんでした。"
            "配線・電源を確認して再huntしてください。",
            False,
        )
        return None

    def set_baud_index(self, index: int, timeout: float = 2.0) -> bool:
        """BAUD_SETでPicoとhostのbaudを切り替える。失敗はFalse＋可視log。"""
        try:
            try:
                idx = int(index)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                self._gui(f"bconのbaud指定が不正です: {index!r}", False)
                return False
            if idx not in BAUD_TABLE:
                self._gui(f"bconのbaud指定が範囲外です: {index!r}(0-4)。", False)
                return False
            limit = parse_clamped_timeout(
                timeout, nan_value=2.0, min_s=0.2, max_s=5.0, fallback=2.0
            )
            assert limit is not None
            if not self._ensure_rx_ready("baud切替しません"):
                return False
            old_rate = self._current_baud()
            if old_rate is None:
                self._gui("bconの現在baudが取れないため切替しません。", False)
                return False
            new_rate = int(BAUD_TABLE[idx])
            if old_rate == new_rate:
                self._logger.info(f"bconのbaudは既に{new_rate}bpsです。")
                return True
            deadline = time.perf_counter() + limit
            with self._write_lock:
                with self._lock:
                    previous_hold = self._tx_hold
                    self._tx_hold = True
                    self._live_pending = None
                    self._live_latest = None
            try:
                sent_ok, frame = self._send_and_wait_frame(
                    (T_STATUS,),
                    T_BAUD_SET,
                    bytes([idx & 0xFF]),
                    "bcon:BAUD_SET",
                    min(0.8, limit),
                )
                if not sent_ok:
                    self._gui("bconのBAUD_SET送出に失敗しました。", False)
                    return False
                if frame is None:
                    self._gui(
                        "bconのBAUD_SET応答(STATUS)が来ないため切替しません。", False
                    )
                    return False
                try:
                    time.sleep(0.1)
                except Exception:
                    pass
                if not self._set_baud(new_rate):
                    self._gui(f"bconのhost側を{new_rate}bpsへ切替えられません。", False)
                    return False
                with self._lock:
                    self._parser = BconParser()
                # Core1のguard開始はACK受信と非同期。NEUTRALを連続で送り、
                # 新rateの2連続受信・SEQ再同期を済ませてからHELLOを確認する。
                self._prime_hunt(
                    seconds=min(0.15, max(0.0, deadline - time.perf_counter()))
                )
                remaining = deadline - time.perf_counter()
                if remaining > 0 and self.hello(timeout=remaining, quiet=True):
                    with self._lock:
                        self._last_good_baud = new_rate
                    self._gui(
                        f"bconのbaudを{new_rate}bpsへ切替えました"
                        "(HELLOでadopt確認。Pico側はadopt後にBCBRへ永続保存します)。",
                        False,
                        info=True,
                    )
                    return True
                try:
                    self._set_baud(old_rate)
                except Exception:
                    pass
                with self._lock:
                    try:
                        self._parser = BconParser()
                    except Exception:
                        pass
                self._gui(
                    f"bconの新baud({new_rate}bps)でadopt確認できないため"
                    f"{old_rate}bpsへ戻す設定を行いました。HELLO による確認が必要です。",
                    False,
                )
                return False
            finally:
                with self._write_lock:
                    with self._lock:
                        self._tx_hold = previous_hold
        except Exception as e:
            self._logger.debug(f"set_baud_indexで例外: {e!r}", exc_info=True)
            return False

    def set_wired_mode(self, enable: bool, timeout: float = 1.0) -> bool:
        """WIRED_MODEを切り替える。失敗はFalse＋可視log。"""
        try:
            flag = bool(enable)
            limit = parse_clamped_timeout(
                timeout, nan_value=1.0, min_s=0.1, max_s=5.0, fallback=1.0
            )
            assert limit is not None
            if not self._ensure_rx_ready("有線切替しません"):
                return False
            sent_ok, frame = self._send_and_wait_frame(
                (T_STATUS,),
                T_WIRED_MODE,
                bytes([0x01 if flag else 0x00]),
                "bcon:WIRED_MODE",
                limit,
            )
            if not sent_ok:
                self._gui("bconの有線切替送出に失敗しました。", False)
                return False
            if frame is None:
                self._gui(f"bconの有線切替応答が来ませんでした({limit:.1f}s)。", False)
                return False
            if flag:
                note = (
                    "bconを有線へ切替えました。Flash保存・約500ms後自発再起動"
                    "します。再起動中のSTATE送出は止め、復帰後に再HELLOから"
                    "やり直してください。有線中のCAPTURE/BEACONは0x10/0x11"
                    "拒否になります。"
                )
            else:
                note = (
                    "bconを無線へ切替えました。Flash保存・約500ms後自発再起動"
                    "します。再起動中のSTATE送出は止め、復帰後に再HELLOから"
                    "やり直してください。復帰後はCAPTURE/BEACONが使えます。"
                )
            self._gui(note, False)
            return True
        except Exception as e:
            self._logger.debug(f"set_wired_modeで例外: {e!r}", exc_info=True)
            return False

    def set_emulate_mode(self, role: int, timeout: float = 1.0) -> bool:
        """コントローラ種別(EMULATE_MODE)を切り替える。失敗はFalse＋可視log。"""
        try:
            try:
                role_value = int(role)
            except (TypeError, ValueError):
                role_value = -1
            if role_value not in (0, 1, 2):
                self._gui(f"bconの種別切替は0-2で指定してください: {role!r}", False)
                return False
            limit = parse_clamped_timeout(
                timeout, nan_value=1.0, min_s=0.1, max_s=5.0, fallback=1.0
            )
            assert limit is not None
            if not self._ensure_rx_ready("種別切替しません"):
                return False
            sent_ok, frame = self._send_and_wait_frame(
                (T_STATUS,),
                T_EMULATE_MODE,
                bytes([role_value]),
                "bcon:EMULATE_MODE",
                limit,
            )
            if not sent_ok:
                self._gui("bconの種別切替送出に失敗しました。", False)
                return False
            if frame is None:
                self._gui(f"bconの種別切替応答が来ませんでした({limit:.1f}s)。", False)
                return False
            name = EMULATE_ROLE_NAMES[role_value]
            note = (
                f"bconを{name}へ切替えました。Flash保存・約500ms後自発再起動"
                "します。再起動中のSTATE送出は止め、復帰後に再HELLOから"
                "やり直してください。"
            )
            self._gui(note, False)
            return True
        except Exception as e:
            self._logger.debug(f"set_emulate_modeで例外: {e!r}", exc_info=True)
            return False

    def _send_until_status(
        self,
        type_: int,
        payload: bytes,
        limit: float,
        label: str,
    ) -> bool:
        """STATUS-ACK が来るまで同じフレームを繰り返し送る。"""
        found: dict[str, Any] = {}
        done = threading.Event()
        entry = ((T_STATUS,), done, found)
        with self._rx_lock:
            self._rx_waiters.append(entry)
        try:
            deadline = time.monotonic() + limit
            sent = 0
            while not done.is_set():
                if time.monotonic() >= deadline:
                    return False
                if not self._send_session_frame(type_, payload, label):
                    return False
                sent += 1
                done.wait(0.08)
            self._logger.debug(f"{label}: {sent}回送ってACKを受けた")
            return True
        finally:
            with self._rx_lock:
                if entry in self._rx_waiters:
                    self._rx_waiters.remove(entry)

    def request_bootsel(self, timeout: float = 1.0) -> bool:
        """BOOTSEL突入を要求する。失敗はFalse＋可視log。"""
        try:
            limit = parse_clamped_timeout(
                timeout, nan_value=1.0, min_s=0.1, max_s=5.0, fallback=1.0
            )
            assert limit is not None
            if not self._ensure_rx_ready("BOOTSEL要求しません"):
                return False
            if not self._send_until_status(
                T_BOOTSEL,
                bytes([BOOTSEL_MAGIC]),
                limit,
                "bcon:BOOTSEL",
            ):
                self._gui(f"bconのBOOTSEL応答が来ませんでした({limit:.1f}s)。", False)
                return False
            self._logger.info(
                "bconへBOOTSEL突入を要求しました。約500ms後自発再起動"
                "します。再起動中のSTATE送出は止め、復帰後に再HELLOから"
                "やり直してください。"
            )
            return True
        except Exception as e:
            self._logger.debug(f"request_bootselで例外: {e!r}", exc_info=True)
            return False

    def request_reconnect(self, timeout: float = 1.0) -> bool:
        """拒否されて待機状態に入った Pico を明示的に起こす。失敗はFalse＋log。"""
        try:
            limit = parse_clamped_timeout(
                timeout, nan_value=1.0, min_s=0.1, max_s=5.0, fallback=1.0
            )
            assert limit is not None
            if not self._ensure_rx_ready("再接続要求しません"):
                return False
            if not self._send_until_status(
                T_RECONNECT,
                b"",
                limit,
                "bcon:RECONNECT",
            ):
                self._gui(f"bconの再接続応答が来ませんでした({limit:.1f}s)。", False)
                return False
            self._logger.info("bconへ再接続を要求しました。Picoが再接続を試みます。")
            return True
        except Exception as e:
            self._logger.debug(f"request_reconnectで例外: {e!r}", exc_info=True)
            return False

    def send_config(self, type_: int, payload: bytes) -> bool:
        """CONFIG系を1発で送る。送出の成否を返す。"""
        try:
            kind = int(type_) & 0xFF
            body = bytes(payload)
        except (TypeError, ValueError):
            return False
        return bool(self._send_session_frame(kind, body, f"bcon:0x{kind:02X}"))

    def request_color(self, timeout: float = 1.0) -> bytes | None:
        """COLOR_GETを送りCOLOR_INFOの12Bを待つ。失敗・無応答はNone。"""
        try:
            limit = parse_clamped_timeout(
                timeout, nan_value=1.0, min_s=0.0, max_s=5.0, fallback=1.0
            )
            assert limit is not None
            if not self._ensure_rx_ready("", announce=False):
                return None
            if limit <= 0.0:
                return None
            sent_ok, frame = self._send_and_wait_frame(
                (T_COLOR_INFO,), T_COLOR_GET, b"", "bcon:COLOR_GET", limit
            )
            if not sent_ok or frame is None:
                return None
            try:
                payload = bytes(frame[1])
            except (TypeError, ValueError, IndexError):
                return None
            if len(payload) != 12:
                return None
            return payload
        except Exception as e:
            self._logger.debug(f"request_colorで例外: {e!r}", exc_info=True)
            return None

    def send_beacon(self, timeout: float = 2.0) -> dict[str, int] | None:
        """BEACON_STARTを送り直後のSTATUS写しを返す。失敗・無応答はNone。"""
        try:
            limit = parse_clamped_timeout(
                timeout, nan_value=2.0, min_s=0.1, max_s=5.0, fallback=2.0
            )
            assert limit is not None
            if not self._ensure_rx_ready("BEACONを送りません"):
                return None
            sent_ok, frame = self._send_and_wait_frame(
                (T_STATUS,), T_BEACON_START, b"", "bcon:BEACON_START", limit
            )
            if not sent_ok:
                self._gui("bconのBEACON送出に失敗しました。", False)
                return None
            if frame is None:
                self._gui(f"bconのBEACON応答が来ませんでした({limit:.1f}s)。", False)
                return None
            return self.last_status()
        except Exception as e:
            self._logger.debug(f"send_beaconで例外: {e!r}", exc_info=True)
            return None
