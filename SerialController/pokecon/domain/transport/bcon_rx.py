#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""bcon_rx.py - BconTransport の受信ポンプ・RX 待機・フレーム配送 Mixin."""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from pokecon.domain.transport.bcon_protocol import (
    PROTO_VER,
    T_HELLO_ACK,
    T_PLAYER_INFO,
    T_RUMBLE,
    T_STATUS,
    BconParser,
)

#: 受信ポンプ 1 回の読み待ちタイムアウト（秒）。
BCON_READ_TIMEOUT = 0.05

#: 受信フレームの中身（種別・荷物・連番）。SEQ は方向別 mod256。
BconFrame = tuple[int, bytes, int]


class BconRxMixin:
    """RX ポンプ・TYPE 別フレーム待機・STATUS/SEQ 解釈の Mixin."""

    if TYPE_CHECKING:
        ser: Any
        _logger: Any
        _lock: threading.RLock
        _rx_lock: threading.Lock
        _rx_subs: list[Callable[[BconFrame], None]]
        _rx_waiters: list[tuple[tuple[int, ...], threading.Event, dict[str, Any]]]
        _parser: BconParser
        _rx_thread: threading.Thread | None
        _rx_stop: threading.Event
        _rx_dead: bool
        _rx_last_seq: int | None
        _rx_seq_gap: int
        _last_status: dict[str, int]
        _last_status_time: float
        _last_player_info: bytes
        _last_player_time: float
        _last_rumble: bytes
        _last_rumble_time: float
        _last_hello_ack: dict[str, int]
        _last_hello_time: float
        _hello_version: tuple[int, int] | None
        _last_frame_by_type: dict[int, tuple[BconFrame, float]]

        def hello_established(self) -> bool: ...

    def subscribe_rx(self, func: Callable[[BconFrame], None]) -> Callable[[], None]:
        """受信フレームごとの購読。戻り値は解除用の呼び出し。

        ポンプが死んでいたら起こし直す。止めた覚えがないのに止まって
        いる（USB の瞬断等）と、以後の購読側が何も受け取れなくなるため。
        """
        if not self.rx_pump_running():
            if self.ser is not None and not self._rx_stop.is_set():
                try:
                    self.start_rx_pump()
                except Exception:
                    self._logger.debug("rx ポンプの再起動に失敗", exc_info=True)
        with self._rx_lock:
            if func not in self._rx_subs:
                self._rx_subs.append(func)

        def _unsub() -> None:
            with self._rx_lock:
                if func in self._rx_subs:
                    self._rx_subs.remove(func)

        return _unsub

    def wait_rx(
        self,
        prefixes: Any = None,
        timeout: float = 0.5,
        wanted_types: Any = None,
    ) -> Any:
        """TYPE一致した最初の1フレームを待つ。見つかれば返す。

        基底のprefix前方一致ではなくフレームTYPE一致で待つ（バイナリに
        行が無いため）。送る前に待ちを登録してから送ること。送ってから
        登録するとポンプが先に読んで届かない（Task 4のhello/ping用）。
        基底と同名のprefixes口も受ける。不正値はNoneで返し投げない。
        """
        want: tuple[int, ...]
        raw: Any = wanted_types if wanted_types is not None else prefixes
        if raw is None:
            return None
        if isinstance(raw, int):
            want = (int(raw) & 0xFF,)
        elif isinstance(raw, (bytes, bytearray)):
            try:
                want = tuple(int(b) & 0xFF for b in bytes(raw))
            except (TypeError, ValueError):
                return None
            if not want:
                return None
        elif isinstance(raw, str):
            return None
        else:
            try:
                want = tuple(int(t) & 0xFF for t in raw)
            except (TypeError, ValueError, AttributeError):
                return None
            if not want:
                return None
        try:
            limit_raw = float(timeout)
        except (TypeError, ValueError):
            return None
        if math.isnan(limit_raw):
            limit = 0.0
        elif math.isinf(limit_raw):
            limit = 5.0
        else:
            limit = max(0.0, min(limit_raw, 5.0))
        if not self.rx_pump_running():
            if self.ser is None or self._rx_stop.is_set():
                return None
            try:
                if not self.start_rx_pump():
                    return None
            except Exception:
                self._logger.debug("rx ポンプの再起動に失敗", exc_info=True)
                return None
            if not self.rx_pump_running():
                return None
        found: dict[str, Any] = {}
        done = threading.Event()
        entry = (want, done, found)
        with self._rx_lock:
            self._rx_waiters.append(entry)
        try:
            if not done.wait(limit):
                return None
            frame = found.get("frame")
            if frame is None:
                return None
            return frame
        finally:
            with self._rx_lock:
                if entry in self._rx_waiters:
                    self._rx_waiters.remove(entry)

    def rx_dead(self) -> bool:
        """受信ポンプが諦めたか。

        待機側が「まだ来ない」と「もう死んでいる」を区別するために見る。
        立っている間は、待っても応答は来ない（起こし直しで消える）。
        """
        return bool(self._rx_dead)

    def rx_pump_running(self) -> bool:
        """読みポンプが動いているか（読むだけ）。"""
        thread = self._rx_thread
        return thread is not None and thread.is_alive()

    def start_rx_pump(self) -> bool:
        """読みポンプを1本だけ起動する。動いていれば True。

        確認と起動を同じ錠の中で行う。二重起動で線が2本になると、
        古い方が止められず漏れる。
        """
        with self._rx_lock:
            thread = self._rx_thread
            if thread is not None and thread.is_alive():
                return True
            if self.ser is None:
                return False
            self._rx_stop.clear()
            # 起こし直しでは「死んだ」印も消す。
            self._rx_dead = False
            thread = threading.Thread(
                target=self._rx_loop, name="BconRxPump", daemon=True
            )
            self._rx_thread = thread
            thread.start()
            return True

    def stop_rx_pump(self) -> None:
        """読みポンプを止める。止まるまで待つ（読みタイムアウトが上限）。

        待ちのあいだは錠を持たない。持ったまま join すると、起動側が
        止まるまで待たされる。
        """
        self._rx_stop.set()
        with self._rx_lock:
            thread = self._rx_thread
        if thread is None:
            return
        thread.join(BCON_READ_TIMEOUT + 0.5)
        with self._rx_lock:
            if self._rx_thread is thread and not thread.is_alive():
                self._rx_thread = None

    def _describe_errcode(self, errcode: int) -> str:
        """STATUS errcodeの日本語説明。CONFIG拒否は0x10+(TYPE&0x0F)で解く。"""
        table = {
            0x00: "正常",
            0x01: "LEN不正",
            0x02: "CRC不一致",
            0x03: "SEQ欠番",
            0x04: "未対応",
            0x05: "UART overrun",
            0x06: "overflow",
        }
        if errcode in table:
            return table[errcode]
        if 0x10 <= errcode <= 0x1F:
            return f"CONFIG拒否(0x10+(TYPE&0x0F)=0x{errcode:02X})"
        return f"不明(0x{errcode:02X})"

    def _note_rx_seq(self, seq: int) -> None:
        """Pico→PC方向のSEQ欠番を数える。初回は数えない。落とさない。"""
        try:
            seq = int(seq) & 0xFF
        except (TypeError, ValueError):
            return
        try:
            with self._lock:
                last = self._rx_last_seq
                if last is None:
                    self._rx_last_seq = seq
                    return
                if seq != ((last + 1) & 0xFF):
                    self._rx_seq_gap += 1
                self._rx_last_seq = seq
        except Exception:
            self._logger.debug("rx SEQ計数で例外", exc_info=True)

    def _note_status_frame(self, payload: bytes) -> None:
        """STATUS payloadを直近保持へ写す。errcode変化だけ可視化する。"""
        if len(payload) != 7:
            self._logger.debug(f"STATUS長が不正のため捨てます: {len(payload)}")
            return
        try:
            flags = payload[0]
            last_seq = payload[1]
            err_crc = int.from_bytes(payload[2:4], "little")
            err_drop = int.from_bytes(payload[4:6], "little")
            errcode = payload[6]
        except Exception:
            self._logger.debug("STATUS解釈で例外", exc_info=True)
            return
        try:
            with self._lock:
                old = int(self._last_status.get("errcode", 0))
                self._last_status = {
                    "flags": int(flags) & 0xFF,
                    "last_seq": int(last_seq) & 0xFF,
                    "err_crc": int(err_crc) & 0xFFFF,
                    "err_drop": int(err_drop) & 0xFFFF,
                    "errcode": int(errcode) & 0xFF,
                }
                self._last_status_time = time.perf_counter()
        except Exception:
            self._logger.debug("STATUS保持で例外", exc_info=True)
            return
        if errcode != 0 and errcode != old:
            if not self.hello_established():
                # hunt確定前の復号ノイズ。実エラーとして見せない。
                self._logger.debug(
                    "HELLO確立前のSTATUS異常を抑えました"
                    f"(errcode=0x{errcode:02X} {self._describe_errcode(errcode)}"
                    f" flags=0x{flags:02X} err_crc={err_crc} err_drop={err_drop})。"
                )
                return
            msg = (
                "bconのSTATUS異常を検出しました"
                f"(errcode=0x{errcode:02X} {self._describe_errcode(errcode)}"
                f" flags=0x{flags:02X} err_crc={err_crc} err_drop={err_drop})。"
                "正常復帰で0へ戻ります。"
            )
            print(msg)
            self._logger.warning(msg)

    def _dispatch_rx(self, frame: BconFrame) -> None:
        """1フレームを購読者と待機中の wait_rx へ届ける。

        届ける前に会話層の保持（直近TYPE・STATUS・HELLO_ACK・RX SEQ）を
        更新する。購読者・待機への配送は錠の外で行い、1件の失敗では
        落とさない。読み口は_rx_loopへ一本化し、ここではreadしない。
        """
        try:
            ftype = int(frame[0]) & 0xFF
        except (TypeError, ValueError, IndexError):
            ftype = -1
        try:
            now = time.perf_counter()
            with self._lock:
                if 0 <= ftype <= 0xFF:
                    self._last_frame_by_type[ftype] = (frame, now)
                if ftype == T_HELLO_ACK:
                    try:
                        payload = frame[1]
                        if len(payload) == 4:
                            adopted = int(payload[0]) & 0xFF
                            result = int(payload[3]) & 0xFF
                            self._last_hello_ack = {
                                "adopted": adopted,
                                "major": int(payload[1]) & 0xFF,
                                "minor": int(payload[2]) & 0xFF,
                                "result": result,
                            }
                            self._last_hello_time = now
                            # 入力許可は hello が待機中の ACK を採用した時だけ行う。
                            if adopted == PROTO_VER:
                                try:
                                    self._hello_version = (
                                        int(payload[1]) & 0xFF,
                                        int(payload[2]) & 0xFF,
                                    )
                                except (TypeError, ValueError, IndexError):
                                    pass
                    except Exception:
                        self._logger.debug("HELLO_ACK保持で例外", exc_info=True)
                elif ftype == T_PLAYER_INFO:
                    try:
                        self._last_player_info = bytes(frame[1])
                        self._last_player_time = now
                    except Exception:
                        self._logger.debug("PLAYER_INFO保持で例外", exc_info=True)
                elif ftype == T_RUMBLE:
                    try:
                        self._last_rumble = bytes(frame[1])
                        self._last_rumble_time = now
                    except Exception:
                        self._logger.debug("RUMBLE保持で例外", exc_info=True)
        except Exception:
            self._logger.debug("rx直近保持で例外", exc_info=True)
        try:
            self._note_rx_seq(frame[2])
        except Exception:
            self._logger.debug("rx SEQ記録で例外", exc_info=True)
        if ftype == T_STATUS:
            try:
                self._note_status_frame(frame[1])
            except Exception:
                self._logger.debug("STATUS記録で例外", exc_info=True)
        with self._rx_lock:
            subs = list(self._rx_subs)
            waiters = list(self._rx_waiters)
        for func in subs:
            try:
                func(frame)
            except Exception:
                self._logger.debug("rx 購読者で例外", exc_info=True)
        for want, done, found in waiters:
            try:
                if frame[0] in want:
                    found.setdefault("frame", frame)
                    done.set()
            except Exception:
                self._logger.debug("rx 待機の照合で例外", exc_info=True)

    def _rx_loop(self) -> None:
        """受信を読み続け、確定分を分配する。例外では落ちない。

        読み口はここへ一本化する。2か所で read すると横取りが起きる。
        到着確認（in_waiting）してから読む。ブロッキングreadは
        データ到着済みでも復帰が遅れる環境があり、PONG等の往復が
        タイムアウト周期に量子化される。到着を見てから読めば即復帰する。
        """
        streak = 0
        while not self._rx_stop.is_set():
            ser = self.ser
            if ser is None:
                break
            try:
                waiting = getattr(ser, "in_waiting", None)
                if waiting is None:
                    chunk = ser.read(64)
                else:
                    available = int(waiting() if callable(waiting) else waiting)
                    if available <= 0:
                        # 到着待ちの譲り（CPU を焼かない）。
                        time.sleep(0.001)
                        streak = 0
                        continue
                    chunk = ser.read(min(int(available), 64))
            except Exception:
                # 一時的な失敗（USB の揺らぎ等）は数回まで許す。1 回で諦めると
                # 以後チャンクが来ても誰も読まず、待機側はタイムアウトを
                # 繰り返すだけになる（原因が分からない）。
                streak += 1
                if streak <= 3:  # 一時的な失敗は 3 回まで許す
                    self._logger.debug("rx 読み取りに失敗。再試行します", exc_info=True)
                    time.sleep(0.02)  # 少し置いてから読み直す
                    continue
                self._rx_dead = True
                self._logger.error(
                    "rx 読み取りが続けて失敗。ポンプを止めます", exc_info=True
                )
                # print は LogPane の待ち行列経由なので裏からでも安全。
                print("受信が止まりました（シリアル接続を確認してください）")
                break
            streak = 0
            if not chunk:
                continue
            try:
                frames = self._parser.feed(bytes(chunk))
            except Exception:
                self._logger.debug("rx パーサで例外", exc_info=True)
                continue
            for frame in frames:
                self._dispatch_rx(frame)
